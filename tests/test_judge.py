"""Correctness judge: request, verdict parsing and harness wiring, replaying recorded judge responses (no live LLM)."""

import json
from types import SimpleNamespace

import pytest

from conftest import FIXTURES
from raac import strings
from raac.answerer import Answer, Sentence, map_response
from raac.config import load_judge_model
from raac.evals import load_cases, run_eval, summarize
from raac.judge import CorrectnessJudge, answer_text, build_request, parse_verdict
from raac.pipeline import IndexVersion, PipelineResult, SeccionRef
from raac.retriever import Retrieval, RetrievedSeccion

RECORDED = json.loads((FIXTURES / "judge-responses.json").read_text())
EVALS = FIXTURES.parent.parent / "evals"
URL = "https://docs.anac.gob.ar/index.php/s/PtMG8j8sFeRyren"
MODEL = RECORDED["correcta"]["request"]["model"]


class _StubClient:
    """messages.create answers from recorded responses, by the user message it receives."""

    def __init__(self, by_content: dict[str, dict], fail: bool = False):
        self.by_content = by_content
        self.fail = fail
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request):
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("overloaded")
        response = self.by_content[request["messages"][0]["content"]]
        return SimpleNamespace(to_dict=lambda: response)


def _recorded_client() -> _StubClient:
    return _StubClient({r["request"]["messages"][0]["content"]: r["response"] for r in RECORDED.values()})


@pytest.fixture(scope="module")
def answers(parte61):
    """The Answers the recorded judge responses were made for."""
    record = json.loads((FIXTURES / "citations-response-61535.json").read_text())
    retrieval = Retrieval(
        routed_partes=["61"],
        secciones=[RetrievedSeccion(parte61, parte61.seccion(s["seccion"])) for s in record["secciones"]],
    )
    real = map_response(record["response"], retrieval, {"61": URL})
    wrong = Answer(
        [
            Sentence(
                "Sí, la licencia de piloto privado incluye siempre las atribuciones VFR nocturnas, sin ningún requisito adicional.",
                next(s for s in real.sentences if s.citations).citations,
            )
        ],
        refused=False,
    )
    return {"correcta": real, "incorrecta": wrong, "rechazo": Answer([], refused=True)}


@pytest.fixture(scope="module")
def cases():
    return {c.id: c for f in ("cases.jsonl", "refusal-cases.jsonl") for c in load_cases(EVALS / f)}


@pytest.mark.parametrize("name", ["correcta", "incorrecta", "rechazo"])
def test_request_matches_the_recording(name, answers, cases):
    case = cases[RECORDED[name]["case"]]
    assert build_request(MODEL, case.question, case.reference_answer, answers[name]) == RECORDED[name]["request"]


def test_request_has_question_reference_and_answer_but_no_citations(answers, cases):
    case = cases["61-vfr-nocturno-ppl"]
    content = build_request(MODEL, case.question, case.reference_answer, answers["correcta"])["messages"][0]["content"]
    assert case.question in content and case.reference_answer in content
    cited = next(s for s in answers["correcta"].sentences if s.citations)
    assert cited.text in content
    assert cited.citations[0].cited_text not in content


@pytest.mark.parametrize(("name", "score", "verdict"), [("correcta", 1.0, "correcta"), ("incorrecta", 0.0, "incorrecta"), ("rechazo", 1.0, "correcta")])
def test_recorded_verdicts(name, score, verdict):
    result = parse_verdict(RECORDED[name]["response"])
    assert (result.score, result.verdict) == (score, verdict)
    assert result.rationale and result.model == RECORDED[name]["response"]["model"]
    assert result.error is None


def test_answer_text_reads_like_the_user_sees_it():
    sentence = Sentence("Necesitás tres aterrizajes.", [])
    assert answer_text(Answer([], refused=True)) == strings.REFUSAL
    assert answer_text(Answer([sentence], refused=False, incomplete=True)) == f"{strings.INCOMPLETE} Necesitás tres aterrizajes."
    assert answer_text(Answer([sentence], refused=False)) == "Necesitás tres aterrizajes."


def test_parse_rejects_responses_without_a_verdict():
    with pytest.raises(ValueError, match="no text block"):
        parse_verdict({"content": [], "stop_reason": "max_tokens"})
    with pytest.raises(ValueError, match="Unknown judge verdict"):
        parse_verdict({"content": [{"type": "text", "text": '{"fundamento": "x", "veredicto": "quizás"}'}]})


