"""Answerer citation mapping against a live-recorded Citations API response (no live LLM call).

The recording comes from `raac ask "<question>" --record tests/fixtures/citations-response-61535.json`
and stores the Secciones in the order they were sent as documents, so the replay rebuilds the
exact documents the API's citation indices point into. The live PDF it was recorded against is
byte-identical to tests/fixtures/raac-parte-61.pdf.
"""

import json
import re
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from conftest import FIXTURES
from raac.answerer import answer, build_documents, map_response
from raac.cli import render
from raac.retriever import Retrieval, RetrievedDefinicion, RetrievedSeccion

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


def test_live_answer_opens_with_a_verdict_and_lists_the_conditions(retrieval, response):
    answer = map_response(response, retrieval, {"61": URL})
    assert (answer.coverage, answer.refused, answer.incomplete, answer.dropped) == ("total", False, False, [])
    verdict = answer.sentences[0]
    assert verdict.text.startswith("Depende") and verdict.citations == [] and verdict.starts is None
    items = [s for s in answer.sentences if s.starts == "item"]
    assert len(items) == 2 and all(s.citations for s in items)
    # The lead-in holding the list together comes right before it.
    lead_in = answer.sentences[answer.sentences.index(items[0]) - 1]
    assert lead_in.text.endswith("condiciones:") and lead_in.citations
    assert any(s.text.startswith("Hasta el 31 de diciembre de 2027") and s.citations for s in answer.sentences)


def test_each_cited_span_lands_in_exactly_one_sentence(retrieval, response):
    answer = map_response(response, retrieval, {"61": URL})
    cited_spans = [b["text"] for b in response["content"] if b.get("type") == "text" and b.get("citations")]
    for span in cited_spans:
        span = span.strip()
        capitalized = span[:1].upper() + span[1:]
        assert sum(span in s.text or s.text.startswith(capitalized) for s in answer.sentences) == 1, span


def test_render_marks_cited_sentences_and_lays_out_the_list(retrieval, response):
    answer = map_response(response, retrieval, {"61": URL})
    out = render(answer)
    body = out.split("\n\n")[1]
    for sentence in answer.sentences:
        marks = r" (\[\d+\])+" if sentence.citations else r"(?! \[)"
        prefix = "- " if sentence.starts == "item" else ""
        assert re.search(re.escape(prefix + sentence.text) + marks, body), sentence.text
    assert body.startswith("Depende")
    assert re.search(
        r"\[\d+\] Parte 61, Sección 61\.535 \(Operaciones VFR nocturnas - Régimen transitorio\), "
        r"página PDF 67 \(página impresa 10\), Edición VI Enmienda I \(mayo 2026\) - " + re.escape(URL),
        out,
    )


# The API's real shape: each cited span is its own block, and sentence ends live in the uncited
# blocks around it. These pin the splitting, Framing and layout rules the live recording exercises.

# Verbatim page text, so numbers in the model's prose can be checked against what was cited.
TEXT_520 = "Tres (3) horas de instrucción en vuelo nocturno, que incluya: diez (10) despegues y diez (10) aterrizajes"
TEXT_535 = "hasta el 31 de diciembre de 2027, los postulantes a la Licencia de Piloto Privado"


def _cite(document_index: int, block: int, text: str = "x") -> dict:
    return {
        "type": "content_block_location",
        "cited_text": text,
        "document_index": document_index,
        "start_block_index": block,
        "end_block_index": block + 1,
    }


def _response(*blocks: tuple[str, list], coverage: str | None = "total") -> dict:
    """A response opening with the coverage line `COBERTURA: <coverage>`, or with none when None."""
    lines = [] if coverage is None else [(f"COBERTURA: {coverage}\n\n", [])]
    return {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": t, "citations": c or None} for t, c in [*lines, *blocks]],
    }


def _texts(answer) -> list[tuple[str, bool]]:
    return [(s.text, bool(s.citations)) for s in answer.sentences]


