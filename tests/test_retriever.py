"""Mapping PageIndex tree-search events to Secciones, on a real (LLM-free) PageIndex flash tree."""

import pytest
from pageindex import page_index_flash

from conftest import FIXTURES
from raac.indexer import ParteIndex, attach_secciones, walk
from raac.retriever import retrieval_from_events


@pytest.fixture(scope="module")
def index61(parte61):
    tree = page_index_flash(str(FIXTURES / "raac-parte-61.pdf"), summary=False, optimize=False)["structure"]
    for i, node in enumerate(walk(tree)):
        node.setdefault("node_id", f"{i:04d}")
    return ParteIndex("61", parte61.content_hash, "doc", attach_secciones(tree, parte61))


def test_nodes_carry_seccion_ids(index61):
    capitulo_d = next(n for n in walk(index61.tree) if n["title"].startswith("CAPÍTULO D") and n["start_index"] > 13)
    assert "61.535" in capitulo_d["secciones"]


def test_pages_read_map_to_secciones_and_nodes(parte61, index61):
    events = [
        {"type": "tool_call", "name": "get_document_structure", "arguments": {"doc_name": "x"}},
        {"type": "tool_call", "name": "get_page_content", "arguments": '{"doc_name": "x", "pages": "67"}'},
        {"type": "tool_result", "name": "get_page_content", "output": "..."},
        {"type": "answer", "delta": "ok"},
    ]
    progress = []
    r = retrieval_from_events(events, parte61, index61, progress.append)
    assert [s.seccion.id for s in r.secciones] == ["61.530", "61.535"]
    assert r.routed_partes == ["61"]
    assert r.visited_nodes
    assert progress == ["Leyendo Parte 61, páginas PDF 67"]


def test_page_ranges_expand(parte61, index61):
    events = [{"type": "tool_call", "name": "get_page_content", "arguments": {"pages": "63-64, 67"}}]
    ids = [s.seccion.id for s in retrieval_from_events(events, parte61, index61).secciones]
    assert "61.520" in ids and "61.535" in ids
