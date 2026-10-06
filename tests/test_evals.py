"""Eval harness: case loading, scorers, runner and report, against fake and recorded pipelines (no live LLM)."""

import json
from types import SimpleNamespace

import pytest

from conftest import FIXTURES
from raac.answerer import Answer, Citation, Sentence
from raac.config import Models
from raac.evals import EvalCaseError, load_cases, run_eval, summarize, write_report
from raac.pipeline import IndexVersion, LocalPipeline, PipelineResult, SeccionRef
from raac.retriever import Retrieval, RetrievedSeccion

SEED = FIXTURES.parent.parent / "evals" / "cases.jsonl"
URL = "https://docs.anac.gob.ar/index.php/s/PtMG8j8sFeRyren"


def _case(**over):
    case = {
        "id": "c1",
        "question": "¿Puedo volar VFR de noche?",
        "expected_secciones": [{"parte": "61", "seccion": "61.535"}],
        "reference_answer": "Sí, con condiciones.",
        "out_of_scope": False,
    }
    case.update(over)
    return case


def _write(tmp_path, *cases):
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in cases) + "\n")
    return path


def _citation(seccion: str) -> Citation:
    return Citation("61", seccion, "t", 67, 67, "10", "10", "VI", "I", "mayo 2026", URL, "texto")


class FakePipeline:
    """A second Pipeline implementation: canned results per question."""

    name = "fake"

    def __init__(self, results):
        self._results = results

    def models(self):
        return {"answer": "fake-model"}

    def index_versions(self):
        return [IndexVersion("61", "abc123", "VI", "I")]

    def run(self, standalone_question):
        result = self._results[standalone_question]
        if isinstance(result, Exception):
            raise result
        return result


# Case loading


def test_seed_cases_load_and_include_out_of_scope(parte61):
    cases = load_cases(SEED)
    assert any(c.out_of_scope for c in cases)
    for case in cases:
        for ref in case.expected_secciones:
            assert ref.parte == "61"
            parte61.seccion(ref.seccion)  # every expected Sección exists in Parte 61


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"extra": 1}, "unknown keys"),
        ({"question": " "}, "'question'"),
        ({"out_of_scope": "no"}, "out_of_scope"),
        ({"expected_secciones": ["61.535"]}, "expected_secciones"),
        ({"expected_secciones": []}, "in-scope cases need some"),
        ({"out_of_scope": True}, "out-of-scope cases have no expected"),
    ],
)
def test_malformed_cases_are_rejected(tmp_path, override, message):
    with pytest.raises(EvalCaseError, match=message):
        load_cases(_write(tmp_path, _case(**override)))


def test_duplicate_ids_are_rejected(tmp_path):
    with pytest.raises(EvalCaseError, match="duplicate"):
        load_cases(_write(tmp_path, _case(), _case()))


# Runner and scorers


@pytest.fixture
def report(tmp_path):
    cases = load_cases(
        _write(
            tmp_path,
            _case(id="hit", question="q-hit", expected_secciones=[{"parte": "61", "seccion": "61.535"}, {"parte": "61", "seccion": "61.520"}]),
            _case(id="miss", question="q-miss"),
            _case(id="fuera-ok", question="q-fuera-ok", expected_secciones=[], out_of_scope=True),
            _case(id="fuera-mal", question="q-fuera-mal", expected_secciones=[], out_of_scope=True),
            _case(id="roto", question="q-roto"),
        )
    )
    answered = Answer(sentences=[Sentence("Sí.", [_citation("61.535")])], refused=False, model="fake-served")
    pipeline = FakePipeline(
        {
            "q-hit": PipelineResult(["61"], [SeccionRef("61", "61.530"), SeccionRef("61", "61.535")], answered),
            "q-miss": PipelineResult(["61"], [SeccionRef("61", "61.140")], Answer([], refused=True, dropped_uncited=["No hay respaldo."])),
            "q-fuera-ok": PipelineResult(["61"], [], Answer([], refused=True)),
            "q-fuera-mal": PipelineResult(
                ["61"],
                [SeccionRef("61", "61.140")],
                Answer([Sentence("Algo.", [_citation("61.140")])], refused=False, dropped_uncited=["Sin cita."]),
            ),
            "q-roto": RuntimeError("boom"),
        }
    )
    return run_eval(pipeline, cases)


