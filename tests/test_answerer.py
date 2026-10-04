"""Answerer citation mapping against a live-recorded Citations API response (no live LLM call).

The recording comes from `raac ask "<question>" --record tests/fixtures/citations-response-61535.json`
and stores the Secciones in the order they were sent as documents, so the replay rebuilds the
exact documents the API's citation indices point into. The live PDF it was recorded against is
byte-identical to tests/fixtures/raac-parte-61.pdf.
"""

import json
import re

import pytest

from conftest import FIXTURES
from raac.answerer import build_documents, map_response
from raac.cli import render
from raac.retriever import Retrieval, RetrievedSeccion

URL = "https://docs.anac.gob.ar/index.php/s/PtMG8j8sFeRyren"


@pytest.fixture(scope="module")
def record():
    return json.loads((FIXTURES / "citations-response-61535.json").read_text())


@pytest.fixture
def response(record):
    return record["response"]


@pytest.fixture
def retrieval(record, parte61):
    assert {s["parte"] for s in record["secciones"]} == {"61"}
    return Retrieval(
        routed_partes=["61"],
        secciones=[RetrievedSeccion(parte61, parte61.seccion(s["seccion"])) for s in record["secciones"]],
    )


@pytest.fixture
def two_secciones(parte61):
    return Retrieval(
        routed_partes=["61"],
        secciones=[RetrievedSeccion(parte61, parte61.seccion(i)) for i in ("61.520", "61.535")],
    )


def test_documents_are_one_per_seccion_with_one_block_per_pdf_page(two_secciones):
    docs = build_documents(two_secciones)
    assert [d["title"] for d in docs] == [
        "RAAC Parte 61 - Sección 61.520 Experiencia de vuelo",
        "RAAC Parte 61 - Sección 61.535 Operaciones VFR nocturnas - Régimen transitorio",
    ]
    assert len(docs[0]["source"]["content"]) == 3
    assert all(d["citations"] == {"enabled": True} for d in docs)


def test_recording_is_the_61535_question_answered_by_the_live_api(record, response):
    assert record["question"] == "¿Puedo volar VFR de noche con mi licencia de piloto privado?"
    assert response["stop_reason"] == "end_turn"
    assert {"61.520", "61.535"} <= {s["seccion"] for s in record["secciones"]}


def test_citation_joined_with_parsed_parte(retrieval, response, parte61):
    answer = map_response(response, retrieval, {"61": URL})
    assert not answer.refused
    cites = [c for s in answer.sentences for c in s.citations if c.seccion == "61.535"]
    assert cites, "the Answer must cite Sección 61.535"
    for c in cites:
        assert (c.parte, c.pdf_page_start, c.pdf_page_end) == ("61", 67, 67)
        assert (c.printed_page_start, c.printed_page_end) == ("10", "10")
        assert (c.edicion, c.enmienda, c.fecha, c.source_url) == ("VI", "I", "mayo 2026", URL)
        assert c.seccion_title == "Operaciones VFR nocturnas - Régimen transitorio"
        # cited_text comes verbatim from the source, never from model prose.
        assert c.cited_text.strip() in parte61.seccion("61.535").pages[0].text


def test_every_sentence_has_a_citation_and_no_citation_is_shared_across_sentences(retrieval, response):
    answer = map_response(response, retrieval, {"61": URL})
    assert len(answer.sentences) >= 2
    assert all(s.citations for s in answer.sentences)
    cited_spans = [
        block["text"] for block in response["content"] if block.get("type") == "text" and block.get("citations")
    ]
    # Each cited span lands in exactly one sentence (possibly as its capitalized start), and nothing
    # is invented around it.
    for span in cited_spans:
        span = span.strip()
        capitalized = span[:1].upper() + span[1:]
        assert sum(span in s.text or s.text.startswith(capitalized) for s in answer.sentences) == 1, span
    # Every kept sentence starts with a capital letter, including the one whose lead-in
    # ("Hay un régimen transitorio:") was dropped as uncited.
    for sentence in answer.sentences:
        first = next(ch for ch in sentence.text if ch.isalpha())
        assert first.isupper(), sentence.text
    assert any(s.text.startswith("Hasta el 31 de diciembre de 2027") for s in answer.sentences)
    all_text = " ".join(s.text for s in answer.sentences)
    for dropped in answer.dropped_uncited:
        assert dropped not in all_text


def test_render_marks_every_sentence_and_prints_citation_fields(retrieval, response):
    answer = map_response(response, retrieval, {"61": URL})
    out = render(answer)
    body = out.split("\n\n")[1]
    for sentence in answer.sentences:
        assert re.search(re.escape(sentence.text) + r" (\[\d+\])+", body), sentence.text
    assert re.search(
        r"\[\d+\] Parte 61, Sección 61\.535 \(Operaciones VFR nocturnas - Régimen transitorio\), "
        r"página PDF 67 \(página impresa 10\), Edición VI Enmienda I \(mayo 2026\) - " + re.escape(URL),
        out,
    )
    for dropped in answer.dropped_uncited:
        assert dropped not in out


# The API's real shape: each cited span is its own block, and sentence ends live in the uncited
# blocks around it. These pin the splitting rules the live recording exercises.


