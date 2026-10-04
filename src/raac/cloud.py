"""PageIndex Cloud pipeline: the baseline the local pipeline is compared against (ADR 0001).

The same Parte PDFs the local pipeline indexes are uploaded to PageIndex Cloud, and each
question goes to its managed chat scoped to those documents with citations enabled. The
answer comes back as prose with inline page-level (or block-level) tags such as
`<doc=raac-61.pdf;page=67;block=p67_text_3>`. Each tag is resolved with the SDK and joined
with our own ParsedParte, so Cloud Citations have the same shape as local ones: Parte,
Sección, PDF page, printed page and the cited page's footer Edición/Enmienda (ADR 0004).

Cloud does not expose its retrieval, so the Secciones it cites stand in for the retrieved
Secciones, and the Partes it cites for the routed Partes. A sentence with no tag is an
uncited sentence, scored the same way as the local Answerer's dropped ones.
"""

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import corpus, strings
from .answerer import _SENTENCE_END, Answer, Citation, Sentence, _page_version
from .parser import ParsedParte, Seccion, parse
from .pipeline import IndexVersion, PipelineResult, SeccionRef

KEY_ENV = "PAGEINDEX_API_KEY"
MODEL = "pageindex-cloud"  # Cloud picks its own model; this names the managed chat in reports
_MANIFEST = "pageindex-cloud.json"  # sha256 -> doc_id, so a PDF is uploaded once

# The two tag formats PageIndex chat writes: <doc=...;page=...;block=...> and <cite doc="..." page="..."/>.
# A <cite ...>claim</cite> tag keeps its claim in the prose and cites at the closing tag.
_TAG_RE = re.compile(r"<doc=([^;<>]+);page=(\d+)(?:-\d+)?(?:;block(?:_id)?=([^;<>]+))?>|<cite\s([^<>]*?)(/?)>|</cite>")
_ATTR_RE = re.compile(r"""\b(\w+)=(["'])(.*?)\2""", re.S)
_LETTER = re.compile(r"[^\W\d_]")
_SPACE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([.,;:!?])")


class CloudKeyMissing(RuntimeError):
    pass


def require_key() -> str:
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise CloudKeyMissing(
            f"{KEY_ENV} is not set. The PageIndex Cloud pipeline needs a PageIndex API key: "
            f"export {KEY_ENV}=... or add it to .env."
        )
    return key


class CloudClient(Protocol):
    """The slice of pageindex.PageIndexCloudClient this pipeline uses."""

    def chat_completions(self, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]: ...

    def get_citations(self, answer: str, doc_id: list[str] | None = None) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class CloudDoc:
    doc_id: str
    parte: ParsedParte
    source_url: str
    from_cache: bool = False  # the PDF came from the local cache, not ANAC


class CloudPipeline:
    name = "pageindex-cloud"

    def __init__(self, client: CloudClient, docs: list[CloudDoc], on_progress: Callable[[str], None] = lambda _msg: None):
        self._client = client
        self._docs = {d.doc_id: d for d in docs}
        self._progress = on_progress

    def models(self) -> dict[str, str]:
        return {"chat": MODEL}

    def index_versions(self) -> list[IndexVersion]:
        return [
            IndexVersion(d.parte.code, d.parte.content_hash, d.parte.edicion, d.parte.enmienda, from_cache=d.from_cache)
            for d in self._docs.values()
        ]

    def run(self, standalone_question: str) -> PipelineResult:
        self._progress(strings.PROGRESS_CLOUD)
        doc_ids = list(self._docs)
        response = self._client.chat_completions(
            messages=[{"role": "user", "content": standalone_question}],
            doc_id=doc_ids,
            enable_citations=True,
        )
        text = response["choices"][0]["message"]["content"] or ""
        resolved = self._client.get_citations(text, doc_id=doc_ids) if _TAG_RE.search(text) else []
        answer = map_answer(text, resolved, self._docs, model=response.get("model") or MODEL)
        cited: list[SeccionRef] = []
        for sentence in answer.sentences:
            for c in sentence.citations:
                if (ref := SeccionRef(c.parte, c.seccion)) not in cited:
                    cited.append(ref)
        return PipelineResult(
            routed_partes=list(dict.fromkeys(r.parte for r in cited)),
            retrieved_secciones=cited,
            answer=answer,
        )


