"""Answerer: Standalone question + Retrieval -> Answer, via the Claude Citations API (ADR 0002).

Each retrieved Sección is one custom-content document whose content blocks are
its text per PDF page, so a `content_block_location` citation maps straight to
Sección + PDF pages. Citation fields are joined from the API output and the
ParsedParte; nothing is parsed from model prose. Sentences without a Citation
are dropped; an Answer with none left is a refusal.
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
    "rioplatense claro, en pocas oraciones. Cada afirmación debe apoyarse en el texto "
    "de los documentos; si dependen de plazos o regímenes transitorios, mencionalos. "
    "Si los documentos no alcanzan para responder, decilo en una oración sin agregar "
    "nada de conocimiento propio."
)
_SENTENCE_END = re.compile(r"([.!?:;]\s*|\n\s*)$")


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
        record_path.write_text(json.dumps(response, ensure_ascii=False, indent=2))
    return map_response(response, retrieval, source_urls)


def map_response(response: dict[str, Any], retrieval: Retrieval, source_urls: dict[str, str]) -> Answer:
    """Turn a Messages API response (as JSON) into an Answer with Citations as data."""
    sentences: list[Sentence] = []
    dropped: list[str] = []
    text, cites = "", []

    def flush() -> None:
        nonlocal text, cites
        if text.strip():
            if cites:
                sentences.append(Sentence(text=text.strip(), citations=cites))
            else:
                dropped.append(text.strip())
        text, cites = "", []

    if response.get("stop_reason") == "refusal":
        return Answer(sentences=[], refused=True, model=response.get("model"))
    for block in response.get("content", []):
        if block.get("type") != "text":
            continue
        text += block["text"]
        cites += [_map_citation(c, retrieval, source_urls) for c in block.get("citations") or []]
        if _SENTENCE_END.search(block["text"]):
            flush()
    flush()
    return Answer(sentences=sentences, refused=not sentences, dropped_uncited=dropped, model=response.get("model"))


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
