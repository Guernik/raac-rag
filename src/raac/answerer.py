"""Answerer: Standalone question + Retrieval -> Answer, via the Claude Citations API (ADR 0002).

Each retrieved Sección is one custom-content document whose content blocks are
its text per PDF page, so a `content_block_location` citation maps straight to
Sección + PDF pages. Citation fields are joined from the API output and the
ParsedParte; nothing is parsed from model prose.

The API returns each cited span as its own text block and puts the surrounding
prose, sentence ends included, in uncited blocks. So sentences are split by
character position over the whole text (never inside a cited span), and each
sentence takes the Citations of the spans it contains. Sentences without a
Citation are dropped; an Answer with none left is a refusal.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import anthropic

from .retriever import Retrieval, RetrievedSeccion

SYSTEM = (
    "Respondés preguntas sobre las Regulaciones Argentinas de Aviación Civil (RAAC) "
    "usando únicamente las Secciones provistas como documentos. Escribí en español "
    "rioplatense claro, en pocas oraciones de prosa simple, sin markdown (sin negritas, "
    "títulos ni listas). Cada oración debe apoyarse en el texto de los documentos; si "
    "dependen de plazos o regímenes transitorios, mencionalos. "
    "Si los documentos no alcanzan para responder, decilo en una oración sin agregar "
    "nada de conocimiento propio."
)
# A sentence ends at terminal punctuation (optionally closing an emphasis) followed by
# whitespace, so "61.520" does not split, or at a line break.
_SENTENCE_END = re.compile(r"[.!?:;](?:\*\*|__)?(?=\s|$)|\n")
# Markdown the model may still emit: emphasis markers and list bullets/numbers.
_MARKUP = re.compile(r"\*\*|__|^\s*(?:[-*•]|\d+[.)])\s+")
_LETTER = re.compile(r"[^\W\d_]")


@dataclass
class Citation:
    parte: str
    seccion: str
    seccion_title: str
    pdf_page_start: int
    pdf_page_end: int
    printed_page_start: str | None
    printed_page_end: str | None
    edicion: str
    enmienda: str
    source_url: str
    cited_text: str


@dataclass
class Sentence:
    text: str
    citations: list[Citation]


@dataclass
class Answer:
    sentences: list[Sentence]
    refused: bool
    dropped_uncited: list[str] = field(default_factory=list)
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_documents(retrieval: Retrieval) -> list[dict[str, Any]]:
    return [
        {
            "type": "document",
            "source": {
                "type": "content",
                "content": [{"type": "text", "text": page.text} for page in r.seccion.pages],
            },
            "title": f"RAAC Parte {r.parte.code} - Sección {r.seccion.id} {r.seccion.title}",
            "context": f"Edición {r.parte.edicion}, Enmienda {r.parte.enmienda}",
            "citations": {"enabled": True},
        }
        for r in retrieval.secciones
    ]


def answer(
    client: anthropic.Anthropic,
    model: str,
    standalone_question: str,
    retrieval: Retrieval,
    source_urls: dict[str, str],
    record_path: Path | None = None,
) -> Answer:
    if not retrieval.secciones:
        return Answer(sentences=[], refused=True)
    with client.beta.messages.stream(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[
            {
                "role": "user",
                "content": [*build_documents(retrieval), {"type": "text", "text": standalone_question}],
            }
        ],
    ) as stream:
        message = stream.get_final_message()
    response = message.model_dump(mode="json")
    if record_path is not None:
        record = {
            "question": standalone_question,
            # Document order, so a replay can rebuild the documents the citation indices point into.
            "secciones": [{"parte": r.parte.code, "seccion": r.seccion.id} for r in retrieval.secciones],
            "response": response,
        }
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    return map_response(response, retrieval, source_urls)


def map_response(response: dict[str, Any], retrieval: Retrieval, source_urls: dict[str, str]) -> Answer:
    """Turn a Messages API response (as JSON) into an Answer with Citations as data."""
    if response.get("stop_reason") == "refusal":
        return Answer(sentences=[], refused=True, model=response.get("model"))
    full = ""
    spans: list[tuple[int, int, list[Citation]]] = []  # cited [start, end) offsets into full
    for block in response.get("content", []):
        if block.get("type") != "text":
            continue
        if block.get("citations"):
            cites = [_map_citation(c, retrieval, source_urls) for c in block["citations"]]
            spans.append((len(full), len(full) + len(block["text"]), cites))
        full += block["text"]

    def splits_a_span(cut: int) -> bool:
        return any(start < cut < end for start, end, _ in spans)

    cuts = [m.end() for m in _SENTENCE_END.finditer(full) if not splits_a_span(m.end())]
    sentences: list[Sentence] = []
    dropped: list[str] = []
    for start, end in zip([0, *cuts], [*cuts, len(full)]):
        text = _MARKUP.sub("", full[start:end]).strip()
        if not _LETTER.search(text):
            continue  # list numbers, stray markup, blank lines
        cites = [c for s_start, s_end, span_cites in spans if start <= s_start and s_end <= end for c in span_cites]
        if cites:
            sentences.append(Sentence(text=_capitalize(text), citations=cites))
        else:
            dropped.append(text)
    return Answer(sentences=sentences, refused=not sentences, dropped_uncited=dropped, model=response.get("model"))


def _capitalize(text: str) -> str:
    """Uppercase the first letter, e.g. when a dropped uncited lead-in ("Hay un régimen transitorio:") preceded it."""
    m = _LETTER.search(text)
    return text if m is None else text[: m.start()] + text[m.start()].upper() + text[m.start() + 1 :]


def _map_citation(raw: dict[str, Any], retrieval: Retrieval, source_urls: dict[str, str]) -> Citation:
    if raw.get("type") != "content_block_location":
        raise ValueError(f"Unexpected citation type {raw.get('type')!r}")
    r: RetrievedSeccion = retrieval.secciones[raw["document_index"]]
    pages = r.seccion.pages[raw["start_block_index"] : raw["end_block_index"]]
    if not pages:
        raise ValueError(f"Citation block range out of bounds for Sección {r.seccion.id}: {raw}")
    return Citation(
        parte=r.parte.code,
        seccion=r.seccion.id,
        seccion_title=r.seccion.title,
        pdf_page_start=pages[0].pdf_page,
        pdf_page_end=pages[-1].pdf_page,
        printed_page_start=pages[0].printed_page,
        printed_page_end=pages[-1].printed_page,
        edicion=r.parte.edicion,
        enmienda=r.parte.enmienda,
        source_url=source_urls[r.parte.code],
        cited_text=raw["cited_text"],
    )