def map_answer(text: str, resolved: list[dict[str, Any]], docs: dict[str, CloudDoc], model: str | None = None) -> Answer:
    """Split a Cloud answer into sentences and give each the Citations of the tags it carries.

    A tag belongs to the sentence it ends: "... de noche. <doc=...>" cites "... de noche.".
    Tags that name no uploaded document or no Sección are left out, so a sentence citing only
    those counts as uncited.
    """
    by_key = {_key(r.get("document"), r.get("page"), r.get("block_id")): r for r in resolved}
    prose = ""
    tags: list[tuple[int, dict[str, Any] | None]] = []  # (offset into prose, resolved citation)
    last = 0
    open_cites: list[tuple[str, int, str | None]] = []
    for m in _TAG_RE.finditer(text):
        prose += text[last : m.start()]
        last = m.end()
        if m.group(0) == "</cite>":
            if open_cites:
                tags.append((len(prose.rstrip()), by_key.get(open_cites.pop())))
        elif m.group(4) is not None and not m.group(5):
            open_cites.append(_tag_key(m))
        else:
            tags.append((len(prose.rstrip()), by_key.get(_tag_key(m))))
    prose += text[last:]
    tags += [(len(prose.rstrip()), by_key.get(k)) for k in open_cites]  # unclosed: cite where the text ends

    cuts = sorted({m.end() for m in _SENTENCE_END.finditer(prose)})
    sentences: list[Sentence] = []
    dropped: list[str] = []
    for start, end in zip([0, *cuts], [*cuts, len(prose)]):
        piece = _SPACE_BEFORE_PUNCT.sub(r"\1", prose[start:end]).strip()  # where a tag sat before the period
        if not _LETTER.search(piece):
            continue
        cites: list[Citation] = []
        for offset, r in tags:
            if start < offset <= end or (start == offset == 0):
                for c in map_citation(r, docs) if r else []:
                    if c not in cites:
                        cites.append(c)
        if cites:
            sentences.append(Sentence(text=piece, citations=cites))
        else:
            dropped.append(piece)
    return Answer(sentences=sentences, refused=not sentences, dropped_uncited=dropped, model=model)


def map_citation(resolved: dict[str, Any], docs: dict[str, CloudDoc]) -> list[Citation]:
    """One resolved Cloud citation -> Citations in our shape, one per Sección it can be pinned to.

    A block citation carries the block's text, which picks the Sección on that page that
    contains it. A page citation (or a block whose text matches none) cites every Sección
    on the page, since Cloud gives nothing finer.
    """
    doc = docs.get(resolved.get("doc_id") or "")
    page = resolved.get("page")
    if doc is None or not isinstance(page, int) or not 1 <= page <= doc.parte.page_count:
        return []
    on_page = doc.parte.secciones_on_pages({page})
    block_text = (resolved.get("text") or "").strip()
    if block_text:
        matching = [s for s in on_page if _contains(_page_of(s, page).text, block_text)]
        on_page = matching or on_page
    return [
        Citation(
            parte=doc.parte.code,
            seccion=s.id,
            seccion_title=s.title,
            pdf_page_start=page,
            pdf_page_end=page,
            printed_page_start=_page_of(s, page).printed_page,
            printed_page_end=_page_of(s, page).printed_page,
            **_page_version(doc.parte, page),
            source_url=doc.source_url,
            cited_text=block_text or _page_of(s, page).text,
        )
        for s in on_page
    ]


def build_cloud_pipeline(
    partes: list[str],
    cache_dir: Path,
    on_progress: Callable[[str], None] = lambda _msg: None,
    client: Any = None,
    offline: bool = False,
) -> CloudPipeline:
    """Upload each Parte's PDF to PageIndex Cloud once (by content hash) and wire the pipeline."""
    key = require_key()  # before any download, so a missing key fails at once
    if client is None:
        from pageindex import PageIndexCloudClient

        client = PageIndexCloudClient(api_key=key)
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = cache_dir / _MANIFEST
    manifest: dict[str, str] = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    docs = []
    for sourced in corpus.load_partes(partes, corpus.CorpusStore(cache_dir / "corpus"), offline, on_progress):
        listing, pdf = sourced.listing, sourced.pdf
        parsed = parse(pdf.data)
        doc_id = manifest.get(pdf.sha256)
        if doc_id is None or not _usable(client, doc_id):
            on_progress(strings.PROGRESS_UPLOADING.format(parte=parsed.code))
            pdf_path = cache_dir / f"raac-{listing.parte}-{pdf.sha256[:16]}.pdf"
            pdf_path.write_bytes(pdf.data)
            submitted = client.submit_document(
                str(pdf_path),
                beta_headers=["block_reference"],
                metadata={"parte": parsed.code, "sha256": pdf.sha256},
                wait=True,
            )
            doc_id = submitted["doc_id"]
            manifest[pdf.sha256] = doc_id
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        docs.append(CloudDoc(doc_id, parsed, listing.share_url, sourced.from_cache))
    return CloudPipeline(client, docs, on_progress)


def _usable(client: Any, doc_id: str) -> bool:
    from pageindex import PageIndexAPIError

    try:
        return client.get_document(doc_id).get("status") == "completed"
    except PageIndexAPIError as e:
        if e.status_code == 404:
            return False
        raise


def _tag_key(m: re.Match) -> tuple[str, int, str | None]:
    if m.group(1) is not None:
        return _key(m.group(1), m.group(2), m.group(3))
    attrs = {name: value for name, _, value in _ATTR_RE.findall(m.group(4))}
    return _key(attrs.get("doc"), attrs.get("page", "").split("-")[0], attrs.get("block"))


def _key(document: Any, page: Any, block_id: Any) -> tuple[str, int, str | None]:
    try:
        page_n = int(page)
    except (TypeError, ValueError):
        page_n = 0
    return (str(document or "").strip(), page_n, (str(block_id).strip() or None) if block_id else None)


def _page_of(seccion: Seccion, pdf_page: int):
    return next(p for p in seccion.pages if p.pdf_page == pdf_page)


def _contains(page_text: str, block_text: str) -> bool:
    """Whether the block's opening words appear in the page text, ignoring case and line breaks."""
    needle = _SPACE.sub(" ", block_text).strip().lower()[:80]
    return bool(needle) and needle in _SPACE.sub(" ", page_text).lower()