def test_failed_judge_call_is_recorded_not_raised(answers):
    result = CorrectnessJudge(_StubClient({}, fail=True), MODEL).judge("q", "ref", answers["rechazo"])
    assert result.score is None and result.verdict is None
    assert result.error == "RuntimeError: overloaded"


def test_judge_model_is_configuration(monkeypatch):
    monkeypatch.delenv("RAAC_MODEL_JUDGE", raising=False)
    assert load_judge_model() == "claude-opus-5-5"
    monkeypatch.setenv("RAAC_MODEL_JUDGE", "claude-sonnet-5-5")
    assert load_judge_model() == "claude-sonnet-5-5"


# Through the harness


class _FakePipeline:
    """Canned results in case order (several cases share a question here)."""

    name = "fake"

    def __init__(self, results):
        self._results = list(results)

    def models(self):
        return {"answer": "fake-model"}

    def index_versions(self):
        return [IndexVersion("61", "abc123")]

    def run(self, standalone_question):
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def judged_report(answers, cases, tmp_path):
    vfr, fuera = cases["61-vfr-nocturno-ppl"], cases["fuera-notam-aeroparque"]
    rows = [
        dict(id="bien", answer=answers["correcta"]),
        dict(id="mal", answer=answers["incorrecta"]),
        dict(id="fuera", answer=answers["rechazo"], case=fuera),
        dict(id="juez-caido", answer=Answer([Sentence("Otra cosa.", [])], refused=False)),
        dict(id="roto", answer=RuntimeError("boom")),
    ]
    lines, results = [], []
    for row in rows:
        source = row.get("case", vfr)
        lines.append(
            json.dumps(
                {
                    "id": row["id"],
                    "question": source.question,
                    "expected_secciones": [{"parte": r.parte, "seccion": r.seccion} for r in source.expected_secciones],
                    "reference_answer": source.reference_answer,
                    "out_of_scope": source.out_of_scope,
                },
                ensure_ascii=False,
            )
        )
        ans = row["answer"]
        results.append(ans if isinstance(ans, Exception) else PipelineResult(["61"], [SeccionRef("61", "61.535")], ans))
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(lines) + "\n")
    client = _recorded_client()  # "juez-caido" is not recorded, so its judge call fails
    report = run_eval(_FakePipeline(results), load_cases(path), judge=CorrectnessJudge(client, MODEL))
    return report, client


def test_report_has_correctness_and_rationale_per_case(judged_report):
    report, _ = judged_report
    by_id = {c["id"]: c for c in report["cases"]}
    assert by_id["bien"]["scores"]["correctness"] == 1.0
    assert by_id["mal"]["scores"]["correctness"] == 0.0
    assert by_id["fuera"]["scores"]["correctness"] == 1.0
    assert by_id["mal"]["judge"]["verdict"] == "incorrecta" and "contradice" in by_id["mal"]["judge"]["rationale"]
    assert by_id["juez-caido"]["scores"]["correctness"] is None
    assert by_id["juez-caido"]["judge"]["error"].startswith("KeyError")
    assert by_id["roto"]["scores"] is None and by_id["roto"]["judge"] is None


def test_correctness_is_separate_from_retrieval_and_grounding(judged_report):
    report, _ = judged_report
    by_id = {c["id"]: c for c in report["cases"]}
    # Same retrieval and grounding as "bien", opposite correctness.
    assert by_id["mal"]["scores"]["retrieval_hit"] is True and by_id["mal"]["scores"]["grounding"] == 1.0
    assert by_id["mal"]["scores"]["correctness"] == 0.0


def test_aggregate_correctness_and_judge_in_report(judged_report):
    report, client = judged_report
    agg = report["aggregate"]
    # bien 1, mal 0, fuera 1, roto (pipeline error) 0; juez-caido left out and counted.
    assert agg["correctness_mean"] == pytest.approx(2 / 4)
    assert agg["judge_errors"] == 1
    assert report["judge"] == {"model": MODEL, "served_models": [RECORDED["correcta"]["response"]["model"]]}
    assert len(client.requests) == 4  # no judge call for the errored case
    text = summarize(report)
    assert "correctness_mean: 0.50" in text
    assert "mal: incorrecta - " in text
    assert "judge: KeyError" in text


def test_without_a_judge_there_is_no_correctness(answers, cases, tmp_path):
    vfr = cases["61-vfr-nocturno-ppl"]
    pipeline = _FakePipeline([PipelineResult(["61"], [SeccionRef("61", "61.535")], answers["correcta"])])
    report = run_eval(pipeline, [vfr])
    assert report["judge"] is None
    assert "correctness_mean" not in report["aggregate"]
    assert report["cases"][0]["scores"]["correctness"] is None
