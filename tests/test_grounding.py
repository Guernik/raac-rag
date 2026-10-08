"""Grounded or silent: unsupported claims never reach the output, the coverage line decides refusals,
and a refusal names the likely Parte when a retrieved Sección has a Remisión to it.

Responses are built in the Citations API's shape (no live LLM call); see test_answerer.py for the
live recording.
"""

import json
import re
from pathlib import Path

import pytest

from raac import strings
from raac.answerer import Answer, Sentence, answer, map_response
from raac.cli import render
from raac.remisiones import find_remisiones, parte_codes
from raac.retriever import Retrieval, RetrievedSeccion

URL = "https://docs.anac.gob.ar/index.php/s/PtMG8j8sFeRyren"
EVAL_CASES = Path(__file__).parent.parent / "evals" / "refusal-cases.jsonl"


def _cite(document_index: int, block: int = 0, text: str = "x") -> dict:
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


@pytest.fixture
def medical(parte61):
    # 61.060 requires a current medical certificate "conforme a la RAAC 67"; 61.065 says its
    # validity is set in Sección 67.015 of RAAC 67.
    return Retrieval(
        routed_partes=["61"],
        secciones=[RetrievedSeccion(parte61, parte61.seccion(i)) for i in ("61.065", "61.060")],
    )


def _body(out: str) -> str:
    return out.split("\n\n")[1]


def test_partial_answer_is_marked_incomplete_and_names_the_likely_parte(medical, parte61):
    answer = map_response(
        _response(
            ("COBERTURA: parcial - cuánto dura el certificado médico; lo regula la Parte 67.\n\nPara renovarla tenés que ", []),
            ("tener la certificación médica aeronáutica vigente", [_cite(1)]),
            (".", []),
        ),
        medical,
        {"61": URL},
    )
    assert not answer.refused
    assert answer.incomplete
    assert [s.text for s in answer.sentences] == ["Para renovarla tenés que tener la certificación médica aeronáutica vigente."]
    assert (answer.coverage, answer.gap) == ("parcial", "cuánto dura el certificado médico; lo regula la Parte 67.")
    assert answer.dropped == []
    [likely] = answer.likely_partes
    assert likely.parte == "67"
    # Cited Secciones come first; the Remisión text is verbatim from the parsed page.
    c = likely.citation
    assert (c.parte, c.seccion, c.pdf_page_start, c.edicion, c.enmienda) == ("61", "61.060", 25, "VI", "I")
    assert "RAAC 67" in c.cited_text
    assert c.cited_text in parte61.seccion("61.060").pages[0].text

    out = render(answer)
    body = _body(out)
    assert body.startswith(strings.INCOMPLETE)
    assert "Lo que falta probablemente lo regula la Parte 67: la Sección 61.060 remite a ella. [1]" in body
    assert "COBERTURA" not in out
    assert "cuánto dura" not in out


def test_out_of_scope_refusal_names_the_parte_a_retrieved_seccion_refers_to(medical, parte61):
    answer = map_response(
        _response(("COBERTURA: ninguna - la validez está en la RAAC Parte 67.", [])), medical, {"61": URL}
    )
    assert answer.refused and not answer.incomplete
    assert answer.sentences == []
    [likely] = answer.likely_partes
    # Nothing was cited, so retrieval order decides: 61.065 is the first retrieved Sección.
    assert (likely.parte, likely.citation.seccion, likely.citation.pdf_page_start) == ("67", "61.065", 26)
    assert likely.citation.cited_text == (
        "La validez de los certificados médicos aeronáuticos se establece en la Sección 67.015 del RAAC 67."
    )

    out = render(answer)
    assert _body(out) == strings.REFUSAL + " " + strings.LIKELY_PARTE.format(parte="67", seccion="61.065") + " [1]"
    assert "[1] Parte 61, Sección 61.065 (Validez del certificado médico aeronáutico), página PDF 26" in out


def test_a_seccion_the_gap_names_is_preferred_for_the_likely_parte(medical):
    answer = map_response(
        _response(("COBERTURA: ninguna - la Sección 61.060 remite a la Parte 67.", [])), medical, {"61": URL}
    )
    [likely] = answer.likely_partes
    assert (likely.parte, likely.citation.seccion) == ("67", "61.060")


def test_cited_sentences_are_withheld_when_coverage_is_ninguna(medical):
    answer = map_response(
        _response(
            ("COBERTURA: ninguna - la Parte 67.\n\n", []),
            ("tener la certificación médica aeronáutica vigente", [_cite(1)]),
            (".", []),
        ),
        medical,
        {"61": URL},
    )
    assert answer.refused and answer.sentences == []
    assert [lp.parte for lp in answer.likely_partes] == ["67"]


def test_parte_named_by_the_model_without_a_remision_in_the_text_is_not_shown(medical):
    answer = map_response(
        _response(("COBERTURA: ninguna - los mínimos meteorológicos están en la Parte 91.", [])), medical, {"61": URL}
    )
    assert answer.refused
    assert answer.likely_partes == []
    assert _body(render(answer)) == strings.REFUSAL


