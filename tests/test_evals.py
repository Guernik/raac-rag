"""Eval harness: case loading, scorers, runner and report, against fake and recorded pipelines (no live LLM)."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import FIXTURES
from raac.answerer import Answer, Citation, Sentence
from raac.config import Models
from raac import pipeline as pipeline_module
from raac.corpus import CorpusError, ParteListing
from raac.evals import (
    EvalCaseError,
    ReplayError,
    load_cases,
    load_retrievals,
    run_eval,
    score,
    select_cases,
    summarize,
    write_report,
)
from raac.pipeline import (
    AnswerOnlyError,
    DefinicionRef,
    IndexVersion,
    LocalPipeline,
    PipelineResult,
    PipelineRetrieval,
    SeccionRef,
)
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
    """A second Pipeline implementation: canned results per question; counts calls per stage."""

    name = "fake"

    def __init__(self, results):
        self._results = results
        self.calls = {"run": 0, "retrieve": 0, "answer": [], "definiciones": []}

    def models(self):
        return {"answer": "fake-model"}

    def index_versions(self):
        return [IndexVersion("61", "abc123", "VI", "I")]

    def _result(self, standalone_question):
        result = self._results[standalone_question]
        if isinstance(result, Exception):
            raise result
        return result

    def run(self, standalone_question):
        self.calls["run"] += 1
        return self._result(standalone_question)

    def retrieve(self, standalone_question):
        self.calls["retrieve"] += 1
        result = self._result(standalone_question)
        return PipelineRetrieval(result.routed_partes, result.retrieved_secciones, result.visited_nodes, result.definiciones)

    def take_usage(self):
        return {}

    def answer(self, standalone_question, secciones, definiciones=None):
        self.calls["answer"].append((standalone_question, secciones))
        self.calls["definiciones"].append(definiciones)
        return self._result(standalone_question).answer


# Case loading


def test_seed_cases_load_and_include_out_of_scope(parte61, parte1):
    cases = load_cases(SEED)
    assert any(c.out_of_scope for c in cases)
    partes = {"61": parte61, "1": parte1}
    for case in cases:
        for ref in case.expected_secciones:
            partes[ref.parte].seccion(ref.seccion)  # every expected Sección exists


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
def cases(tmp_path):
    return load_cases(
        _write(
            tmp_path,
            _case(id="hit", question="q-hit", expected_secciones=[{"parte": "61", "seccion": "61.535"}, {"parte": "61", "seccion": "61.520"}]),
            _case(id="miss", question="q-miss"),
            _case(id="fuera-ok", question="q-fuera-ok", expected_secciones=[], out_of_scope=True),
            _case(id="fuera-mal", question="q-fuera-mal", expected_secciones=[], out_of_scope=True),
            _case(id="roto", question="q-roto"),
        )
    )


@pytest.fixture
def fake(cases):
    answered = Answer(sentences=[Sentence("Sí.", [_citation("61.535")])], refused=False, model="fake-served")
    return FakePipeline(
        {
            "q-hit": PipelineResult(
                ["61"],
                [SeccionRef("61", "61.530"), SeccionRef("61", "61.535")],
                answered,
                ["61:0007"],
                [DefinicionRef("1", "1.11", "Noche")],
            ),
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


@pytest.fixture
def report(fake, cases):
    return run_eval(fake, cases)


def _by_id(report):
    return {c["id"]: c for c in report["cases"]}


def test_per_case_scores(report):
    cases = _by_id(report)
    assert cases["hit"]["scores"] == {"retrieval_hit": True, "retrieval_recall": 0.5, "grounding": 1.0, "refusal_correct": True, "correctness": None}
    assert cases["hit"]["cited_secciones"] == [{"parte": "61", "seccion": "61.535"}]
    assert cases["miss"]["scores"] == {"retrieval_hit": False, "retrieval_recall": 0.0, "grounding": None, "refusal_correct": False, "correctness": None}
    assert cases["fuera-ok"]["scores"] == {"retrieval_hit": None, "retrieval_recall": None, "grounding": None, "refusal_correct": True, "correctness": None}
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
    assert report["stages"] == ["retrieval", "answer"] and report["retrievals_from"] is None
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
    assert text.startswith("stages: retrieval, answer")


# Per-stage runs and case subsets


def test_retrieval_stage_makes_no_answer_call(fake, cases):
    report = run_eval(fake, cases, stage="retrieval")
    assert fake.calls["answer"] == [] and fake.calls["retrieve"] == len(cases)
    assert report["stages"] == ["retrieval"]
    assert summarize(report).startswith("stages: retrieval\n")
    hit = _by_id(report)["hit"]
    assert hit["scores"] == {"retrieval_hit": True, "retrieval_recall": 0.5, "grounding": None, "refusal_correct": None, "correctness": None}
    assert hit["answer"] is None and hit["served_model"] is None
    assert hit["visited_nodes"] == ["61:0007"]
    agg = report["aggregate"]
    assert agg["retrieval_hit_rate"] == pytest.approx(1 / 3)
    for key in ("grounded_rate", "uncited_sentences", "refusal_rate_out_of_scope", "false_refusal_rate_in_scope"):
        assert agg[key] is None


@pytest.fixture
def recorded(fake, cases, tmp_path):
    path = tmp_path / "retrieval.json"
    write_report(run_eval(fake, cases, stage="retrieval"), path)
    return path


def test_answer_stage_replays_recorded_retrievals_without_searching(cases, recorded):
    ok = [c for c in cases if c.id != "roto"]
    answers = {
        "q-hit": Answer([Sentence("Sí.", [_citation("61.535")])], refused=False, model="fake-served"),
        "q-miss": Answer([], refused=True),
        "q-fuera-ok": Answer([], refused=True),
        "q-fuera-mal": Answer([Sentence("Algo.", [_citation("61.140")])], refused=False),
    }
    replayer = FakePipeline({q: PipelineResult([], [], a) for q, a in answers.items()})
    replayer.retrieve = lambda q: pytest.fail("the answer stage must not route or search")

    retrievals = load_retrievals(recorded)
    report = run_eval(replayer, ok, stage="answer", retrievals=retrievals)

    assert report["stages"] == ["answer"] and report["retrievals_from"] == str(recorded)
    assert "retrievals replayed from:" in summarize(report)
    assert replayer.calls["answer"][0] == ("q-hit", [SeccionRef("61", "61.530"), SeccionRef("61", "61.535")])
    assert [q for q, _ in replayer.calls["answer"]] == ["q-hit", "q-miss", "q-fuera-ok", "q-fuera-mal"]
    assert replayer.calls["definiciones"][0] == [DefinicionRef("1", "1.11", "Noche")]
    hit = _by_id(report)["hit"]
    assert hit["retrieved_secciones"] == [{"parte": "61", "seccion": "61.530"}, {"parte": "61", "seccion": "61.535"}]
    assert hit["scores"] == {"retrieval_hit": None, "retrieval_recall": None, "grounding": 1.0, "refusal_correct": True, "correctness": None}
    agg = report["aggregate"]
    assert agg["retrieval_hit_rate"] is None and agg["retrieval_recall_mean"] is None
    assert agg["refusal_rate_out_of_scope"] == 0.5 and agg["false_refusal_rate_in_scope"] == 0.5


def test_answer_stage_refuses_unusable_recordings(fake, cases, recorded, tmp_path):
    with pytest.raises(ReplayError, match="needs recorded Retrievals"):
        run_eval(fake, cases, stage="answer")
    with pytest.raises(ReplayError, match=r"errored for \['roto'\]"):
        run_eval(fake, cases, stage="answer", retrievals=load_retrievals(recorded))
    changed = [c if c.id != "hit" else replace(c, question="otra") for c in cases if c.id != "roto"]
    with pytest.raises(ReplayError, match=r"question changed since the recording for \['hit'\]"):
        run_eval(fake, changed, stage="answer", retrievals=load_retrievals(recorded))
    answer_only = tmp_path / "answer.json"
    write_report(run_eval(fake, [c for c in cases if c.id != "roto"], stage="answer", retrievals=load_retrievals(recorded)), answer_only)
    with pytest.raises(ReplayError, match="did not run the retrieval stage"):
        load_retrievals(answer_only)


def test_case_subset_keeps_file_order_and_rejects_unknown_ids(fake, cases):
    subset = select_cases(cases, ["fuera-ok", "hit"])
    assert [c.id for c in subset] == ["hit", "fuera-ok"]
    report = run_eval(fake, subset)
    assert [c["id"] for c in report["cases"]] == ["hit", "fuera-ok"] and fake.calls["run"] == 2
    assert select_cases(cases, None) == cases
    with pytest.raises(EvalCaseError, match=r"unknown case ids \['nope'\]"):
        select_cases(cases, ["hit", "nope"])


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
        self.requests = []

        def start(**kw):
            self.requests.append(kw)
            return _Ctx(stream)

        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=start))


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

    usage = record["response"]["usage"]
    assert case["usage"]["answer"]["input_tokens"] == usage["input_tokens"]
    assert case["usage"]["answer"]["output_tokens"] == usage["output_tokens"]
    assert case["cost_usd"] == pytest.approx((usage["input_tokens"] * 4 + usage["output_tokens"] * 20) / 1e6)


_MODELS = Models(indexing="claude-haiku-4-5", routing="claude-haiku-4-5", search="claude-opus-5-5", answer="claude-opus-5-5")


def test_local_answer_only_pipeline_replays_recorded_secciones(parte61):
    record = json.loads((FIXTURES / "citations-response-61535.json").read_text())
    refs = [SeccionRef(s["parte"], s["seccion"]) for s in record["secciones"]]
    claude = _StubAnthropic(record["response"])
    pipeline = LocalPipeline(None, claude, _MODELS, [parte61], {"61": URL})

    with pytest.raises(AnswerOnlyError):
        pipeline.retrieve(record["question"])
    result = pipeline.answer(record["question"], refs)

    (request,) = claude.requests
    titles = [b["title"] for b in request["messages"][0]["content"] if b["type"] == "document"]
    assert titles == [f"RAAC Parte 61 - Sección {r.seccion} {parte61.seccion(r.seccion).title}" for r in refs]
    assert not result.refused and any(c.seccion == "61.535" for s in result.sentences for c in s.citations)
    assert pipeline.take_usage()["answer"].input_tokens == record["response"]["usage"]["input_tokens"]
    with pytest.raises(KeyError, match="Parte 91 is not loaded"):
        pipeline.answer(record["question"], [SeccionRef("91", "91.1")])


def _listing(parte):
    return ParteListing(parte=parte, titulo="t", share_url=URL)


def test_answer_pipeline_loads_the_recorded_version_from_cache(parte61, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_module, "load_models", lambda: _MODELS)
    monkeypatch.setattr(pipeline_module.anthropic, "Anthropic", lambda: None)
    monkeypatch.setattr(pipeline_module.corpus, "fetch_listings", lambda partes: [_listing(p) for p in partes])
    (tmp_path / f"raac-61-{parte61.content_hash[:16]}.pdf").write_bytes((FIXTURES / "raac-parte-61.pdf").read_bytes())

    built = pipeline_module.build_answer_pipeline([IndexVersion("61", parte61.content_hash, "VI", "I")], tmp_path)
    assert built.index_versions() == [IndexVersion("61", parte61.content_hash, "VI", "I")]
    assert built.answer("q", []).refused  # nothing to answer from, and no call made

    stale = "0" * 64
    (tmp_path / f"raac-61-{stale[:16]}.pdf").write_bytes((FIXTURES / "raac-parte-61.pdf").read_bytes())
    with pytest.raises(CorpusError, match="recorded version 0000000000000000 is not available"):
        pipeline_module.build_answer_pipeline([IndexVersion("61", stale)], tmp_path)


# CLI wiring


@pytest.mark.parametrize(
    "argv",
    [
        ["eval", "--stage", "answer"],
        ["eval", "--retrievals", "r.json"],
        ["eval", "--stage", "answer", "--retrievals", "r.json", "--parte", "61"],
    ],
)
def test_eval_cli_rejects_inconsistent_stage_flags(argv):
    from raac.cli import main

    with pytest.raises(SystemExit):
        main(argv)


def test_eval_cli_fails_on_unknown_case_before_building_a_pipeline(monkeypatch):
    from raac import cli

    monkeypatch.setattr(cli, "build_local_pipeline", lambda *a, **k: pytest.fail("built a pipeline"))
    with pytest.raises(EvalCaseError, match="unknown case ids"):
        cli.main(["eval", "--cases", str(SEED), "--case", "no-existe"])


def test_seccion_of_an_attached_definicion_counts_as_retrieved(tmp_path):
    [case] = load_cases(
        _write(tmp_path, _case(expected_secciones=[{"parte": "61", "seccion": "61.430"}, {"parte": "1", "seccion": "1.11"}]))
    )
    with_def = PipelineRetrieval(["61"], [SeccionRef("61", "61.430")], definiciones=[DefinicionRef("1", "1.11", "Área de control (CTA)")])
    assert score(case, with_def, None).retrieval_recall == 1.0
    without = PipelineRetrieval(["61"], [SeccionRef("61", "61.430")])
    assert score(case, without, None).retrieval_recall == 0.5
