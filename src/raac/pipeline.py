"""Pipeline: question -> retrieved Secciones + Answer, the seam the eval harness runs against.

The local pipeline is CorpusSource + ParteParser + Indexer + Retriever + Answerer. Another
implementation (e.g. a PageIndex Cloud adapter) only has to satisfy `Pipeline`.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import anthropic

from . import corpus, indexer, strings
from .answerer import Answer, answer
from .config import Models, load_models
from .parser import ParsedParte, parse
from .retriever import Retriever


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


@dataclass
class PipelineResult:
    routed_partes: list[str]
    retrieved_secciones: list[SeccionRef]
    answer: Answer


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
    ):
        self._progress = on_progress
        self._retriever = retriever
        self._client = client
        self._models = models
        self._partes = partes
        self._source_urls = source_urls

    def models(self) -> dict[str, str]:
        return {"indexing": self._models.indexing, "search": self._models.search, "answer": self._models.answer}

    def index_versions(self) -> list[IndexVersion]:
        return [IndexVersion(p.code, p.content_hash, p.edicion, p.enmienda) for p in self._partes]

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
            retrieved_secciones=[SeccionRef(r.parte.code, r.seccion.id) for r in retrieval.secciones],
            answer=result,
        )


def build_local_pipeline(
    partes: list[str],
    cache_dir: Path,
    on_progress: Callable[[str], None] = lambda _msg: None,
) -> LocalPipeline:
    """Download, parse and index each Parte (cached by content hash), then wire the pipeline."""
    models = load_models()
    cache_dir.mkdir(parents=True, exist_ok=True)
    storage = cache_dir / "pageindex"
    storage.mkdir(exist_ok=True)
    client = indexer.pageindex_client(models, storage)
    loaded = []
    source_urls = {}
    for code in partes:
        on_progress(strings.PROGRESS_DOWNLOADING.format(parte=code))
        listing = corpus.fetch_listing(code)
        pdf = corpus.download(listing)
        pdf_path = cache_dir / f"raac-{listing.parte}-{pdf.sha256[:16]}.pdf"
        pdf_path.write_bytes(pdf.data)
        parsed = parse(pdf.data)
        on_progress(strings.PROGRESS_INDEXING.format(parte=parsed.code))
        loaded.append((parsed, indexer.index(parsed, pdf_path, client, storage)))
        source_urls[parsed.code] = listing.share_url
    return LocalPipeline(
        Retriever(client, loaded, on_progress=on_progress),
        anthropic.Anthropic(),
        models,
        [p for p, _ in loaded],
        source_urls,
        on_progress,
    )
