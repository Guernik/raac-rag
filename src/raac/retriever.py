"""Retriever: Standalone question -> Retrieval (ADR 0001 seam).

Parte routing first picks which Partes to search (router.py). Tree search is
PageIndex's own local agent over each routed ParteIndex; we observe its tool
calls to learn which PDF pages it read, then map those pages to Secciones.
Pages read in Parte 1's Definiciones Sección (1.11, ~50 pages) map to the
Definiciones on them, not to the whole Sección. When Parte 1 is loaded, the
Definiciones the retrieved Secciones use are also attached as context for the
Answerer (definiciones.py), whether or not Parte 1 was routed.
"""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pageindex import PageIndexClient

from . import definiciones, strings
from .indexer import ParteIndex, walk
from .parser import DEFINICIONES_PARTE, DEFINICIONES_SECCION, Definicion, ParsedParte, Seccion

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
class RetrievedDefinicion:
    parte: ParsedParte
    definicion: Definicion


@dataclass
class Retrieval:
    routed_partes: list[str]
    visited_nodes: list[str] = field(default_factory=list)  # "<parte>:<node_id>"
    secciones: list[RetrievedSeccion] = field(default_factory=list)
    definiciones: list[RetrievedDefinicion] = field(default_factory=list)  # context; cited only if relied on


Progress = Callable[[str], None]


class Router(Protocol):
    def route(self, standalone_question: str) -> list[str]: ...


class Retriever:
    def __init__(
        self,
        client: PageIndexClient,
        partes: list[tuple[ParsedParte, ParteIndex]],
        router: Router | None = None,
        on_progress: Progress = lambda _msg: None,
    ):
        self._client = client
        self._partes = {parsed.code: (parsed, index) for parsed, index in partes}
        self._router = router
        self._progress = on_progress

    def retrieve(self, standalone_question: str) -> Retrieval:
        if self._router is None:
            routed = list(self._partes)
        else:
            self._progress(strings.PROGRESS_ROUTING)
            routed = [code for code in self._router.route(standalone_question) if code in self._partes]
            self._progress(strings.PROGRESS_ROUTED.format(partes=", ".join(routed) or "-"))
        retrieval = Retrieval(routed_partes=routed)
        for code in routed:
            parsed, index = self._partes[code]
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
            retrieval.definiciones += part.definiciones
        if DEFINICIONES_PARTE in self._partes:
            read = {id(d.definicion) for d in retrieval.definiciones}
            retrieval.definiciones += [
                d
                for d in attach_definiciones(retrieval.secciones, self._partes[DEFINICIONES_PARTE][0])
                if id(d.definicion) not in read
            ]
        return retrieval


def attach_definiciones(secciones: list[RetrievedSeccion], parte1: ParsedParte) -> list[RetrievedDefinicion]:
    """Parte 1 Definiciones the retrieved Secciones use."""
    used = definiciones.used_by((r.seccion.text for r in secciones), parte1.definiciones)
    return [RetrievedDefinicion(parte1, d) for d in used]


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
    secciones = parsed.secciones_on_pages(pages)
    read_definiciones = []
    if parsed.definiciones and any(s.id == DEFINICIONES_SECCION for s in secciones):
        secciones = [s for s in secciones if s.id != DEFINICIONES_SECCION]
        read_definiciones = [
            RetrievedDefinicion(parsed, d) for d in parsed.definiciones if any(p.pdf_page in pages for p in d.pages)
        ]
    return Retrieval(
        routed_partes=[parsed.code],
        visited_nodes=visited,
        secciones=[RetrievedSeccion(parsed, s) for s in secciones],
        definiciones=read_definiciones,
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
