"""Remisiones extracted by ParteParser."""

from raac.parser import ParsedParte


def targets(parte: ParsedParte, seccion_id: str) -> list[tuple[str, str | None]]:
    return [(r.to_parte, r.to_seccion) for r in parte.seccion(seccion_id).remisiones]


def test_61_535_has_a_remision_to_61_520(parte61):
    [r] = parte61.seccion("61.535").remisiones
    assert (r.to_parte, r.to_seccion) == ("61", "61.520")
    assert r.text == (
        "acredite la experiencia prevista en la Sección 61.520 (a)(1)(v) o 61.520 (b)(1) (v) según co-\nrresponda; y"
    )
    assert parte61.seccion("61.535").pages[r.page_index].pdf_page == 67


def test_61_405_f_has_a_remision_to_parte_67(parte61):
    [r] = parte61.seccion("61.405").remisiones
    assert (r.to_parte, r.to_seccion) == ("67", None)
    assert r.text == "Poseer una Certificación Médica Aeronáutica conforme a la RAAC 67."


def test_remisiones_to_seccion_and_parte_in_text_order(parte61):
    assert targets(parte61, "61.065") == [("67", "67.015"), ("67", None)]
    assert targets(parte61, "61.060") == [
        ("67", None),
        ("61", "61.140"),
        ("61", "61.130"),
        ("61", "61.135"),
        ("121", None),
        ("135", None),
    ]


def test_seccion_lists_and_broken_ids(parte61):
    # "Secciones 61. 815 y 61.825 de este capítulo": the id is broken after the dot.
    found = {t for s in parte61.secciones for t in targets(parte61, s.id)}
    assert {("61", "61.815"), ("61", "61.825")} <= found
    # Never to the own Parte as a whole, never to the Sección itself.
    assert all(r.to_seccion or r.to_parte != "61" for s in parte61.secciones for r in s.remisiones)
    assert all(r.to_seccion != s.id for s in parte61.secciones for r in s.remisiones)
