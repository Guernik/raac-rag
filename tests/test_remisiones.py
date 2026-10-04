"""Remisiones extracted by ParteParser and followed one hop after tree search (no LLM calls)."""

from types import SimpleNamespace

from raac import retriever
from raac.indexer import ParteIndex
from raac.parser import ParsedParte
from raac.retriever import RetrievedSeccion, Retriever, follow_remisiones


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


def retrieved(*pairs: tuple[ParsedParte, str]) -> list[RetrievedSeccion]:
    return [RetrievedSeccion(parte, parte.seccion(sid)) for parte, sid in pairs]


def ids(secciones: list[RetrievedSeccion]) -> list[str]:
    return [f"{r.parte.code}:{r.seccion.id}" for r in secciones]


def test_follows_seccion_remisiones(parte61, parte67):
    progress = []
    followed, log = follow_remisiones(retrieved((parte61, "61.535")), {"61": parte61, "67": parte67}, progress.append)
    assert ids(followed) == ["61:61.520"]
    assert log == ["61:61.535 -> 61:61.520"]
    assert progress == ["Siguiendo una remisión a Parte 61, Sección 61.520"]


def test_parte_remisiones_are_left_to_routing(parte61, parte67):
    # 61.405(f) "conforme a la RAAC 67" names no Sección.
    assert follow_remisiones(retrieved((parte61, "61.405")), {"61": parte61, "67": parte67}) == ([], [])


def test_never_more_than_one_hop(parte61):
    # 61.605 -> 61.610 -> 61.600: 61.610 was reached through a Remisión, so 61.600 is a second hop.
    followed, _ = follow_remisiones(retrieved((parte61, "61.605")), {"61": parte61})
    assert "61:61.610" in ids(followed) and "61:61.600" not in ids(followed)


def test_skips_retrieved_unloaded_and_repeated_targets(parte61):
    # 61.060 names 61.140, 61.130, 61.135 and Partes 67, 121, 135; 61.130 is already retrieved.
    followed, log = follow_remisiones(retrieved((parte61, "61.060"), (parte61, "61.130")), {"61": parte61})
    assert ids(followed) == ["61:61.140", "61:61.135"]
    assert log == ["61:61.060 -> 61:61.140", "61:61.060 -> 61:61.135"]


def test_followed_secciones_are_capped(parte61, monkeypatch):
    monkeypatch.setattr(retriever, "MAX_FOLLOWED", 1)
    followed, _ = follow_remisiones(retrieved((parte61, "61.060")), {"61": parte61})
    assert ids(followed) == ["61:61.140"]


def test_followed_secciones_join_the_retrieval(parte61, parte67):
    # Tree search reads PDF page 67 (end of 61.530, 61.535); 61.535 names 61.520.
    client = SimpleNamespace(
        chat=lambda *a, **k: SimpleNamespace(
            events=[{"type": "tool_call", "name": "get_page_content", "arguments": '{"pages": "67"}'}]
        )
    )
    r = Retriever(
        client,
        [(parte61, ParteIndex("61", "h", "d61", [])), (parte67, ParteIndex("67", "h", "d67", []))],
        router=SimpleNamespace(route=lambda q: ["61"]),
    ).retrieve("¿Puedo volar VFR de noche con mi PPL?")
    assert "61:61.535" in ids(r.secciones) and "61:61.520" in ids(r.secciones)
    assert ids(r.secciones)[-len(r.followed_remisiones):] == [f.split(" -> ")[1] for f in r.followed_remisiones]
    assert any(f.endswith("-> 61:61.520") for f in r.followed_remisiones)
