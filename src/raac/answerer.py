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

When the Secciones do not cover (part of) the question, the model writes one
uncited line starting with GAP_MARKER. That line is never shown: it only marks
the Answer as incomplete, and the Partes it names are kept as likely Partes when
a retrieved Sección has a Remisión to them, cited from that Remisión's text.

Parte 1 Definiciones attached to the Retrieval follow the Secciones as documents of
their own, so the model can cite one when the Answer relies on the defined meaning.
A Definición the model does not cite never becomes a Citation.
"""

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import anthropic

from . import strings
from .parser import ParsedParte, SeccionPage
from .remisiones import Remision, find_remisiones, parte_codes
from .retriever import Retrieval, RetrievedDefinicion, RetrievedSeccion
from .usage import UsageMeter

GAP_MARKER = "SIN RESPALDO:"

SYSTEM = (
    "Respondés preguntas sobre las Regulaciones Argentinas de Aviación Civil (RAAC) "
    "usando únicamente las Secciones provistas como documentos. Escribí en español "
    "rioplatense claro, en pocas oraciones de prosa simple, sin markdown (sin negritas, "
    "títulos ni listas). Cada oración debe apoyarse en el texto de los documentos; si "
    "dependen de plazos o regímenes transitorios, mencionalos. "
    "No agregues nada de conocimiento propio. Si los documentos no alcanzan para responder "
    "la pregunta, o una parte de ella, terminá con una línea aparte, sin citas, que empiece "
    f"con '{GAP_MARKER}' y diga qué falta; si los documentos remiten a otra Parte de la RAAC "
    "que probablemente lo cubra, nombrala ahí (por ejemplo 'Parte 67'). "
    "Algunos documentos son Definiciones de la Parte 1: usalas para entender los términos "
    "de las Secciones y citalas solo si la respuesta depende del significado definido."
)
# A sentence ends at terminal punctuation (optionally closing an emphasis) followed by
# whitespace, so "61.520" does not split, or at a line break.
_SENTENCE_END = re.compile(r"[.!?:;](?:\*\*|__)?(?=\s|$)|\n")
# Markdown the model may still emit: emphasis markers and list bullets/numbers.
_MARKUP = re.compile(r"\*\*|__|^\s*(?:[-*•]|\d+[.)])\s+")
_LETTER = re.compile(r"[^\W\d_]")
_GAP = re.compile(re.escape(GAP_MARKER) + r"[^\n]*", re.I)


@dataclass
class Citation:
    parte: str
    seccion: str
    seccion_title: str
    pdf_page_start: int
    pdf_page_end: int
    printed_page_start: str | None
    printed_page_end: str | None
    edicion: str | None  # as printed in the footer of the first cited page (ADR 0004)
    enmienda: str | None
    fecha: str | None
    source_url: str
    cited_text: str
    definicion: str | None = None  # the defined term, when the Citation is a Parte 1 Definición


@dataclass
class Sentence:
    text: str
    citations: list[Citation]


@dataclass
class LikelyParte:
    """A Parte that likely covers what the Answer could not, cited from a retrieved Sección's Remisión to it."""

    parte: str
    citation: Citation


@dataclass
class Answer:
    sentences: list[Sentence]  # every one carries at least one Citation
    refused: bool  # no grounded sentence: the Answer says so and cites only likely Partes' Remisiones
    incomplete: bool = False  # grounded sentences, but the model flagged part of the question as uncovered
    likely_partes: list[LikelyParte] = field(default_factory=list)
    gap: str | None = None  # the model's GAP_MARKER line, for logs only; never shown
    dropped_uncited: list[str] = field(default_factory=list)
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_documents(retrieval: Retrieval) -> list[dict[str, Any]]:
    docs = []
    for r in retrieval.secciones:
        doc = {
            "type": "document",
            "source": {
                "type": "content",
                "content": [{"type": "text", "text": page.text} for page in r.seccion.pages],
            },
            "title": f"RAAC Parte {r.parte.code} - Sección {r.seccion.id} {r.seccion.title}",
            "citations": {"enabled": True},
        }
        if context := _footer_context(r.parte, r.seccion.pdf_page_start):
            doc["context"] = context
        docs.append(doc)
    for d in retrieval.definiciones:
        doc = {
            "type": "document",
            "source": {
                "type": "content",
                "content": [{"type": "text", "text": page.text} for page in d.definicion.pages],
            },
            "title": f"RAAC Parte {d.parte.code} - Sección {d.definicion.seccion_id} - Definición: {d.definicion.term}",
            "citations": {"enabled": True},
        }
        if context := _footer_context(d.parte, d.definicion.pages[0].pdf_page):
            doc["context"] = context
        docs.append(doc)
    return docs


