"""Remisiones extracted by ParteParser and followed one hop by tree search (no LLM calls)."""

import json

import pytest
from agents import function_tool

from raac.indexer import ParteIndex
from raac.parser import ParsedParte
from raac.retriever import ReferenceFollower, Retriever, follow_reference_tool


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


@pytest.fixture
def follower(parte61, parte67):
    return ReferenceFollower(parte61, {"61": parte61, "67": parte67})


def test_follow_seccion_remision(follower):
    text = follower.follow("61.535", "61.520")
    assert text.startswith("RAAC Parte 61 - Sección 61.520")
    assert "(páginas PDF 63-65)" in text
    assert [(r.parte.code, r.seccion.id) for r in follower.followed] == [("61", "61.520")]
    assert follower.log == ["61:61.535 -> 61:61.520"]


def test_follow_parte_remision_lists_secciones_then_reads_one(follower):
    listing = follower.follow("61.405", "RAAC 67")
    assert "67.020 Clases de certificado médico y su aplicación" in listing
    assert follower.followed == []
    text = follower.follow("61.405", "67.020")
    assert text.startswith("RAAC Parte 67 - Sección 67.020")
    assert [(r.parte.code, r.seccion.id) for r in follower.followed] == [("67", "67.020")]


def test_never_more_than_one_hop(follower):
    follower.follow("61.535", "61.520")
    # 61.520 was reached through a Remisión: its own Remisiones are a second hop.
    assert "solo se sigue un salto" in follower.follow("61.520", "61.515")
    # Secciones of another Parte can never be a starting point.
    follower.follow("61.405", "67.020")
    assert "no es de la RAAC Parte 61" in follower.follow("67.020", "67.025")
    assert [r.seccion.id for r in follower.followed] == ["61.520", "67.020"]


def test_only_what_the_seccion_refers_to(follower):
    assert "no remite a 61.001" in follower.follow("61.535", "61.001")
    assert "Sus Remisiones: 61.520" in follower.follow("61.535", "67")
    assert "no está cargada" in follower.follow("61.060", "121")
    assert follower.followed == []


def test_tool_schema(follower):
    tool = follow_reference_tool(follower, function_tool)
    assert tool.name == "follow_reference"
    assert set(tool.params_json_schema["properties"]) == {"desde", "hacia"}


def test_followed_secciones_join_the_retrieval(parte61, parte67):
    def fake_search(_client, _question, _doc_id, follower):
        follower.follow("61.405", "67.020")
        return [{"type": "tool_call", "name": "get_page_content", "arguments": json.dumps({"pages": "51"})}]

    progress = []
    r = Retriever(
        client=None,
        partes=[(parte61, ParteIndex("61", "h", "d61", [])), (parte67, ParteIndex("67", "h", "d67", []))],
        router=type("R", (), {"route": lambda self, q: ["61"]})(),
        on_progress=progress.append,
        search=fake_search,
    ).retrieve("¿Qué necesito para volar solo como alumno?")
    ids = [(s.parte.code, s.seccion.id) for s in r.secciones]
    assert ("61", "61.405") in ids and ids[-1] == ("67", "67.020")
    assert r.followed_remisiones == ["61:61.405 -> 67:67.020"]
    assert "Siguiendo una remisión a Parte 67, Sección 67.020" in progress