def _cite(document_index: int, block: int, text: str = "x") -> dict:
    return {
        "type": "content_block_location",
        "cited_text": text,
        "document_index": document_index,
        "start_block_index": block,
        "end_block_index": block + 1,
    }


def _response(*blocks: tuple[str, list]) -> dict:
    return {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": t, "citations": c or None} for t, c in blocks],
    }


def test_uncited_sentence_between_cited_spans_is_dropped_not_merged(two_secciones):
    answer = map_response(
        _response(
            ("Si ", []),
            ("no tenés instrucción nocturna, no volás VFR de noche", [_cite(1, 0)]),
            (". Esto vale en todo el mundo. Además, ", []),
            ("necesitás constancia del instructor", [_cite(1, 0)]),
            (".", []),
        ),
        two_secciones,
        {"61": URL},
    )
    assert [s.text for s in answer.sentences] == [
        "Si no tenés instrucción nocturna, no volás VFR de noche.",
        "Además, necesitás constancia del instructor.",
    ]
    assert answer.dropped_uncited == ["Esto vale en todo el mundo."]


def test_uncited_text_after_a_trailing_period_block_is_not_merged(two_secciones):
    answer = map_response(
        _response(("Hace falta instrucción nocturna", [_cite(1, 0)]), (".", []), (" Y un buen avión.", [])),
        two_secciones,
        {"61": URL},
    )
    assert [s.text for s in answer.sentences] == ["Hace falta instrucción nocturna."]
    assert answer.dropped_uncited == ["Y un buen avión."]


def test_uncited_sentence_after_a_cited_span_ending_in_a_period_is_dropped(two_secciones):
    answer = map_response(
        _response(
            ("Hace falta instrucción nocturna.", [_cite(1, 0)]),
            (" En ese caso la licencia lleva otra leyenda, ", []),
            ("pudiendo levantarla", [_cite(1, 0)]),
            (" más adelante.\n\nY un buen avión.", []),
        ),
        two_secciones,
        {"61": URL},
    )
    assert [s.text for s in answer.sentences] == [
        "Hace falta instrucción nocturna.",
        "En ese caso la licencia lleva otra leyenda, pudiendo levantarla más adelante.",
    ]
    assert answer.dropped_uncited == ["Y un buen avión."]


def test_section_numbers_and_cited_punctuation_do_not_split(two_secciones):
    answer = map_response(
        _response(
            ("Según la Sección 61.520 (a)(1)(v), ", []),
            ("son tres horas. Con diez aterrizajes", [_cite(0, 0)]),
            (".", []),
        ),
        two_secciones,
        {"61": URL},
    )
    assert [s.text for s in answer.sentences] == [
        "Según la Sección 61.520 (a)(1)(v), son tres horas. Con diez aterrizajes."
    ]


def test_markdown_headings_and_list_numbers_are_not_sentences(two_secciones):
    answer = map_response(
        _response(
            ("**Depende.** Hay dos condiciones:\n\n1. ", []),
            ("tener instrucción", [_cite(1, 0)]),
            (".\n2. ", []),
            ("tener constancia", [_cite(1, 0)]),
            (".", []),
        ),
        two_secciones,
        {"61": URL},
    )
    assert [s.text for s in answer.sentences] == ["Tener instrucción.", "Tener constancia."]
    assert answer.dropped_uncited == ["Depende.", "Hay dos condiciones:"]


def test_kept_sentences_start_with_a_capital_letter_but_dropped_text_is_untouched(two_secciones):
    answer = map_response(
        _response(
            ("Hay un régimen transitorio: ", []),
            ('"hasta 2027', [_cite(1, 0)]),
            (' rige el reemplazo". más datos sueltos.', []),
        ),
        two_secciones,
        {"61": URL},
    )
    assert [s.text for s in answer.sentences] == ['"Hasta 2027 rige el reemplazo".']
    assert answer.dropped_uncited == ["Hay un régimen transitorio:", "más datos sueltos."]


def test_no_citations_means_refusal(two_secciones):
    response = {"stop_reason": "end_turn", "content": [{"type": "text", "text": "No sé.", "citations": None}]}
    answer = map_response(response, two_secciones, {"61": URL})
    assert answer.refused and not answer.sentences


def test_api_refusal_stop_reason_is_a_refusal(two_secciones):
    answer = map_response({"stop_reason": "refusal", "content": []}, two_secciones, {"61": URL})
    assert answer.refused


def test_citation_shows_the_cited_pages_own_footer(parte26):
    # ADR 0004: Parte 26 is I Edición by most footers, but 26.001's page prints "4º Edición".
    retrieval = Retrieval(routed_partes=["26"], secciones=[RetrievedSeccion(parte26, parte26.seccion("26.001"))])
    assert build_documents(retrieval)[0]["context"] == "Edición IV, 23 marzo 2022"
    raw = {
        "type": "content_block_location",
        "document_index": 0,
        "start_block_index": 0,
        "end_block_index": 1,
        "cited_text": "Definición.",
    }
    response = {"content": [{"type": "text", "text": "Hay una definición.", "citations": [raw]}], "model": "m"}
    c = map_response(response, retrieval, {"26": URL}).sentences[0].citations[0]
    assert (parte26.edicion, c.edicion, c.enmienda, c.fecha) == ("I", "IV", None, "23 marzo 2022")
