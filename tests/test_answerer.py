"""Answerer citation mapping against a recorded Citations API response (no live LLM call)."""

import json

import pytest

from conftest import FIXTURES
from raac.answerer import build_documents, map_response
from raac.cli import render
from raac.retriever import Retrieval, RetrievedSeccion

URL = "https://docs.anac.gob.ar/index.php/s/PtMG8j8sFeRyren"


@pytest.fixture
def retrieval(parte61):
    return Retrieval(
        routed_partes=["61"],
        secciones=[RetrievedSeccion(parte61, parte61.seccion(i)) for i in ("61.520", "61.535")],
    )


@pytest.fixture
def response():
    return json.loads((FIXTURES / "citations-response-61535.json").read_text())


def test_documents_are_one_per_seccion_with_one_block_per_pdf_page(retrieval):
    docs = build_documents(retrieval)
    assert [d["title"] for d in docs] == [
        "RAAC Parte 61 - Sección 61.520 Experiencia de vuelo",
        "RAAC Parte 61 - Sección 61.535 Operaciones VFR nocturnas - Régimen transitorio",
    ]
    assert len(docs[0]["source"]["content"]) == 3
    assert all(d["citations"] == {"enabled": True} for d in docs)


def test_citation_joined_with_parsed_parte(retrieval, response, parte61):
    answer = map_response(response, retrieval, {"61": URL})
    assert not answer.refused
    c = answer.sentences[0].citations[0]
    assert (c.parte, c.seccion, c.pdf_page_start, c.printed_page_start) == ("61", "61.535", 67, "10")
    assert (c.edicion, c.enmienda, c.source_url) == ("VI", "I", URL)
    assert c.cited_text == parte61.seccion("61.535").pages[0].text


def test_every_sentence_has_a_citation_and_uncited_text_is_dropped(retrieval, response):
    answer = map_response(response, retrieval, {"61": URL})
    assert len(answer.sentences) == 2
    assert all(s.citations for s in answer.sentences)
    # "Depende de tu instrucción." and the closing claim carry no Citation.
    assert answer.dropped_uncited == [
        "Depende de tu instrucción.",
        "Esto vale para cualquier aeródromo del mundo.",
    ]


def test_no_citations_means_refusal(retrieval):
    response = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "No sé.", "citations": None}]}
    answer = map_response(response, retrieval, {"61": URL})
    assert answer.refused and not answer.sentences


def test_api_refusal_stop_reason_is_a_refusal(retrieval):
    answer = map_response({"stop_reason": "refusal", "content": []}, retrieval, {"61": URL})
    assert answer.refused


def test_render_prints_citation_fields(retrieval, response):
    out = render(map_response(response, retrieval, {"61": URL}))
    assert (
        "[1] Parte 61, Sección 61.535 (Operaciones VFR nocturnas - Régimen transitorio), "
        "página PDF 67 (página impresa 10), Edición VI Enmienda I - " + URL
    ) in out
    assert "aeródromo del mundo" not in out
