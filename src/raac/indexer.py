"""Indexer: ParsedParte -> ParteIndex (a PageIndex local tree with Sección ids on its nodes, ADR 0001)."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pageindex import PageIndexClient

from .config import Models
from .parser import ParsedParte


@dataclass
class ParteIndex:
    parte: str
    content_hash: str
    doc_id: str
    tree: list[dict[str, Any]]  # PageIndex nodes, each with a "secciones" list of Sección ids
    description: str | None = None  # PageIndex document description: the Parte's root summary


def pageindex_client(models: Models, storage_path: Path) -> PageIndexClient:
    # PageIndex reaches Claude through LiteLLM, hence the "anthropic/" route prefix.
    return PageIndexClient(
        index={"model": f"anthropic/{models.indexing}", "storage_path": str(storage_path)},
        chat=f"anthropic/{models.search}",
    )


def index(parsed: ParsedParte, pdf_path: Path, client: PageIndexClient, storage_path: Path) -> ParteIndex:
    """Build (or load, when this Parte + content hash was indexed before) the tree."""
    meta_path = storage_path / f"raac-{parsed.code}-{parsed.content_hash[:16]}.json"
    if meta_path.exists():
        doc_id = json.loads(meta_path.read_text())["doc_id"]
    else:
        doc_id = client.submit_document(str(pdf_path))["doc_id"]
        meta_path.write_text(json.dumps({"doc_id": doc_id, "content_hash": parsed.content_hash}))
    tree = client.get_tree(doc_id, node_summary=True, include_text=False)["result"]
    return ParteIndex(
        parte=parsed.code,
        content_hash=parsed.content_hash,
        doc_id=doc_id,
        tree=attach_secciones(tree, parsed),
        description=client.get_document(doc_id).get("description"),
    )


def attach_secciones(tree: list[dict[str, Any]], parsed: ParsedParte) -> list[dict[str, Any]]:
    """Tag each node with the Secciones whose text starts within its page range."""
    for node in walk(tree):
        start, end = node["start_index"], node["end_index"]
        node["secciones"] = [s.id for s in parsed.secciones if start <= s.pdf_page_start <= end]
    return tree


def walk(tree: list[dict[str, Any]]):
    for node in tree:
        yield node
        yield from walk(node.get("nodes") or [])