def _by_id(report):
    return {c["id"]: c for c in report["cases"]}


def test_per_case_scores(report):
    cases = _by_id(report)
    assert cases["hit"]["scores"] == {"retrieval_hit": True, "retrieval_recall": 0.5, "grounding": 1.0, "refusal_correct": True}
    assert cases["hit"]["cited_secciones"] == [{"parte": "61", "seccion": "61.535"}]
    assert cases["miss"]["scores"] == {"retrieval_hit": False, "retrieval_recall": 0.0, "grounding": None, "refusal_correct": False}
    assert cases["fuera-ok"]["scores"] == {"retrieval_hit": None, "retrieval_recall": None, "grounding": None, "refusal_correct": True}
    assert cases["fuera-mal"]["scores"]["grounding"] == 0.5
    assert cases["fuera-mal"]["scores"]["refusal_correct"] is False
    assert cases["roto"]["scores"] is None and cases["roto"]["error"] == "RuntimeError: boom"


def test_aggregate_scores_count_errors_as_failures(report):
    agg = report["aggregate"]
    assert (agg["cases"], agg["in_scope"], agg["out_of_scope"], agg["errors"]) == (5, 3, 2, 1)
    assert agg["retrieval_hit_rate"] == pytest.approx(1 / 3)
    assert agg["retrieval_recall_mean"] == pytest.approx(0.5 / 3)
    assert agg["grounded_rate"] == pytest.approx(1 / 3)  # hit grounded, fuera-mal not, roto errored
    assert agg["uncited_sentences"] == 2
    assert agg["refusal_rate_out_of_scope"] == 0.5
    assert agg["false_refusal_rate_in_scope"] == pytest.approx(1 / 3)


def test_report_records_pipeline_models_and_index_versions(report, tmp_path):
    assert report["pipeline"] == {
        "name": "fake",
        "models": {"answer": "fake-model"},
        "served_answer_models": ["fake-served"],
        "index_versions": [{"parte": "61", "content_hash": "abc123", "edicion": "VI", "enmienda": "I", "from_cache": False}],
    }
    path = tmp_path / "out" / "report.json"
    write_report(report, path)
    assert json.loads(path.read_text()) == report
    text = summarize(report)
    assert "retrieval_hit_rate: 0.33" in text and "Parte 61 abc123" in text


# The local pipeline, replaying the recorded Citations API response


class _StubRetriever:
    def __init__(self, retrieval):
        self.retrieval = retrieval

    def retrieve(self, standalone_question):
        return self.retrieval


class _StubAnthropic:
    def __init__(self, response):
        message = SimpleNamespace(model_dump=lambda mode: response)
        stream = SimpleNamespace(get_final_message=lambda: message)
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: _Ctx(stream)))


class _Ctx:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *exc):
        return False


def test_local_pipeline_runs_through_the_harness(parte61, tmp_path):
    record = json.loads((FIXTURES / "citations-response-61535.json").read_text())
    retrieval = Retrieval(
        routed_partes=["61"],
        secciones=[RetrievedSeccion(parte61, parte61.seccion(s["seccion"])) for s in record["secciones"]],
    )
    models = Models(indexing="claude-haiku-4-5", routing="claude-haiku-4-5", search="claude-opus-5-5", answer="claude-opus-5-5")
    pipeline = LocalPipeline(_StubRetriever(retrieval), _StubAnthropic(record["response"]), models, [parte61], {"61": URL})
    cases = [c for c in load_cases(SEED) if c.id == "61-vfr-nocturno-ppl"]
    report = run_eval(pipeline, cases)

    assert report["pipeline"]["name"] == "local"
    assert report["pipeline"]["models"] == {
        "indexing": "claude-haiku-4-5",
        "routing": "claude-haiku-4-5",
        "search": "claude-opus-5-5",
        "answer": "claude-opus-5-5",
    }
    assert report["pipeline"]["served_answer_models"] == [record["response"]["model"]]
    assert report["pipeline"]["index_versions"] == [
        {"parte": "61", "content_hash": parte61.content_hash, "edicion": "VI", "enmienda": "I", "from_cache": False}
    ]
    case = report["cases"][0]
    assert case["error"] is None
    assert case["scores"]["retrieval_hit"] is True
    assert case["scores"]["refusal_correct"] is True
    assert {"parte": "61", "seccion": "61.535"} in case["cited_secciones"]
