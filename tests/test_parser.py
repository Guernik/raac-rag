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


def test_edicion_without_enmienda_and_ordinal_footers(parte1, parte67, parte91_sample):
    # Parte 1 prints "5º Edición", 91 "4º Edición" (sometimes in a block of its own),
    # 67 "V Edición"; none prints an Enmienda.
    assert (parte1.code, parte1.edicion, parte1.enmienda) == ("1", "V", None)
    assert (parte67.code, parte67.edicion, parte67.enmienda) == ("67", "V", None)
    assert (parte91_sample.code, parte91_sample.edicion, parte91_sample.enmienda) == ("91", "IV", None)


def test_printed_page_from_header_when_footer_has_none(parte1, parte91_sample):
    # Body pages of Partes 1 and 91 carry the printed page in the running header ("SUBPARTE B 2. 2").
    assert parte1.printed_pages[11] == "2.1"
    assert parte1.printed_pages[6] == "v"
    assert parte91_sample.printed_pages[2] == "2.2"


def test_parte_67_secciones(parte67):
    ids = [s.id for s in parte67.secciones]
    assert len(ids) == len(set(ids)) == 36
    assert ids[0] == "67.001"
    s = parte67.seccion("67.015")
    assert s.title.startswith("Otorgamiento del certificado")
    assert s.pdf_page_start == 11


def test_parte_1_secciones(parte1):
    assert [s.id for s in parte1.secciones] == ["1.3", "1.7", "1.11", "1.21"]
    assert "Aeródromo (AD)" in parte1.seccion("1.11").text


def test_parte_91_headings_mid_block_and_after_subparte_indice(parte91_sample):
    ids = [s.id for s in parte91_sample.secciones]
    assert len(ids) == len(set(ids))
    # 91.101 is listed in the Subparte B Índice and starts on the next page.
    assert parte91_sample.seccion("91.101").pdf_page_start == 3
    assert "Índice" not in parte91_sample.seccion("91.101").text
    # Title on its own line, starting in lowercase.
    assert parte91_sample.seccion("91.103").title == "información sobre vuelos."
    # Id and title on one line.
    assert parte91_sample.seccion("91.177").title == "Altitudes mínimas para operaciones IFR."
    # Several headings inside one text block.
    assert {"91.505", "91.507", "91.509", "91.510"} <= set(ids)
    assert "91.509" not in parte91_sample.seccion("91.505").text
    # "91.137 al 91.145 Reservado" closes a Sección without opening one.
    assert "91.137" not in ids
    assert "Reservado" not in parte91_sample.seccion("91.147").text.split("\n")[1]


def test_page_check_list_is_not_a_seccion(parte1):
    # "Lista de verificación de páginas" pairs the label "1.1" with "SUBPARTE A".
    assert "1.1" not in [s.id for s in parte1.secciones]
