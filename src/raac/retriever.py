"""Retriever: Standalone question -> Retrieval (ADR 0001 seam).

Parte routing first picks which Partes to search (router.py). Tree search is
PageIndex's own local agent over each routed ParteIndex; we observe its tool
calls to learn which PDF pages it read, then map those pages to Secciones.
The agent also gets `follow_reference`: from a Sección of the searched Parte it
may read the Sección or Parte a Remisión names, one hop and never further.
"""

import json
import re
from collections.abc import Callable, Iterable, Iterator
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
    "transitorios o excepciones. Si una de esas Secciones remite a otra Sección o Parte "
    "cuyo texto hace falta para responder, leelo con follow_reference. "
    "Luego respondé en una sola oración."
)
_FOLLOW_DESCRIPTION = (
    "Lee el texto al que remite una Sección de la RAAC (una Remisión), un solo salto. "
    "desde: id de la Sección que leíste y que contiene la Remisión, por ejemplo '61.535'. "
    "hacia: la Sección nombrada en la Remisión ('61.520'); o la Parte nombrada ('67'), que "
    "devuelve la lista de sus Secciones; o una Sección de esa Parte ('67.020'). "
    "No se pueden seguir Remisiones del texto obtenido con esta herramienta."
)
_SID = re.compile(r"(?:secci[oó]n\s+)?(\d{1,3})\.\s?(\d{1,4})", re.I)
_PARTE = re.compile(r"(?:RAAC\s+)?(?:Parte\s+)?([0-9]{1,3}|[A-Z]{2,3})", re.I)


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
        search: "Search | None" = None,
    ):
        self._client = client
        self._search = search or search_events
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
        loaded = {code: parsed for code, (parsed, _) in self._partes.items()}
        for code in routed:
            parsed, index = self._partes[code]
            self._progress(strings.PROGRESS_SEARCHING.format(parte=parsed.code))
            follower = ReferenceFollower(parsed, loaded, self._progress)
            events = self._search(self._client, standalone_question, index.doc_id, follower)
            part = retrieval_from_events(events, parsed, index, self._progress)
            retrieval.visited_nodes += part.visited_nodes
            retrieval.secciones += part.secciones + follower.followed
            retrieval.followed_remisiones += follower.log
        seen: set[tuple[str, str]] = set()
        retrieval.secciones = [
            r for r in retrieval.secciones
            if (r.parte.code, r.seccion.id) not in seen and not seen.add((r.parte.code, r.seccion.id))
        ]
        return retrieval


class ReferenceFollower:
    """`follow_reference` for tree search over one Parte.

    One hop: `desde` must be a Sección of the searched Parte that was not itself
    reached through this tool, and `hacia` must be what one of its Remisiones names.
    A Remisión to a whole Parte lists that Parte's Secciones and lets the agent read
    one of them, which is still the same hop. Followed Secciones join the Retrieval.
    """

    def __init__(self, searched: ParsedParte, partes: dict[str, ParsedParte], on_progress: Progress = lambda _msg: None):
        self._searched = searched
        self._partes = partes
        self._progress = on_progress
        self.followed: list[RetrievedSeccion] = []
        self.log: list[str] = []

    def follow(self, desde: str, hacia: str) -> str:
        code = self._searched.code
        m = _SID.fullmatch(desde.strip())
        desde = f"{m.group(1)}.{m.group(2)}" if m else desde.strip()
        try:
            source = self._searched.seccion(desde)
        except KeyError:
            return f"La Sección {desde} no es de la RAAC Parte {code}; desde tiene que ser una Sección de esa Parte."
        if any(r.parte.code == code and r.seccion.id == desde for r in self.followed):
            return f"La Sección {desde} se leyó siguiendo una Remisión; solo se sigue un salto."

        target = hacia.strip()
        if m := _SID.fullmatch(target):
            to_parte, to_seccion = m.group(1), f"{m.group(1)}.{m.group(2)}"
        elif m := _PARTE.fullmatch(target):
            to_parte, to_seccion = m.group(1).upper(), None
        else:
            return f"No entiendo hacia={hacia!r}: usá un id de Sección ('61.520') o un código de Parte ('67')."
        if not any(
            r.to_parte == to_parte and (to_seccion is None or r.to_seccion in (None, to_seccion))
            for r in source.remisiones
        ):
            named = ", ".join(r.to_seccion or f"Parte {r.to_parte}" for r in source.remisiones) or "ninguna"
            return f"La Sección {desde} no remite a {target}. Sus Remisiones: {named}."
        parte = self._partes.get(to_parte)
        if parte is None:
            return f"La RAAC Parte {to_parte} no está cargada; no se puede leer."
        if to_seccion is None:
            listing = "\n".join(f"{s.id} {s.title}" for s in parte.secciones)
            return (
                f"RAAC Parte {to_parte}, Secciones:\n{listing}\n\n"
                f"Para leer una, llamá follow_reference(desde='{desde}', hacia='<id de la Sección>')."
            )
        try:
            seccion = parte.seccion(to_seccion)
        except KeyError:
            return f"La Sección {to_seccion} no está en la RAAC Parte {to_parte}."

        self._progress(strings.PROGRESS_FOLLOWING.format(parte=to_parte, seccion=to_seccion))
        self.log.append(f"{code}:{desde} -> {to_parte}:{to_seccion}")
        if not any(r.parte is parte and r.seccion is seccion for r in self.followed):
            self.followed.append(RetrievedSeccion(parte, seccion))
        pages = f"{seccion.pdf_page_start}-{seccion.pdf_page_end}"
        return f"RAAC Parte {to_parte} - Sección {seccion.id} {seccion.title} (páginas PDF {pages})\n\n{seccion.text}"


Search = Callable[[PageIndexClient, str, str, "ReferenceFollower"], Iterable[dict[str, Any]]]


def search_events(
    client: PageIndexClient, question: str, doc_id: str, follower: ReferenceFollower
) -> Iterator[dict[str, Any]]:
    """Run PageIndex's local chat agent, as `client.chat(stream=True).events` does, plus `follow_reference`.

    `chat()` takes no extra tools, so this builds the same agent through PageIndex's
    internal helpers (pinned in uv.lock) and appends ours.
    """
    from agents import function_tool
    from pageindex import local_chat

    messages = [{"role": "system", "content": _SEARCH_INSTRUCTIONS}, {"role": "user", "content": question}]
    agent, items, _ = local_chat._chat_agent(client, messages, doc_id, None)
    agent.tools = [*agent.tools, follow_reference_tool(follower, function_tool)]
    run_kwargs = local_chat._run_kwargs(None)
    return local_chat._stream_sync(lambda: local_chat._chat_events_agen(client, agent, items, run_kwargs))


def follow_reference_tool(follower: ReferenceFollower, function_tool: Callable[..., Any]) -> Any:
    def follow_reference(desde: str, hacia: str) -> str:
        return follower.follow(desde, hacia)

    return function_tool(follow_reference, name_override="follow_reference", description_override=_FOLLOW_DESCRIPTION)


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