def test_framing_is_kept_uncited_and_uncited_claims_are_dropped(two_secciones):
    answer = map_response(
        _response(
            ("Sí, pero depende de tu instrucción. Si ", []),
            ("no tenés instrucción nocturna, no volás VFR de noche", [_cite(1, 0, TEXT_535)]),
            (". Además necesitás 50 horas de vuelo. Y ", []),
            ("necesitás constancia del instructor", [_cite(1, 0, TEXT_535)]),
            (".", []),
        ),
        two_secciones,
        {"61": URL},
    )
    assert _texts(answer) == [
        ("Sí, pero depende de tu instrucción.", False),
        ("Si no tenés instrucción nocturna, no volás VFR de noche.", True),
        ("Y necesitás constancia del instructor.", True),
    ]
    assert answer.dropped == ["Además necesitás 50 horas de vuelo."]


@pytest.mark.parametrize(
    "claim, kept",
    [
        ("Son 3 horas.", True),  # "Tres (3)" in the cited text
        ("Son tres horas con diez aterrizajes.", True),
        ("Son cinco horas.", False),
        ("Vence en marzo.", False),  # a month the cited text does not have
        ("Rige hasta el 31 de diciembre de 2027.", True),
        ("Lo regula la Sección 61.520.", True),  # a cited Sección
        ("Lo regula la Sección 61.140.", False),
        ("Lo dice el inciso (a)(1)(v).", True),  # inciso markers are not numbers
    ],
)
def test_framing_check_matches_numbers_months_and_secciones_against_the_cited_text(two_secciones, claim, kept):
    answer = map_response(
        _response(
            ("Hace falta ", []),
            ("instrucción nocturna", [_cite(0, 0, TEXT_520), _cite(1, 0, TEXT_535)]),
            (". " + claim, []),
        ),
        two_secciones,
        {"61": URL},
    )
    assert (claim in [s.text for s in answer.sentences]) == kept
    assert (answer.dropped == [claim]) == (not kept)


def test_a_cited_sentence_with_a_number_the_cited_text_lacks_is_dropped(two_secciones):
    answer = map_response(
        _response(("Son ", []), ("cuatro horas de instrucción nocturna", [_cite(0, 0, TEXT_520)]), (".", [])),
        two_secciones,
        {"61": URL},
    )
    assert answer.refused and answer.dropped == ["Son cuatro horas de instrucción nocturna."]


def test_section_numbers_and_cited_punctuation_do_not_split(two_secciones):
    answer = map_response(
        _response(
            ("Según la Sección 61.520 (a)(1)(v), ", []),
            ("son tres horas. Con diez aterrizajes", [_cite(0, 0, TEXT_520)]),
            (".", []),
        ),
        two_secciones,
        {"61": URL},
    )
    assert [s.text for s in answer.sentences] == [
        "Según la Sección 61.520 (a)(1)(v), son tres horas. Con diez aterrizajes."
    ]


def test_a_lead_in_then_one_list_item_per_condition(two_secciones):
    answer = map_response(
        _response(
            ("**Depende.** Tenés que cumplir estas condiciones:\n\n1. ", []),
            ("tener instrucción", [_cite(1, 0)]),
            (". Sin excepciones.\n2. ", []),
            ("tener constancia", [_cite(1, 0)]),
            (".\n\nY ", []),
            ("figura en la licencia", [_cite(1, 0)]),
            (".", []),
        ),
        two_secciones,
        {"61": URL},
    )
    assert [(s.text, s.starts) for s in answer.sentences] == [
        ("Depende.", None),
        ("Tenés que cumplir estas condiciones:", None),
        ("Tener instrucción.", "item"),
        ("Sin excepciones.", None),  # continues the item
        ("Tener constancia.", "item"),
        ("Y figura en la licencia.", "paragraph"),
    ]


def test_a_dropped_claims_break_carries_over_to_the_next_sentence(two_secciones):
    answer = map_response(
        _response(
            ("Condiciones:\n- ", []),
            ("tener instrucción", [_cite(1, 0)]),
            (".\n- Volar 50 horas. ", []),
            ("Con constancia", [_cite(1, 0)]),
            (".", []),
        ),
        two_secciones,
        {"61": URL},
    )
    assert [(s.text, s.starts) for s in answer.sentences] == [
        ("Condiciones:", None),
        ("Tener instrucción.", "item"),
        ("Con constancia.", "item"),
    ]
    assert answer.dropped == ["Volar 50 horas."]


