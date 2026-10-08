"""Pipeline: question -> retrieved Secciones + Answer, the seam the eval harness runs against.

The local pipeline is CorpusSource + ParteParser + Indexer + Retriever + Answerer. Another
implementation (e.g. a PageIndex Cloud adapter) only has to satisfy `Pipeline`. A `StagedPipeline`
can also run its two stages alone: `retrieve` (routing + tree search) and `answer` (Answerer over
given Secciones and Definiciones), so per-stage evals can replay a recorded Retrieval without
searching again.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import anthropic

from . import corpus, indexer, strings
from .answerer import Answer, Sentence
from .answerer import answer as write_answer
from .config import Models, load_models
from .parser import ParsedParte, parse
from .retriever import Retrieval, RetrievedDefinicion, RetrievedSeccion, Retriever
from .router import ParteRouter, parte_card
from .usage import StageUsage, UsageMeter

DEFAULT_PARTES = ["1", "61", "67", "91"]


@dataclass(frozen=True)
class SeccionRef:
    parte: str
    seccion: str


@dataclass(frozen=True)
class DefinicionRef:
    parte: str
    seccion: str
    term: str


@dataclass(frozen=True)
class IndexVersion:
    parte: str
    content_hash: str
    edicion: str | None = None
    enmienda: str | None = None
    from_cache: bool = False  # ANAC was unreachable or the run was offline; the content hash is the version either way


@dataclass
class PipelineRetrieval:
    routed_partes: list[str]
    retrieved_secciones: list[SeccionRef]
    visited_nodes: list[str] = field(default_factory=list)  # "<parte>:<node_id>"
    definiciones: list[DefinicionRef] = field(default_factory=list)  # Parte 1 Definiciones given as context


@dataclass
class PipelineResult:
    routed_partes: list[str]
    retrieved_secciones: list[SeccionRef]
    answer: Answer
    visited_nodes: list[str] = field(default_factory=list)  # "<parte>:<node_id>"
    definiciones: list[DefinicionRef] = field(default_factory=list)  # Parte 1 Definiciones given as context
    usage: dict[str, StageUsage] = field(default_factory=dict)  # per stage: routing, search, answer


class Pipeline(Protocol):
    name: str

    def models(self) -> dict[str, str]:
        """Model ID per stage, as configured."""
        ...

    def index_versions(self) -> list[IndexVersion]: ...

    def run(self, standalone_question: str) -> PipelineResult:
        """On failure, the raised exception may carry the usage spent so far as `.usage`."""
        ...


class StagedPipeline(Pipeline, Protocol):
    def retrieve(self, standalone_question: str) -> PipelineRetrieval:
        """Routing + tree search only; no answer-model call."""
        ...

    def answer(
        self, standalone_question: str, secciones: list[SeccionRef], definiciones: list[DefinicionRef] | None = None
    ) -> Answer:
        """The Answerer over these Secciones, in this order; no routing or search call."""
        ...

    def take_usage(self) -> dict[str, StageUsage]:
        """Usage per stage recorded since the last take, including by a call that raised."""
        ...


class AnswerOnlyError(RuntimeError):
    pass


class LocalPipeline:
    name = "local"

    def __init__(
        self,
        retriever: Retriever | None,
        client: anthropic.Anthropic,
        models: Models,
        partes: list[ParsedParte],
        source_urls: dict[str, str],
        on_progress: Callable[[str], None] = lambda _msg: None,
        router: ParteRouter | None = None,
        from_cache: frozenset[str] = frozenset(),
        meter: UsageMeter | None = None,
    ):
        self._meter = meter or UsageMeter()  # shared with the router and Retriever that record into it
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

    def run(
        self,
        standalone_question: str,
        record_path: Path | None = None,
        on_sentence: Callable[[Sentence], None] | None = None,
    ) -> PipelineResult:
        self._meter.take()  # drop anything recorded outside a run, e.g. by `raac route`
        try:
            retrieval = self._retrieve(standalone_question)
            result = self._answer(standalone_question, retrieval, record_path, on_sentence)
        except Exception as e:
            e.usage = self._meter.take()  # what the failed exchange already cost
            raise
        return PipelineResult(
            routed_partes=retrieval.routed_partes,
            visited_nodes=retrieval.visited_nodes,
            retrieved_secciones=_refs(retrieval),
            definiciones=_definicion_refs(retrieval),
            answer=result,
            usage=self._meter.take(),
        )

    def retrieve(self, standalone_question: str) -> PipelineRetrieval:
        retrieval = self._retrieve(standalone_question)
        return PipelineRetrieval(
            retrieval.routed_partes, _refs(retrieval), retrieval.visited_nodes, _definicion_refs(retrieval)
        )

    def answer(
        self, standalone_question: str, secciones: list[SeccionRef], definiciones: list[DefinicionRef] | None = None
    ) -> Answer:
        partes = {p.code: p for p in self._partes}

        def loaded(parte: str, seccion: str) -> ParsedParte:
            if parte not in partes:
                raise KeyError(f"Parte {parte} is not loaded; cannot answer from Sección {seccion}")
            return partes[parte]

        retrieved = []
        for ref in secciones:
            parte = loaded(ref.parte, ref.seccion)
            retrieved.append(RetrievedSeccion(parte, parte.seccion(ref.seccion)))
        attached = []
        for ref in definiciones or []:
            parte = loaded(ref.parte, ref.seccion)
            match = [d for d in parte.definiciones if d.seccion_id == ref.seccion and d.term == ref.term]
            if not match:
                raise KeyError(f"Parte {ref.parte} has no Definición {ref.term!r} in Sección {ref.seccion}")
            attached.append(RetrievedDefinicion(parte, match[0]))
        return self._answer(standalone_question, Retrieval(routed_partes=[], secciones=retrieved, definiciones=attached))

    def take_usage(self) -> dict[str, StageUsage]:
        return self._meter.take()

    def _retrieve(self, standalone_question: str) -> Retrieval:
        if self._retriever is None:
            raise AnswerOnlyError("this pipeline was built for the answer stage only; it has no Retriever")
        return self._retriever.retrieve(standalone_question)

    def _answer(
        self,
        standalone_question: str,
        retrieval: Retrieval,
        record_path: Path | None = None,
        on_sentence: Callable[[Sentence], None] | None = None,
    ) -> Answer:
        self._progress(strings.PROGRESS_ANSWERING)
        return write_answer(
            self._client,
            self._models.answer,
            standalone_question,
            retrieval,
            source_urls=self._source_urls,
            record_path=record_path,
            meter=self._meter,
            on_sentence=on_sentence,
        )


def _refs(retrieval: Retrieval) -> list[SeccionRef]:
    return [SeccionRef(r.parte.code, r.seccion.id) for r in retrieval.secciones]


def _definicion_refs(retrieval: Retrieval) -> list[DefinicionRef]:
    return [DefinicionRef(d.parte.code, d.definicion.seccion_id, d.definicion.term) for d in retrieval.definiciones]


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
    meter = UsageMeter()
    router = ParteRouter(claude, models.routing, cards, record_path=routing_record_path, meter=meter)
    return LocalPipeline(
        Retriever(client, loaded, router=router, on_progress=on_progress, meter=meter),
        claude,
        models,
        [p for p, _ in loaded],
        source_urls,
        on_progress,
        router=router,
        from_cache=frozenset(s.listing.parte for s in sourced if s.from_cache),
        meter=meter,
    )


def build_answer_pipeline(
    versions: list[IndexVersion],
    cache_dir: Path,
    on_progress: Callable[[str], None] = lambda _msg: None,
) -> LocalPipeline:
    """A pipeline that can only answer, over exactly these Parte versions: no index, router or tree search.

    Each PDF comes from the cache when present, else is downloaded; either way its content hash
    must match, or the recorded Secciones may not be the ones this PDF holds.
    """
    models = load_models()
    cache_dir.mkdir(parents=True, exist_ok=True)
    wanted = {v.parte: v.content_hash for v in versions}
    loaded = []
    source_urls = {}
    for listing in corpus.fetch_listings(list(wanted)):
        content_hash = wanted[listing.parte]
        pdf_path = cache_dir / f"raac-{listing.parte}-{content_hash[:16]}.pdf"
        if pdf_path.exists():
            data = pdf_path.read_bytes()
        else:
            on_progress(strings.PROGRESS_DOWNLOADING.format(parte=listing.parte))
            data = corpus.download(listing).data
        parsed = parse(data)
        if parsed.content_hash != content_hash:
            raise corpus.CorpusError(
                f"Parte {listing.parte}: recorded version {content_hash[:16]} is not available "
                f"(got {parsed.content_hash[:16]}); re-run the retrieval stage against the current PDF"
            )
        pdf_path.write_bytes(data)
        loaded.append(parsed)
        source_urls[parsed.code] = listing.share_url
    return LocalPipeline(None, anthropic.Anthropic(), models, loaded, source_urls, on_progress)
