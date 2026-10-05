"""Pipeline: question -> retrieved Secciones + Answer, the seam the eval harness runs against.

The local pipeline is CorpusSource + ParteParser + Indexer + Retriever + Answerer. Another
implementation (e.g. a PageIndex Cloud adapter) only has to satisfy `Pipeline`.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import anthropic

from . import corpus, indexer, strings
from .answerer import Answer, answer
from .config import Models, load_models
from .parser import ParsedParte, parse
from .retriever import Retriever
from .router import ParteRouter, parte_card

DEFAULT_PARTES = ["1", "61", "67", "91"]


@dataclass(frozen=True)
class SeccionRef:
    parte: str
    seccion: str


@dataclass(frozen=True)
class IndexVersion:
    parte: str
    content_hash: str
    edicion: str | None = None
    enmienda: str | None = None
    from_cache: bool = False  # ANAC was unreachable or the run was offline; the content hash is the version either way


@dataclass
class PipelineResult:
    routed_partes: list[str]
    retrieved_secciones: list[SeccionRef]
    answer: Answer
    visited_nodes: list[str] = field(default_factory=list)  # "<parte>:<node_id>"


class Pipeline(Protocol):
    name: str

    def models(self) -> dict[str, str]:
        """Model ID per stage, as configured."""
        ...

    def index_versions(self) -> list[IndexVersion]: ...

    def run(self, standalone_question: str) -> PipelineResult: ...


class LocalPipeline:
    name = "local"

    def __init__(
        self,
        retriever: Retriever,
        client: anthropic.Anthropic,
        models: Models,
        partes: list[ParsedParte],
        source_urls: dict[str, str],
        on_progress: Callable[[str], None] = lambda _msg: None,
        router: ParteRouter | None = None,
        from_cache: frozenset[str] = frozenset(),
    ):
        self._progress = on_progress
        self._retriever = retriever
        self.router = router
        self._client = client
        self._models = models
        self._partes = partes
        self._source_urls = source_urls
        self._from_cache = from_cache

    def models(self) -> dict[str, str]:
        return {
            "indexing": self._models.indexing,
            "routing": self._models.routing,
            "search": self._models.search,
            "answer": self._models.answer,
        }

    def index_versions(self) -> list[IndexVersion]:
        return [
            IndexVersion(p.code, p.content_hash, p.edicion, p.enmienda, from_cache=p.code in self._from_cache)
            for p in self._partes
        ]

    def run(self, standalone_question: str, record_path: Path | None = None) -> PipelineResult:
        retrieval = self._retriever.retrieve(standalone_question)
        self._progress(strings.PROGRESS_ANSWERING)
        result = answer(
            self._client,
            self._models.answer,
            standalone_question,
            retrieval,
            source_urls=self._source_urls,
            record_path=record_path,
        )
        return PipelineResult(
            routed_partes=retrieval.routed_partes,
            visited_nodes=retrieval.visited_nodes,
            retrieved_secciones=[SeccionRef(r.parte.code, r.seccion.id) for r in retrieval.secciones],
            answer=result,
        )


def build_local_pipeline(
    partes: list[str],
    cache_dir: Path,
    on_progress: Callable[[str], None] = lambda _msg: None,
    routing_record_path: Path | None = None,
    offline: bool = False,
) -> LocalPipeline:
    """Download (or, when ANAC is down or `offline`, load from cache), parse and index each Parte, then wire the pipeline."""
    models = load_models()
    cache_dir.mkdir(parents=True, exist_ok=True)
    storage = cache_dir / "pageindex"
    storage.mkdir(exist_ok=True)
    client = indexer.pageindex_client(models, storage)
    loaded = []
    cards = []
    source_urls = {}
    sourced = corpus.load_partes(partes, corpus.CorpusStore(cache_dir / "corpus"), offline, on_progress)
    for listing, pdf in ((s.listing, s.pdf) for s in sourced):
        pdf_path = cache_dir / f"raac-{listing.parte}-{pdf.sha256[:16]}.pdf"
        pdf_path.write_bytes(pdf.data)
        parsed = parse(pdf.data)
        on_progress(strings.PROGRESS_INDEXING.format(parte=parsed.code))
        index = indexer.index(parsed, pdf_path, client, storage)
        loaded.append((parsed, index))
        cards.append(parte_card(listing.parte, listing.titulo, index))
        source_urls[parsed.code] = listing.share_url
    claude = anthropic.Anthropic()
    router = ParteRouter(claude, models.routing, cards, record_path=routing_record_path)
    return LocalPipeline(
        Retriever(client, loaded, router=router, on_progress=on_progress),
        claude,
        models,
        [p for p, _ in loaded],
        source_urls,
        on_progress,
        router=router,
        from_cache=frozenset(s.listing.parte for s in sourced if s.from_cache),
    )
