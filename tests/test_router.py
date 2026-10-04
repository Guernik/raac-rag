"""Parte routing and routed retrieval, with recorded-shape responses (no live LLM calls)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from raac.indexer import ParteIndex, attach_secciones
from raac.retriever import Retriever
from raac.router import ParteCard, ParteRouter, build_request, routed_partes, routing_report

ROUTING_CASES = Path(__file__).parent.parent / "evals" / "routing-cases.jsonl"

CARDS = [
    ParteCard("1", "Definiciones generales, abreviaturas y siglas", "Define los términos usados en las RAAC.", ["SUBPARTE A", "SUBPARTE B"]),
    ParteCard("61", "Licencias, Certificado de Competencia y Habilitaciones para piloto", "Requisitos para licencias de piloto.", ["CAPÍTULO A"]),
    ParteCard("67", "Certificación médica aeronáutica", "Requisitos psicofísicos y certificados médicos.", ["Capítulo A"]),
    ParteCard("91", "Reglas de vuelo y operación general", "Reglas de vuelo VFR e IFR y operación.", ["SUBPARTE B"]),
]


def response(text: str) -> dict:
    # Shape of a Messages API response with output_config.format json_schema.
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 900, "output_tokens": 12},
    }


class FakeMessages:
    def __init__(self, text: str):
        self.text = text
        self.requests: list[dict] = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(to_dict=lambda: response(self.text))


def fake_claude(text: str) -> SimpleNamespace:
    return SimpleNamespace(messages=FakeMessages(text))


def test_request_constrains_answer_to_known_partes():
    req = build_request("claude-haiku-4-5", "¿Puedo volar VFR de noche con PPL?", CARDS)
    schema = req["output_config"]["format"]["schema"]
    assert schema["properties"]["partes"]["items"]["enum"] == ["1", "61", "67", "91"]
    assert req["model"] == "claude-haiku-4-5"
    prompt = req["messages"][0]["content"]
    assert "Certificación médica aeronáutica" in prompt and "Reglas de vuelo VFR e IFR" in prompt
    assert prompt.endswith("Pregunta: ¿Puedo volar VFR de noche con PPL?")


def test_routed_partes_keeps_order_and_drops_unknown_and_repeated():
    assert routed_partes(response('{"partes": ["91", "61", "91", "135"]}'), CARDS) == ["91", "61"]
    assert routed_partes(response('{"partes": []}'), CARDS) == []


def test_response_without_text_raises():
    with pytest.raises(ValueError, match="no text block"):
        routed_partes({"content": [], "stop_reason": "refusal"}, CARDS)


def test_router_records_raw_response(tmp_path):
    claude = fake_claude('{"partes": ["61", "67"]}')
    record = tmp_path / "routing.json"
    router = ParteRouter(claude, "claude-haiku-4-5", CARDS, record_path=record)
    assert router.route("¿Qué apto médico necesita un alumno piloto?") == ["61", "67"]
    saved = json.loads(record.read_text())
    assert saved["question"] == "¿Qué apto médico necesita un alumno piloto?"
    assert saved["response"]["content"][0]["text"] == '{"partes": ["61", "67"]}'


def test_single_parte_needs_no_routing_call():
    claude = fake_claude("unused")
    assert ParteRouter(claude, "m", CARDS[1:2]).route("x") == ["61"]
    assert claude.messages.requests == []


class FakePageIndex:
    """Replays a PageIndex agent that reads one page per document."""

    def __init__(self, pages_by_doc: dict[str, str]):
        self.pages_by_doc = pages_by_doc
        self.searched: list[str] = []

    def chat(self, question, doc_id, stream, instructions):
        self.searched.append(doc_id)
        events = [{"type": "tool_call", "name": "get_page_content", "arguments": {"pages": self.pages_by_doc[doc_id]}}]
        return SimpleNamespace(events=events)


def _index(parsed, pages: int) -> ParteIndex:
    tree = [{"title": "Todo", "node_id": "0000", "start_index": 1, "end_index": pages}]
    return ParteIndex(parsed.code, parsed.content_hash, f"doc-{parsed.code}", attach_secciones(tree, parsed))


def test_retriever_searches_only_routed_partes(parte61, parte67, parte91_sample):
    partes = [(p, _index(p, p.page_count)) for p in (parte61, parte67, parte91_sample)]
    pageindex = FakePageIndex({"doc-61": "67", "doc-67": "13", "doc-91": "6"})
    router = SimpleNamespace(route=lambda q: ["61", "91", "135"])  # 135 is not loaded
    progress = []
    r = Retriever(pageindex, partes, router=router, on_progress=progress.append).retrieve("¿VFR nocturno con PPL?")
    assert r.routed_partes == ["61", "91"]
    assert pageindex.searched == ["doc-61", "doc-91"]
    assert r.visited_nodes == ["61:0000", "91:0000"]
    ids = [f"{s.parte.code}:{s.seccion.id}" for s in r.secciones]
    assert "61:61.535" in ids and "91:91.151" in ids
    assert not any(i.startswith("67:") for i in ids)
    assert progress[:2] == ["Eligiendo en qué Partes buscar...", "Buscando en: 61, 91"]


def test_retriever_without_router_searches_every_parte(parte61, parte67):
    partes = [(p, _index(p, p.page_count)) for p in (parte61, parte67)]
    pageindex = FakePageIndex({"doc-61": "67", "doc-67": "13"})
    r = Retriever(pageindex, partes).retrieve("x")
    assert r.routed_partes == ["61", "67"]


def load_routing_cases() -> list[dict]:
    return [json.loads(line) for line in ROUTING_CASES.read_text().splitlines() if line.strip()]


def test_routing_cases_cover_61_vs_91_ambiguity():
    expected = [{s["parte"] for s in c["expected_secciones"]} for c in load_routing_cases()]
    assert {"61"} in expected and {"91"} in expected and {"61", "91"} in expected
    assert {"67"} in expected and {"1"} in expected and {"61", "67"} in expected


def test_routing_case_secciones_exist(parte1, parte61, parte67, parte91_sample):
    parsed = {p.code: p for p in (parte1, parte61, parte67, parte91_sample)}
    for case in load_routing_cases():
        assert set(case) >= {"id", "question", "expected_secciones", "reference_answer", "out_of_scope"}
        for s in case["expected_secciones"]:
            parsed[s["parte"]].seccion(s["seccion"])  # raises KeyError if missing


def test_routing_report_scores_per_case_and_per_parte():
    cases = [
        {"id": "a", "question": "q1", "expected_secciones": [{"parte": "61", "seccion": "61.535"}, {"parte": "91", "seccion": "91.151"}]},
        {"id": "b", "question": "q2", "expected_secciones": [{"parte": "91", "seccion": "91.155"}]},
    ]
    answers = {"q1": ["61"], "q2": ["91", "1"]}
    router = SimpleNamespace(route=answers.__getitem__, model="claude-haiku-4-5")
    report = routing_report(router, cases)
    assert [c["hit"] for c in report["cases"]] == [False, True]
    assert report["cases"][1]["extra"] == ["1"]
    assert report["accuracy"] == 0.5 and report["exact"] == 0.0
    assert report["recall_per_parte"] == {"61": 1.0, "91": 0.5}
    assert report["model"] == "claude-haiku-4-5"
