import pymupdf
import pytest

from raac.parser import ParseError, parse


def test_parte_and_version_from_footer(parte61):
    assert parte61.code == "61"
    assert (parte61.edicion, parte61.enmienda) == ("VI", "I")
    assert parte61.page_count == 135


def test_61_535_on_pdf_page_67_printed_page_10(parte61):
    s = parte61.seccion("61.535")
    assert s.title == "Operaciones VFR nocturnas - Régimen transitorio"
    assert (s.pdf_page_start, s.pdf_page_end) == (67, 67)
    assert s.pages[0].printed_page == "10"
    assert "31 de diciembre de 2027" in s.text
    assert s.pages[0].rects


def test_pdf_page_and_printed_page_kept_separate(parte61):
    assert parte61.printed_pages[66] == "10"
    assert parte61.printed_pages[1] == "II"  # front matter uses roman labels
    assert parte61.printed_pages[0] is None  # cover has no footer


def test_indice_is_not_parsed_as_secciones(parte61):
    ids = [s.id for s in parte61.secciones]
    assert len(ids) == len(set(ids))
    assert parte61.seccion("61.001").pdf_page_start == 13


def test_seccion_ends_at_capitulo_heading(parte61):
    # Capítulo E starts on PDF page 68; 61.535 is the last Sección of Capítulo D.
    assert all("CAPÍTULO E" not in p.text for p in parte61.seccion("61.535").pages)


def test_secciones_span_pages(parte61):
    s = parte61.seccion("61.520")
    assert [p.pdf_page for p in s.pages] == [63, 64, 65]


def test_missing_footer_raises():
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "RAAC PARTE 61\n61.001\nDefiniciones")
    with pytest.raises(ParseError, match="Edición/Enmienda"):
        parse(doc.tobytes())