def test_capitalized_unless_it_continues_a_lead_in_on_the_same_line(two_secciones):
    answer = map_response(
        _response(
            ("Hay un régimen transitorio: ", []),
            ('"hasta 2027', [_cite(1, 0, TEXT_535)]),
            (' rige el reemplazo". más datos sueltos. Vence en 2030. ', []),
            ("y se reemplaza", [_cite(1, 0, TEXT_535)]),
            (".", []),
        ),
        two_secciones,
        {"61": URL},
    )
    assert [s.text for s in answer.sentences] == [
        "Hay un régimen transitorio:",
        '"hasta 2027 rige el reemplazo".',
        "Más datos sueltos.",
        "Y se reemplaza.",  # after a dropped claim
    ]
    assert answer.dropped == ["Vence en 2030."]


def test_framing_alone_is_a_refusal(two_secciones):
    answer = map_response(_response(("Sí, podés.", [])), two_secciones, {"61": URL})
    assert answer.refused and not answer.sentences


@pytest.mark.parametrize(
    "coverage, refused, incomplete",
    [
        ("total", False, False),
        ("parcial - falta el costo", False, True),
        ("ninguna - no hay nada", True, False),
        ("quizás", True, False),  # malformed
        (None, True, False),  # missing
    ],
)
def test_the_coverage_line_decides_refusal_and_incompleteness(two_secciones, coverage, refused, incomplete):
    answer = map_response(
        _response(("Hace falta ", []), ("instrucción nocturna", [_cite(1, 0)]), (".", []), coverage=coverage),
        two_secciones,
        {"61": URL},
    )
    assert (answer.refused, answer.incomplete) == (refused, incomplete)
    assert bool(answer.sentences) == (not refused)
    assert all("COBERTURA" not in s.text for s in answer.sentences)


def test_a_coverage_line_not_at_the_start_or_inside_a_cited_span_is_a_refusal(two_secciones):
    late = _response(("Hace falta ", []), ("instrucción", [_cite(1, 0)]), (".\nCOBERTURA: total", []), coverage=None)
    cited = _response(("COBERTURA: total", [_cite(1, 0)]), (". Hace falta ", []), ("instrucción", [_cite(1, 0)]), coverage=None)
    assert map_response(late, two_secciones, {"61": URL}).refused
    assert map_response(cited, two_secciones, {"61": URL}).refused


def test_no_citations_means_refusal(two_secciones):
    answer = map_response(_response(("No sé.", [])), two_secciones, {"61": URL})
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
    response = {**_response(("Hay una definición.", [raw])), "model": "m"}
    c = map_response(response, retrieval, {"26": URL}).sentences[0].citations[0]
    assert (parte26.edicion, c.edicion, c.enmienda, c.fecha) == ("I", "IV", None, "23 marzo 2022")


# Parte 1 Definiciones attached as context follow the Secciones as documents.

URL1 = "https://docs.anac.gob.ar/index.php/s/parte1"


@pytest.fixture
def with_definiciones(parte1, parte61):
    defs = {d.term: d for d in parte1.definiciones}
    return Retrieval(
        routed_partes=["61"],
        secciones=[RetrievedSeccion(parte61, parte61.seccion("61.535"))],
        definiciones=[RetrievedDefinicion(parte1, defs[t]) for t in ("Vuelo nocturno", "Noche")],
    )


def test_definiciones_are_documents_after_the_secciones(with_definiciones):
    docs = build_documents(with_definiciones)
    assert [d["title"] for d in docs] == [
        "RAAC Parte 61 - Sección 61.535 Operaciones VFR nocturnas - Régimen transitorio",
        "RAAC Parte 1 - Sección 1.11 - Definición: Vuelo nocturno",
        "RAAC Parte 1 - Sección 1.11 - Definición: Noche",
    ]
    noche = docs[2]
    assert noche["citations"] == {"enabled": True}
    assert [b["text"][:6] for b in noche["source"]["content"]] == ["Noche:", "corres"]  # one block per PDF page
    assert noche["context"] == "Edición V, 24 septiembre 2024"


def test_cited_definicion_maps_to_parte_1(with_definiciones):
    answer = map_response(
        _response(
            ("Sin instrucción nocturna no podés volar VFR de noche", [_cite(0, 0)]),
            (", y la noche ", []),
            ("comienza al fin del crepúsculo civil vespertino", [_cite(2, 0, "Noche: Las horas comprendidas")]),
            (".", []),
        ),
        with_definiciones,
        {"61": URL, "1": URL1},
    )
    [sentence] = answer.sentences
    c = sentence.citations[1]
    assert (c.parte, c.seccion, c.definicion, c.seccion_title) == ("1", "1.11", "Noche", "Definición de «Noche»")
    assert (c.pdf_page_start, c.printed_page_start, c.edicion, c.source_url) == (38, "2.27", "V", URL1)
    assert sentence.citations[0].definicion is None


