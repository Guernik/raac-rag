"""Answerer: Standalone question + Retrieval -> Answer, via the Claude Citations API (ADR 0002).

Each retrieved Sección is one custom-content document whose content blocks are
its text per PDF page, so a `content_block_location` citation maps straight to
Sección + PDF pages. Citation fields are joined from the API output and the
ParsedParte; nothing is parsed from model prose.

The API returns each cited span as its own text block and puts the surrounding
prose, sentence ends included, in uncited blocks. So sentences are split by
character position over the whole text (never inside a cited span), and each
sentence takes the Citations of the spans it contains. A sentence without a
Citation is Framing (ADR 0003). Framing is checked, not trusted: a sentence, cited
or not, with a number, month or Sección reference absent from the Answer's cited
text is dropped as an unsupported claim. An Answer with no cited sentence left is
a refusal. Line breaks and list markers become each sentence's `starts`, so
clients can show a lead-in followed by one list item per condition.

The answer opens with a coverage line, COVERAGE_MARKER plus total, parcial or
ninguna and what is missing. It is never shown: ninguna, or a missing or
malformed line, makes the Answer a refusal even if cited sentences exist, and
parcial makes it incomplete. The Partes the line names are kept as likely Partes
when a retrieved Sección has a Remisión to them, cited from that Remisión's text.

Parte 1 Definiciones attached to the Retrieval follow the Secciones as documents of
their own, so the model can cite one when the Answer relies on the defined meaning.
A Definición the model does not cite never becomes a Citation.

With `on_sentence`, each grounded sentence is also handed over as soon as the text after it
shows it is complete, while the answer still streams. The returned Answer is authoritative:
it adds the refusal, gap and likely Partes, and a caller shows it in place of what streamed.
"""

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import anthropic

from . import strings
from .parser import ParsedParte, SeccionPage
from .remisiones import Remision, find_remisiones, parte_codes
from .retriever import Retrieval, RetrievedDefinicion, RetrievedSeccion
from .usage import UsageMeter

COVERAGE_MARKER = "COBERTURA:"
COVERAGES = ("total", "parcial", "ninguna")

SYSTEM = (
    "Respondés preguntas sobre las Regulaciones Argentinas de Aviación Civil (RAAC) "
    "usando únicamente las Secciones provistas como documentos. No agregues nada de "
    "conocimiento propio.\n\n"
    f"Empezá siempre con una línea aparte, sin citas: '{COVERAGE_MARKER} total' si los "
    f"documentos alcanzan para responder toda la pregunta, '{COVERAGE_MARKER} parcial - <qué falta>' "
    f"si alcanzan para una parte, o '{COVERAGE_MARKER} ninguna - <qué falta>' si no alcanzan "
    "para nada. Una parte cuenta como cubierta solo si los documentos la responden directamente: "
    "si la pregunta es de teoría o performance (aerodinámica, meteorología, cálculos de la aeronave) "
    "y los documentos solo tocan el tema en reglas que no la responden, es ninguna. Si los documentos remiten a otra Parte de la RAAC que probablemente cubra lo "
    "que falta, nombrala en esa línea (por ejemplo 'Parte 67'). Con ninguna, no escribas nada más.\n\n"
    "Después respondé en español rioplatense, con un voseo natural (podés, tenés que), "
    "parafraseando el texto de las Secciones en palabras simples: la cita ya le muestra al "
    "usuario el texto literal, así que no copies marcadores de incisos ni líneas como "
    "'(A) Reservado', y no fuerces el voseo sobre la redacción legal. Cada requisito, condición, "
    "número, plazo, fecha o referencia a una Sección tiene que ir en una oración citada; si "
    "dependen de plazos o regímenes transitorios, mencionalos. Sin cita solo podés escribir "
    "encuadre: un veredicto breve al comienzo (sí, no o depende) que las oraciones citadas "
    "respalden, frases que introducen una lista y conectores. No apliques las reglas a la "
    "situación particular del usuario, y no hables de los documentos ni de las Secciones provistas: "
    "lo que falta ya va en la línea de cobertura.\n\n"
    "Si la respuesta depende de varias condiciones o requisitos, escribí una frase introductoria "
    "que termine en dos puntos, sin contar los ítems ('estas condiciones:', no 'estas dos condiciones:'), "
    "y después una lista con un ítem por condición, cada uno en su propia línea empezando con '- '. Separá los párrafos con una línea en blanco. No uses "
    "títulos, negritas ni otro markdown.\n\n"
    "Algunos documentos son Definiciones de la Parte 1: usalas para entender los términos "
    "de las Secciones y citalas solo si la respuesta depende del significado definido."
)
# A sentence ends at terminal punctuation (optionally closing an emphasis) followed by
# whitespace, so "61.520" does not split, or at a line break.
_SENTENCE_END = re.compile(r"[.!?:;](?:\*\*|__)?(?=\s|$)|\n")
# Markdown the model may still emit: emphasis markers, headings and list bullets/numbers.
_MARKUP = re.compile(r"\*\*|__|^\s*(?:#+|[-*•]|\d+[.)])\s+")
_BULLET = re.compile(r"(?:[-*•]|\d+[.)])(?=\s|$)")
_LETTER = re.compile(r"[^\W\d_]")
_COVERAGE = re.compile(
    r"\s*(?:\*\*|__)?" + re.escape(COVERAGE_MARKER) + r"(?:\*\*|__)?[ \t]*(\w+)[ \t]*[-–—:.,;]?[ \t]*([^\n]*)", re.I
)
# What makes a sentence a checkable claim: numbers (digits or words), months, Sección references.
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_INCISO = re.compile(r"\((?:[^\W\d_]{1,4}|\d{1,2})\)")  # (a), (1), (iv); "tres (3)" still counts by its word
_WORD = re.compile(r"[^\W\d_]+")
_NUMBER_WORDS = {
    w: n
    for n, words in {
        2: "dos", 3: "tres", 4: "cuatro", 5: "cinco", 6: "seis", 7: "siete", 8: "ocho", 9: "nueve",
        10: "diez", 11: "once", 12: "doce", 13: "trece", 14: "catorce", 15: "quince", 16: "dieciseis",
        17: "diecisiete", 18: "dieciocho", 19: "diecinueve", 20: "veinte", 21: "veintiuno", 22: "veintidos",
        23: "veintitres", 24: "veinticuatro", 25: "veinticinco", 26: "veintiseis", 27: "veintisiete",
        28: "veintiocho", 29: "veintinueve", 30: "treinta", 40: "cuarenta", 50: "cincuenta", 60: "sesenta",
        70: "setenta", 80: "ochenta", 90: "noventa", 100: "cien ciento", 200: "doscientos",
        300: "trescientos", 400: "cuatrocientos", 500: "quinientos", 600: "seiscientos",
        700: "setecientos", 800: "ochocientos", 900: "novecientos", 1000: "mil",
    }.items()
    for w in words.split()
}
_MONTHS = set("enero febrero marzo abril mayo junio julio agosto septiembre setiembre octubre noviembre diciembre".split())