def test_out_of_scope_answer_without_a_coverage_line_is_a_plain_refusal(medical):
    answer = map_response(
        _response(
            ("La altitud de densidad aumenta con la temperatura, así que ", []),
            ("la carrera de despegue se alarga", [_cite(1)]),
            (".", []),
        ),
        medical,
        {"61": URL},
    )
    assert answer.refused and answer.coverage is None
    assert answer.likely_partes == []
    out = render(answer)
    assert out == strings.NOTICE + "\n\n" + strings.REFUSAL
    assert "altitud" not in out


def test_api_refusal_and_empty_retrieval_are_refusals(medical):
    assert map_response({"stop_reason": "refusal", "content": []}, medical, {"61": URL}).refused
    empty = answer(None, "unused", "¿Pregunta?", Retrieval(routed_partes=["61"]), {"61": URL})
    assert empty.refused and empty.sentences == [] and empty.likely_partes == []


def test_no_unsupported_claim_reaches_the_output(medical):
    answer = map_response(
        _response(
            ("COBERTURA: total\n\nSí. Inventado en 2019. ", []),
            ("tener la certificación médica aeronáutica vigente", [_cite(1, text="certificación médica vigente")]),
            (". Vale por 24 meses. Lo dice la Sección 61.140. ", []),
            ("tenerla vigente", [_cite(1, text="certificación médica vigente")]),
            (" durante 12 meses.", []),
        ),
        medical,
        {"61": URL},
    )
    assert answer.dropped == [
        "Inventado en 2019.",
        "Vale por 24 meses.",
        "Lo dice la Sección 61.140.",
        "tenerla vigente durante 12 meses.",  # cited, but the number is not in the cited text
    ]
    out = render(answer)
    assert _body(out) == "Sí. Tener la certificación médica aeronáutica vigente. [1]"
    for claim in answer.dropped:
        assert claim not in out


def test_render_shows_framing_without_citation_marks():
    out = render(Answer(sentences=[Sentence("Depende.", []), Sentence("Condiciones:", [], "paragraph")], refused=False))
    assert _body(out) == "Depende.\nCondiciones:"


@pytest.mark.parametrize(
    "text, codes",
    [
        ("conforme a la RAAC 67;", ["67"]),
        ("otorgado conforme RAAC Parte 67 – Certificado", ["67"]),
        ("en la Sección 67.015 del RAAC 67.", ["67"]),
        ("RAAC 61, RAAC 63 y RAAC 65.", ["61", "63", "65"]),
        ("RAAC Partes 141 o 142, podrán", ["141", "142"]),
        ("Parte 121 ó 135 de estas RAAC", ["121", "135"]),
        ("lo regula la parte HL.", ["HL"]),
        ("la Sección 61.535 (a)", []),
        ("RAAC DE LA ANAC", []),
    ],
)
def test_parte_codes(text, codes):
    assert parte_codes(text) == codes


def test_remisiones_skip_the_own_parte_and_keep_the_verbatim_line(parte61):
    remisiones = find_remisiones(parte61.seccion("61.065"), "61")
    assert [(r.to_parte, r.page_index) for r in remisiones] == [("67", 0)]
    assert remisiones[0].text == (
        "La validez de los certificados médicos aeronáuticos se establece en la Sección 67.015 del RAAC 67."
    )
    # A Remisión wrapped onto its own line is widened to the clause, back to the inciso marker.
    [to_67] = [r for r in find_remisiones(parte61.seccion("61.060"), "61") if r.to_parte == "67"][:1]
    assert to_67.text == (
        "Se encuentre vigente la certificación médica aeronáutica correspondiente y otorgado\nbajo la RAAC 67;"
    )
    assert all(r.to_parte != "61" for s in parte61.secciones for r in find_remisiones(s, "61"))


def test_refusal_eval_cases_are_out_of_scope_in_the_eval_case_format():
    lines = [json.loads(line) for line in EVAL_CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) >= 3
    assert len({c["id"] for c in lines}) == len(lines)
    for case in lines:
        assert set(case) <= {"id", "question", "expected_secciones", "reference_answer", "out_of_scope", "source"}
        assert case["out_of_scope"] is True and case["expected_secciones"] == []
        assert case["question"].strip() and case["reference_answer"].strip()


def test_live_recording_refusal_names_parte_67(parte61):
    """Replays `raac ask "¿Cuánto dura el certificado médico clase 2 de un piloto privado?" --record ...`
    against Parte 61 alone (recorded 2026-10-08 with the fixture PDF, byte-identical to the live one)."""
    record = json.loads((Path(__file__).parent / "fixtures" / "citations-response-medico-67.json").read_text())
    retrieval = Retrieval(
        routed_partes=["61"],
        secciones=[RetrievedSeccion(parte61, parte61.seccion(s["seccion"])) for s in record["secciones"]],
    )
    answer = map_response(record["response"], retrieval, {"61": URL})
    assert answer.refused and answer.coverage == "ninguna" and answer.sentences == []
    assert "Parte 67" in answer.gap
    [likely] = answer.likely_partes
    # The gap names 61.065, whose Remisión points to Parte 67.
    assert (likely.parte, likely.citation.seccion) == ("67", "61.065")
    assert "RAAC 67" in likely.citation.cited_text

    out = render(answer)
    assert _body(out).startswith(strings.REFUSAL)
    assert "COBERTURA" not in out
