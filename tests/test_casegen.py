"""Generated eval cases and the review flow, replaying recorded responses (no live LLM calls)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from raac import casegen
from raac.config import load_tool_model
from raac.evals import load_cases
from raac.parser import ParsedParte

FIXTURES = Path(__file__).parent / "fixtures"
# Recorded live with `claude-opus-5-5`, seed 11: Partes 61 (3 Secciones) and 67 (2), skipping
# the Secciones evals/cases.jsonl covered then. Pinned, so a new eval case doesn't change the sample.
RECORDED = json.loads((FIXTURES / "casegen-responses.json").read_text())
RECORDED_EXCLUDE = {"61": {"61.140", "61.160", "61.410", "61.535", "61.635"}, "67": set()}
CASES = Path(__file__).parent.parent / "evals" / "cases.jsonl"


class ReplayMessages:
    """Answers each request with the next recorded response for the Sección it asks about."""

    def __init__(self, responses: dict[str, list[dict]]):
        self.responses = {k: list(v) for k, v in responses.items()}
        self.requests: list[dict] = []

    def create(self, **request):
        self.requests.append(request)
        seccion = request["messages"][0]["content"].split('id="')[1].split('"')[0]
        raw = self.responses[seccion].pop(0)
        return SimpleNamespace(to_dict=lambda: raw)


def replay_client(responses: dict[str, list[dict]]) -> SimpleNamespace:
    return SimpleNamespace(messages=ReplayMessages(responses))


def recorded_by_seccion() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in RECORDED:
        out.setdefault(r["seccion"], []).append(r["response"])
    return out


def text_response(question: str, answer: str = "Respuesta.") -> dict:
    return {
        "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": json.dumps({"pregunta": question, "respuesta_referencia": answer})}],
        "stop_reason": "end_turn",
    }


def test_sample_skips_reserved_and_excluded_secciones_and_is_repeatable(parte61: ParsedParte):
    exclude = {"61.535", "61.160"}
    sample = casegen.sample_secciones(parte61, 20, exclude, seed=3)
    assert len(sample) == 20
    assert all(s.title != "Reservado" and s.id not in exclude for s in sample)
    assert [s.id for s in sample] == [s.id for s in casegen.sample_secciones(parte61, 20, exclude, seed=3)]
    pdf_order = [s.id for s in parte61.secciones]
    assert [pdf_order.index(s.id) for s in sample] == sorted(pdf_order.index(s.id) for s in sample)
    assert not casegen.eligible(parte61.seccion("61.030"))  # "Reservado"
    assert casegen.eligible(parte61.seccion("61.160"))  # short, but a real rule


def test_sample_never_exceeds_the_pool(parte67: ParsedParte):
    assert len(casegen.sample_secciones(parte67, 1000, set(), seed=1)) == len(parte67.secciones)


def test_request_asks_for_user_phrasing_with_a_json_schema(parte61: ParsedParte):
    req = casegen.build_request("m", "61", parte61.seccion("61.535"))
    assert req["model"] == "m"
    assert "No copies frases del texto regulatorio" in req["system"]
    assert 'id="61.535"' in req["messages"][0]["content"]
    assert req["output_config"]["format"]["schema"]["required"] == ["pregunta", "respuesta_referencia"]


def test_request_truncates_very_long_secciones(parte1: ParsedParte):
    longest = max(parte1.secciones, key=lambda s: len(s.text))
    content = casegen.build_request("m", "1", longest)["messages"][0]["content"]
    assert len(content) < 13_000 < len(longest.text)


def test_copied_wording_is_detected(parte61: ParsedParte):
    s = parte61.seccion("61.160")
    copied = "¿Es cierto que el titular de una licencia que haya realizado un cambio de domicilio debe avisar?"
    assert casegen.copies_wording(copied, s) is not None
    assert casegen.copies_wording("¿Qué dice la 61.160 sobre mudarse?", s) is not None
    assert casegen.copies_wording("Me mudé, ¿tengo que avisarle a la ANAC?", s) is None


def test_recorded_candidates_are_phrased_as_users_ask(parte61, parte67):
    client = replay_client(recorded_by_seccion())
    candidates = []
    for parte, n in ((parte61, 3), (parte67, 2)):
        new, discarded = casegen.generate(client, "claude-opus-5-5", parte, n, RECORDED_EXCLUDE[parte.code], seed=11)
        assert discarded == []
        candidates += new
    assert [c.id for c in candidates] == [f"gen-{r['seccion'].split('.')[0]}-{r['seccion']}" for r in RECORDED]
    for c in candidates:
        parte = parte61 if c.parte == "61" else parte67
        assert casegen.copies_wording(c.question, parte.seccion(c.seccion)) is None
        assert c.seccion_text == parte.seccion(c.seccion).text
        assert c.reference_answer and c.status == casegen.PENDING
        assert c.to_case()["source"] == "generated"


def test_copied_question_is_retried_with_feedback_then_kept(parte61: ParsedParte):
    copied = "¿Es cierto que el titular de una licencia que haya realizado un cambio de domicilio debe avisar?"
    client = replay_client({"61.160": [text_response(copied), text_response("Me mudé, ¿aviso a la ANAC?")]})
    candidates, discarded = casegen.generate(client, "m", parte61, 1, {s.id for s in parte61.secciones} - {"61.160"})
    assert [c.question for c in candidates] == ["Me mudé, ¿aviso a la ANAC?"]
    assert discarded == []
    retry = client.messages.requests[1]["messages"]
    assert [m["role"] for m in retry] == ["user", "assistant", "user"]
    assert "copia" in retry[2]["content"]


def test_question_that_keeps_copying_is_discarded(parte61: ParsedParte):
    copied = "¿Es cierto que el titular de una licencia que haya realizado un cambio de domicilio debe avisar?"
    client = replay_client({"61.160": [text_response(copied), text_response(copied)]})
    candidates, discarded = casegen.generate(client, "m", parte61, 1, {s.id for s in parte61.secciones} - {"61.160"})
    assert candidates == []
    assert discarded and discarded[0].startswith("61:61.160: copia")


def test_covered_secciones_counts_cases_and_candidates_of_any_status(tmp_path: Path):
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps({"id": "x", "expected_secciones": [{"parte": "61", "seccion": "61.535"}, {"parte": "91", "seccion": "91.1"}]}) + "\n")
    rejected = candidate("61.900", status=casegen.REJECTED)
    assert casegen.covered_secciones(cases, [rejected], "61") == {"61.535", "61.900"}


def candidate(seccion: str, status: str = casegen.PENDING) -> casegen.Candidate:
    return casegen.Candidate(
        id=f"gen-61-{seccion}",
        parte="61",
        seccion=seccion,
        seccion_title="Título",
        seccion_text="Texto de la Sección.",
        question=f"¿Pregunta sobre {seccion}?",
        reference_answer=f"Respuesta sobre {seccion}.",
        model="m",
        status=status,
    )


def scripted(answers: list[str]):
    it = iter(answers)
    return lambda _prompt: next(it)


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    cases = tmp_path / "cases.jsonl"
    cases.write_text(CASES.read_text(encoding="utf-8"), encoding="utf-8")
    cands = tmp_path / "candidates.jsonl"
    casegen.save_candidates([candidate(s) for s in ("61.1", "61.2", "61.3", "61.4")], cands)
    return cands, cases


def test_review_accepts_edits_rejects_and_skips(files):
    cands, cases = files
    shown: list[str] = []
    counts = casegen.review(
        cands, cases, read=scripted(["a", "x", "e", "¿Pregunta editada?", "", "r", "s"]), write=shown.append
    )
    assert counts == {casegen.ACCEPTED: 2, casegen.REJECTED: 1, "saltado": 1}
    statuses = {c.seccion: c.status for c in casegen.load_candidates(cands)}
    assert statuses == {"61.1": "aceptado", "61.2": "aceptado", "61.3": "rechazado", "61.4": "pendiente"}

    loaded = load_cases(cases)  # the eval set still loads with the accepted cases in it
    generated = [c for c in loaded if c.source == "generated"]
    assert [(c.id, c.question, c.reference_answer) for c in generated] == [
        ("gen-61-61.1", "¿Pregunta sobre 61.1?", "Respuesta sobre 61.1."),
        ("gen-61-61.2", "¿Pregunta editada?", "Respuesta sobre 61.2."),  # empty answer keeps the reference
    ]
    assert [r.seccion for r in generated[0].expected_secciones] == ["61.1"]
    assert all(c.source == "owner" for c in loaded if not c.id.startswith("gen-"))
    card = shown[0]
    assert "¿Pregunta sobre 61.1?" in card and "Texto de la Sección." in card and "Respuesta sobre 61.1." in card


def test_review_quit_keeps_decisions_and_resumes_with_pending(files):
    cands, cases = files
    casegen.review(cands, cases, read=scripted(["r", "q"]), write=lambda _s: None)
    assert [c.status for c in casegen.load_candidates(cands)] == ["rechazado", "pendiente", "pendiente", "pendiente"]
    shown: list[str] = []
    casegen.review(cands, cases, read=scripted(["q"]), write=shown.append)
    assert "gen-61-61.2" in shown[0]


def test_accept_refuses_an_existing_case_id(files):
    _cands, cases = files
    casegen.accept(candidate("61.1"), cases)
    with pytest.raises(casegen.CandidateError):
        casegen.accept(candidate("61.1"), cases)


def test_committed_candidates_load():
    candidates = casegen.load_candidates(Path(__file__).parent.parent / "evals" / "candidates.jsonl")
    assert candidates and all(c.id.startswith("gen-") for c in candidates)


def test_malformed_candidate_is_rejected(tmp_path: Path):
    path = tmp_path / "c.jsonl"
    path.write_text('{"id": "x"}\n')
    with pytest.raises(casegen.CandidateError):
        casegen.load_candidates(path)


def test_casegen_model_is_configuration(monkeypatch):
    monkeypatch.delenv("RAAC_MODEL_CASEGEN", raising=False)
    assert load_tool_model("casegen") == "claude-opus-5-5"
    monkeypatch.setenv("RAAC_MODEL_CASEGEN", "claude-sonnet-5-5")
    assert load_tool_model("casegen") == "claude-sonnet-5-5"