Starts = Literal["paragraph", "item"]


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
    citations: list[Citation]  # empty for Framing
    starts: Starts | None = None  # a new paragraph or list item begins here; None continues the current one


@dataclass
class LikelyParte:
    """A Parte that likely covers what the Answer could not, cited from a retrieved Sección's Remisión to it."""

    parte: str
    citation: Citation


@dataclass
class Answer:
    sentences: list[Sentence]  # cited sentences and Framing; at least one cited unless refused
    refused: bool  # nothing grounded: the Answer says so and cites only likely Partes' Remisiones
    incomplete: bool = False  # grounded sentences, but the coverage line says part of the question is uncovered
    likely_partes: list[LikelyParte] = field(default_factory=list)
    coverage: str | None = None  # total / parcial / ninguna from the coverage line; None when missing or malformed
    gap: str | None = None  # what the coverage line says is missing, for logs only; never shown
    dropped: list[str] = field(default_factory=list)  # unsupported claims: uncited or not backed by the cited text
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
    on_sentence: Callable[[Sentence], None] | None = None,
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
        if on_sentence is not None:
            _emit_complete_sentences(stream, retrieval, source_urls, on_sentence)
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


def _emit_complete_sentences(
    stream: Any, retrieval: Retrieval, source_urls: dict[str, str], on_sentence: Callable[[Sentence], None]
) -> None:
    """Map the text so far at each finished content block; every sentence but the last is complete.

    Nothing is handed over before the coverage line allows an Answer and a cited sentence is complete,
    so Framing never streams alone. A sentence kept only once later cited text backs it is left to the
    final Answer.
    """
    emitted_end = 0
    for event in stream:
        if event.type != "content_block_stop":
            continue
        snapshot = stream.current_message_snapshot.model_dump(mode="json")
        answer, ends = _map(snapshot, retrieval, source_urls)
        if answer.coverage not in ("total", "parcial"):
            continue
        complete = list(zip(answer.sentences, ends))[:-1]
        if not any(s.citations for s, _ in complete):
            continue
        for sentence, end in complete:
            if end > emitted_end:
                on_sentence(sentence)
                emitted_end = end


def map_response(response: dict[str, Any], retrieval: Retrieval, source_urls: dict[str, str]) -> Answer:
    """Turn a Messages API response (as JSON) into an Answer with Citations as data."""
    return _map(response, retrieval, source_urls)[0]