def test_definiciones_not_relied_on_are_not_citations(with_definiciones):
    answer = map_response(
        _response(("Sin instrucción nocturna no podés volar VFR de noche", [_cite(0, 0)]), (".", [])),
        with_definiciones,
        {"61": URL, "1": URL1},
    )
    cites = [c for s in answer.sentences for c in s.citations]
    assert cites and all((c.parte, c.definicion) == ("61", None) for c in cites)


def test_live_recording_cites_only_the_definiciones_it_relies_on(parte1, parte61):
    # `raac ask "Soy alumno piloto, ¿puedo volar solo en una TMA?"` with Partes 1 and 61 (the fixture
    # PDFs), recorded with --record. Tree search read Parte 1 pages, so ~80 Definiciones were documents.
    record = json.loads((FIXTURES / "citations-response-definicion-tma.json").read_text())
    partes = {"1": parte1, "61": parte61}
    defs = {d.term: d for d in parte1.definiciones}
    retrieval = Retrieval(
        routed_partes=["61", "1"],
        secciones=[RetrievedSeccion(partes[s["parte"]], partes[s["parte"]].seccion(s["seccion"])) for s in record["secciones"]],
        definiciones=[RetrievedDefinicion(parte1, defs[d["term"]]) for d in record["definiciones"]],
    )
    assert len(retrieval.definiciones) > 50
    answer = map_response(record["response"], retrieval, {"1": URL1, "61": URL})
    cites = [c for s in answer.sentences for c in s.citations]
    cited_definiciones = {c.definicion for c in cites if c.definicion}
    assert cited_definiciones == {"Área de control terminal (TMA)", "Área de control (CTA)"}
    assert "61.430" in {c.seccion for c in cites}
    for c in cites:
        if c.definicion:
            assert (c.parte, c.seccion) == ("1", "1.11")
            assert c.cited_text.strip() in defs[c.definicion].text


class _ReplayedStream:
    """Replays a recorded response as a stream: one content_block_stop per block, with the snapshot so far."""

    def __init__(self, response):
        self._response = response
        self._done = 0

    def __iter__(self):
        for self._done in range(1, len(self._response["content"]) + 1):
            yield SimpleNamespace(type="content_block_stop")

    @property
    def current_message_snapshot(self):
        snapshot = {**self._response, "content": self._response["content"][: self._done]}
        return SimpleNamespace(model_dump=lambda mode: snapshot)

    def get_final_message(self):
        return SimpleNamespace(model_dump=lambda mode: self._response)


class _StreamingClient:
    def __init__(self, response):
        stream = _ReplayedStream(response)
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: nullcontext(stream)))


def test_sentences_stream_as_they_complete_and_match_the_final_answer(response, retrieval):
    streamed = []
    final = answer(_StreamingClient(response), "m", "q", retrieval, {"61": URL}, on_sentence=streamed.append)
    assert len(final.sentences) > 1
    assert streamed == final.sentences[: len(streamed)]
    assert len(streamed) >= len(final.sentences) - 1  # only the last may wait for the final Answer


def test_framing_waits_for_the_first_cited_sentence_and_nothing_streams_without_coverage(two_secciones):
    blocks = [
        ("Depende. Hay condiciones:\n- ", []),
        ("tener instrucción", [_cite(1, 0)]),
        (".\n- ", []),
        ("tener constancia", [_cite(1, 0)]),
        (".", []),
    ]
    streamed = []
    response = _response(*blocks)
    answer(_StreamingClient(response), "m", "q", two_secciones, {"61": URL}, on_sentence=streamed.append)
    assert [s.text for s in streamed] == ["Depende.", "Hay condiciones:", "Tener instrucción."]

    for coverage in ("ninguna - nada", None):
        streamed = []
        final = answer(
            _StreamingClient(_response(*blocks, coverage=coverage)), "m", "q", two_secciones, {"61": URL},
            on_sentence=streamed.append,
        )
        assert final.refused and streamed == []
