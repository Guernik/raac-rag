"""Which Parte 1 Definiciones a retrieved Sección uses (no LLM)."""

from raac import definiciones
from raac.parser import DEFINICIONES_SECCION
from raac.indexer import ParteIndex
from raac.retriever import RetrievedSeccion, attach_definiciones, retrieval_from_events


def terms(defs):
    return [d.term for d in defs]


def test_terms_used_by_61_535(parte1, parte61):
    used = terms(definiciones.used_by([parte61.seccion("61.535").text], parte1.definiciones))
    assert {"Vuelo VFR", "Vuelo nocturno", "Piloto privado"} <= set(used)
    # "Vuelo nocturno: (Ver noche)." brings "Noche" along, right after it.
    assert used[used.index("Vuelo nocturno") + 1] == "Noche"
    # A one-word term inside a longer matched one adds nothing.
    assert "Piloto" not in used


def test_phrase_match_ignores_case_line_breaks_plurals_and_parentheticals(parte1):
    defs = [d for d in parte1.definiciones if d.term in ("Aeródromo controlado", "Área de control terminal (TMA)")]
    text = "hacia AERÓDROMOS\ncontrolados y en un área de\ncontrol terminal"
    assert set(terms(definiciones.used_by([text], defs))) == {"Aeródromo controlado", "Área de control terminal (TMA)"}
    assert definiciones.used_by(["aeródromo no controlado"], defs) == []


def test_capped_per_seccion_and_in_total(parte1, parte61):
    texts = [parte61.seccion(i).text for i in ("61.520", "61.140", "61.405", "61.430")]
    assert len(definiciones.used_by(texts[:1], parte1.definiciones, per_seccion=3)) <= 3 * 2  # each may bring a pointed-to one
    assert len(definiciones.used_by(texts, parte1.definiciones, max_total=5)) == 5


def test_61_430_gets_tma_cta_and_espacio_aereo_controlado(parte1, parte61):
    attached = attach_definiciones([RetrievedSeccion(parte61, parte61.seccion("61.430"))], parte1)
    assert {"Área de control terminal (TMA)", "Área de control (CTA)", "Espacio aéreo controlado"} <= {
        d.definicion.term for d in attached
    }
    assert all(d.parte is parte1 for d in attached)


def test_pages_read_in_the_definiciones_seccion_map_to_definiciones(parte1):
    index = ParteIndex("1", parte1.content_hash, "doc", [{"node_id": "0000", "title": "x", "start_index": 1, "end_index": 65}])
    events = [{"type": "tool_call", "name": "get_page_content", "arguments": {"pages": "38"}}]
    r = retrieval_from_events(events, parte1, index)
    assert DEFINICIONES_SECCION not in [s.seccion.id for s in r.secciones]
    read = [d.definicion.term for d in r.definiciones]
    assert "Noche" in read and len(read) < 20
    assert all(any(p.pdf_page == 38 for p in d.definicion.pages) for d in r.definiciones)