def _map(response: dict[str, Any], retrieval: Retrieval, source_urls: dict[str, str]) -> tuple[Answer, list[int]]:
    """The Answer, and where each of its sentences ends in the response text."""
    model = response.get("model")
    if response.get("stop_reason") == "refusal":
        return Answer(sentences=[], refused=True, model=model), []
    full = ""
    spans: list[tuple[int, int, list[Citation]]] = []  # cited [start, end) offsets into full
    for block in response.get("content", []):
        if block.get("type") != "text":
            continue
        if block.get("citations"):
            cites = [_map_citation(c, retrieval, source_urls) for c in block["citations"]]
            spans.append((len(full), len(full) + len(block["text"]), cites))
        full += block["text"]

    coverage, gap, body = _coverage(full, spans)
    if coverage is None:
        return Answer(sentences=[], refused=True, model=model), []
    support = {t for _, _, cites in spans for c in cites for t in _claim_tokens(f"{c.cited_text} {c.seccion} {c.parte}")}

    def splits_a_span(cut: int) -> bool:
        return any(start < cut < end for start, end, _ in spans)

    cuts = sorted({m.end() for m in _SENTENCE_END.finditer(full, body) if not splits_a_span(m.end())})
    sentences: list[Sentence] = []
    ends: list[int] = []
    dropped: list[str] = []
    pending: Starts | None = None  # a break before text that was not kept carries over to the next sentence
    content_end = body  # where the previous piece's text ended
    for start, end in zip([body, *cuts], [*cuts, len(full)]):
        raw = full[start:end]
        lead = len(raw) - len(raw.lstrip())
        before = full[content_end : start + lead]
        if raw.strip():
            content_end = start + len(raw.rstrip())
        if _BULLET.match(raw, lead) and ("\n" in before or not sentences):
            pending = "item"
        elif "\n" in before and pending is None:
            pending = "paragraph"
        text = _MARKUP.sub("", raw).strip()
        if not _LETTER.search(text):
            continue  # list numbers, stray markup, blank lines
        cites = [c for s_start, s_end, span_cites in spans if start <= s_start and s_end <= end for c in span_cites]
        if _claim_tokens(text) - support:
            dropped.append(text)
            continue
        starts = None if pending == "paragraph" and not sentences else pending
        continues = starts is None and sentences and ends[-1] == start and sentences[-1].text.endswith((":", ";"))
        sentences.append(Sentence(text=text if continues else _capitalize(text), citations=cites, starts=starts))
        ends.append(end)
        pending = None
    cited = {(c.parte, c.seccion) for s in sentences for c in s.citations}
    refused = coverage == "ninguna" or not cited
    if refused:
        sentences, ends = [], []
    answer = Answer(
        sentences=sentences,
        refused=refused,
        incomplete=not refused and coverage == "parcial",
        likely_partes=_likely_partes(gap, retrieval, cited, source_urls) if coverage != "total" else [],
        coverage=coverage,
        gap=gap,
        dropped=dropped,
        model=model,
    )
    return answer, ends


def _coverage(full: str, spans: list[tuple[int, int, list[Citation]]]) -> tuple[str | None, str | None, int]:
    """The coverage line's verdict and what it says is missing, and where the Answer after it starts.

    The verdict is None when the text does not open with an uncited, well-formed coverage line.
    """
    m = _COVERAGE.match(full)
    if m is None or any(start < m.end() for start, _, _ in spans) or m.group(1).lower() not in COVERAGES:
        return None, None, 0
    return m.group(1).lower(), m.group(2).strip() or None, m.end()


def _claim_tokens(text: str) -> set[str]:
    """Numbers (as values, written in digits or words), months and Sección numbers in `text`."""
    text = _INCISO.sub(" ", text)
    tokens = {str(int(re.sub(r"[.,]", "", n))) for n in _NUMBER.findall(text)}
    plain = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    for word in _WORD.findall(plain):
        if word in _NUMBER_WORDS:
            tokens.add(str(_NUMBER_WORDS[word]))
        elif word in _MONTHS:
            tokens.add(word)
    return tokens


def _likely_partes(
    gap: str | None, retrieval: Retrieval, cited: set[tuple[str, str]], source_urls: dict[str, str]
) -> list[LikelyParte]:
    """Partes the gap names that a retrieved Sección has a Remisión to; cited Secciones first, then those it names.

    A Parte the model names without a Remisión in the text is not shown: it would be an uncited claim.
    """
    if gap is None:
        return []
    named = parte_codes(gap)
    ordered = sorted(
        retrieval.secciones, key=lambda r: ((r.parte.code, r.seccion.id) not in cited, r.seccion.id not in gap)
    )
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
    """Uppercase the first letter, e.g. when a dropped claim or a lead-in's colon preceded it on another line."""
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