def answer(
    client: anthropic.Anthropic,
    model: str,
    standalone_question: str,
    retrieval: Retrieval,
    source_urls: dict[str, str],
    record_path: Path | None = None,
    meter: UsageMeter | None = None,
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
    if meter is not None:
        meter.record_anthropic("answer", response, requested_model=model)
    if record_path is not None:
        record = {
            "question": standalone_question,
            # Document order, so a replay can rebuild the documents the citation indices point into.
            "secciones": [{"parte": r.parte.code, "seccion": r.seccion.id} for r in retrieval.secciones],
            "definiciones": [
                {"parte": d.parte.code, "seccion": d.definicion.seccion_id, "term": d.definicion.term}
                for d in retrieval.definiciones
            ],
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

    gaps = [
        (m.start(), m.end())
        for m in _GAP.finditer(full)
        if not any(m.start() < s_end and s_start < m.end() for s_start, s_end, _ in spans)
    ]
    cuts = sorted(
        {m.end() for m in _SENTENCE_END.finditer(full) if not splits_a_span(m.end())}
        | {cut for gap in gaps for cut in gap}
    )
    sentences: list[Sentence] = []
    dropped: list[str] = []
    for start, end in zip([0, *cuts], [*cuts, len(full)]):
        if any(g_start <= start and end <= g_end for g_start, g_end in gaps):
            continue
        text = _MARKUP.sub("", full[start:end]).strip()
        if not _LETTER.search(text):
            continue  # list numbers, stray markup, blank lines
        cites = [c for s_start, s_end, span_cites in spans if start <= s_start and s_end <= end for c in span_cites]
        if cites:
            sentences.append(Sentence(text=_capitalize(text), citations=cites))
        else:
            dropped.append(text)
    gap = " ".join(full[start:end].strip() for start, end in gaps) or None
    cited = {(c.parte, c.seccion) for s in sentences for c in s.citations}
    return Answer(
        sentences=sentences,
        refused=not sentences,
        incomplete=bool(sentences) and gap is not None,
        likely_partes=_likely_partes(gap, retrieval, cited, source_urls),
        gap=gap,
        dropped_uncited=dropped,
        model=response.get("model"),
    )


def _likely_partes(
    gap: str | None, retrieval: Retrieval, cited: set[tuple[str, str]], source_urls: dict[str, str]
) -> list[LikelyParte]:
    """Partes the gap line names that a retrieved Sección has a Remisión to; cited Secciones first.

    A Parte the model names without a Remisión in the text is not shown: it would be an uncited claim.
    """
    if gap is None:
        return []
    named = parte_codes(gap)
    ordered = sorted(retrieval.secciones, key=lambda r: (r.parte.code, r.seccion.id) not in cited)
    found: dict[str, LikelyParte] = {}
    for r in ordered:
        for remision in find_remisiones(r.seccion, r.parte.code):
            if remision.to_parte in named and remision.to_parte not in found:
                found[remision.to_parte] = LikelyParte(remision.to_parte, _remision_citation(remision, r, source_urls))
    return [found[code] for code in named if code in found]


def _remision_citation(remision: Remision, r: RetrievedSeccion, source_urls: dict[str, str]) -> Citation:
    page = r.seccion.pages[remision.page_index]
    return Citation(
        parte=r.parte.code,
        seccion=r.seccion.id,
        seccion_title=r.seccion.title,
        pdf_page_start=page.pdf_page,
        pdf_page_end=page.pdf_page,
        printed_page_start=page.printed_page,
        printed_page_end=page.printed_page,
        **_page_version(r.parte, page.pdf_page),
        source_url=source_urls[r.parte.code],
        cited_text=remision.text,
    )


def _capitalize(text: str) -> str:
    """Uppercase the first letter, e.g. when a dropped uncited lead-in ("Hay un régimen transitorio:") preceded it."""
    m = _LETTER.search(text)
    return text if m is None else text[: m.start()] + text[m.start()].upper() + text[m.start() + 1 :]


def _map_citation(raw: dict[str, Any], retrieval: Retrieval, source_urls: dict[str, str]) -> Citation:
    if raw.get("type") != "content_block_location":
        raise ValueError(f"Unexpected citation type {raw.get('type')!r}")
    index = raw["document_index"]
    if index >= len(retrieval.secciones):
        d: RetrievedDefinicion = retrieval.definiciones[index - len(retrieval.secciones)]
        parte, seccion_id, all_pages = d.parte, d.definicion.seccion_id, d.definicion.pages
        title, term = strings.DEFINICION_TITLE.format(term=d.definicion.term), d.definicion.term
    else:
        r: RetrievedSeccion = retrieval.secciones[index]
        parte, seccion_id, all_pages = r.parte, r.seccion.id, r.seccion.pages
        title, term = r.seccion.title, None
    pages: list[SeccionPage] = all_pages[raw["start_block_index"] : raw["end_block_index"]]
    if not pages:
        raise ValueError(f"Citation block range out of bounds for Sección {seccion_id}: {raw}")
    return Citation(
        parte=parte.code,
        seccion=seccion_id,
        seccion_title=title,
        pdf_page_start=pages[0].pdf_page,
        pdf_page_end=pages[-1].pdf_page,
        printed_page_start=pages[0].printed_page,
        printed_page_end=pages[-1].printed_page,
        **_page_version(parte, pages[0].pdf_page),
        source_url=source_urls[parte.code],
        cited_text=raw["cited_text"],
        definicion=term,
    )


def _page_version(parte: ParsedParte, pdf_page: int) -> dict[str, str | None]:
    v = parte.page_version(pdf_page)
    return {"edicion": v and v.edicion, "enmienda": v and v.enmienda, "fecha": v and v.fecha}


def _footer_context(parte: ParsedParte, pdf_page: int) -> str:
    v = parte.page_version(pdf_page)
    if v is None:
        return ""
    return ", ".join(x for x in (f"Edición {v.edicion}", v.enmienda and f"Enmienda {v.enmienda}", v.fecha) if x)
