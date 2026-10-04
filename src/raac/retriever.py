"""Retriever: Standalone question -> Retrieval (ADR 0001 seam).

Parte routing first picks which Partes to search (router.py). Tree search is
PageIndex's own local agent over each routed ParteIndex; we observe its tool
calls to learn which PDF pages it read, then map those pages to Secciones.
Then the Remisiones of those Secciones to other Secciones are followed in code,
one hop and never further, with no extra LLM turns. Remisiones to a whole Parte
are left to Parte routing.
"""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

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
# Bounds the documents a Retrieval sends the Answerer; eval cases follow at most 5.
MAX_FOLLOWED = 10


@dataclass
class RetrievedSeccion:
    parte: ParsedParte
    seccion: Seccion


@dataclass
class Retrieval:
    routed_partes: list[str]
    visited_nodes: list[str] = field(default_factory=list)  # "<parte>:<node_id>"
    secciones: list[RetrievedSeccion] = field(default_factory=list)
    followed_remisiones: list[str] = field(default_factory=list)  # "<parte>:<sección> -> <parte>:<sección>"


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
        loaded = {code: parsed for code, (parsed, _) in self._partes.items()}
        followed, retrieval.followed_remisiones = follow_remisiones(retrieval.secciones, loaded, self._progress)
        retrieval.secciones += followed
        return retrieval


def follow_remisiones(
    secciones: list[RetrievedSeccion],
    partes: dict[str, ParsedParte],
    on_progress: Progress = lambda _msg: None,
) -> tuple[list[RetrievedSeccion], list[str]]:
    """One hop: the loaded Secciones that `secciones` name in a Remisión and that are not
    already among them, in retrieval then text order, at most MAX_FOLLOWED. Returns them
    and a log of "<parte>:<sección> -> <parte>:<sección>". Followed Secciones' own
    Remisiones are never followed."""
    have = {(r.parte.code, r.seccion.id) for r in secciones}
    followed: list[RetrievedSeccion] = []
    log: list[str] = []
    for source in secciones:
        for remision in source.seccion.remisiones:
            parte = partes.get(remision.to_parte)
            key = (remision.to_parte, remision.to_seccion)
            if remision.to_seccion is None or parte is None or key in have:
                continue
            try:
                seccion = parte.seccion(remision.to_seccion)
            except KeyError:
                continue
            if len(followed) == MAX_FOLLOWED:
                return followed, log
            have.add(key)
            followed.append(RetrievedSeccion(parte, seccion))
            log.append(f"{source.parte.code}:{source.seccion.id} -> {parte.code}:{seccion.id}")
            on_progress(strings.PROGRESS_FOLLOWING.format(parte=parte.code, seccion=seccion.id))
    return followed, log


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
