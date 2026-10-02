"""Retriever: Standalone question -> Retrieval (ADR 0001 seam).

Tree search is PageIndex's own local agent over the ParteIndex; we observe its
tool calls to learn which PDF pages it read, then map those pages to Secciones.
Parte routing is not in this slice: every loaded Parte is searched.
"""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from pageindex import PageIndexClient

from . import strings
from .indexer import ParteIndex, walk
from .parser import ParsedParte, Seccion

_SEARCH_INSTRUCTIONS = (
    "Tu tarea es localizar, en la RAAC, el texto que responde la pregunta. "
    "Revisá la estructura del documento y leé con get_page_content las páginas de "
    "todas las Secciones pertinentes, incluidas las que establezcan regímenes "
    "transitorios o excepciones. Luego respondé en una sola oración."
)


@dataclass
class RetrievedSeccion:
    parte: ParsedParte
    seccion: Seccion


@dataclass
class Retrieval:
    routed_partes: list[str]
    visited_nodes: list[str] = field(default_factory=list)  # "<parte>:<node_id>"
    secciones: list[RetrievedSeccion] = field(default_factory=list)


Progress = Callable[[str], None]


class Retriever:
    def __init__(
        self,
        client: PageIndexClient,
        partes: list[tuple[ParsedParte, ParteIndex]],
        on_progress: Progress = lambda _msg: None,
    ):
        self._client = client
        self._partes = partes
        self._progress = on_progress

    def retrieve(self, standalone_question: str) -> Retrieval:
        retrieval = Retrieval(routed_partes=[p.code for p, _ in self._partes])
        for parsed, index in self._partes:
            self._progress(strings.PROGRESS_SEARCHING.format(parte=parsed.code))
            stream = self._client.chat(
                standalone_question,
                doc_id=index.doc_id,
                stream=True,
                instructions=_SEARCH_INSTRUCTIONS,
            )
            part = retrieval_from_events(stream.events, parsed, index, self._progress)
            retrieval.visited_nodes += part.visited_nodes
            retrieval.secciones += part.secciones
        return retrieval


def retrieval_from_events(
    events: Iterable[dict[str, Any]],
    parsed: ParsedParte,
    index: ParteIndex,
    on_progress: Progress = lambda _msg: None,
) -> Retrieval:
    """Map PageIndex agent events (tool calls) to the pages, nodes and Secciones it read."""
    pages: set[int] = set()
    for event in events:
        if event.get("type") != "tool_call" or event.get("name") != "get_page_content":
            continue
        arguments = event.get("arguments") or {}
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        spec = str(arguments.get("pages", ""))
        read = _expand_pages(spec)
        if read:
            on_progress(strings.PROGRESS_READING.format(parte=parsed.code, pages=spec))
        pages |= read
    visited = [
        f"{parsed.code}:{node['node_id']}"
        for node in walk(index.tree)
        if not node.get("nodes") and any(node["start_index"] <= p <= node["end_index"] for p in pages)
    ]
    return Retrieval(
        routed_partes=[parsed.code],
        visited_nodes=visited,
        secciones=[RetrievedSeccion(parsed, s) for s in parsed.secciones_on_pages(pages)],
    )


def _expand_pages(spec: str) -> set[int]:
    pages: set[int] = set()
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        start, _, end = part.partition("-")
        if start.isdigit() and (not end or end.isdigit()):
            pages.update(range(int(start), int(end or start) + 1))
    return pages
