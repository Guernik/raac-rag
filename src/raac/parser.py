"""ParteParser: pure PDF -> ParsedParte.

Reads the Parte code from the running header, the Edición/Enmienda, date and
printed page label from each page footer, and splits the body into Secciones
(id, title, PDF pages, printed page labels, text per page, text rectangles,
Remisiones).
Footers of one Parte may disagree; they are kept per page (ADR 0004). For Parte 1
it also extracts the Definiciones of Subparte B, each with its own pages and text.
"""

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

from .remisiones import Remision, extract_remisiones

_HEADER_RE = re.compile(r"^\s*RAAC\s+PARTE\s+(\S+)", re.I)
# Footers vary by Parte, e.g. "ADMINISTRACIÓN NACIONAL\nVI Edición\nmayo 2026\nDE AVIACIÓN CIVIL\n 10\nEnmienda I"
# (61), "...\n5º Edición\n..." (1, no Enmienda printed), or split into a separate
# "4º Edición\n22 mayo 2026" block (91).
_FOOTER_RE = re.compile(r"^\s*(ADMINISTRACI[ÓO]N\s+NACIONAL|(\d+\s*[º°]|[IVXLC]+)\s+Edici[óo]n\b)", re.I)
_FOOTER_ZONE = 0.85  # footer blocks start below this fraction of the page height
_EDICION_RE = re.compile(r"(?:^|\n)\s*(\d+)\s*[º°]?\s+Edici[óo]n|(?:^|\n)\s*([IVXLC]+)\s+Edici[óo]n", re.I)
_ENMIENDA_RE = re.compile(r"(?:^|\n)\s*Enmienda\s+(\d+|[IVXLC]+)\s*(?:\n|$)", re.I)
_MONTHS = "enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre"
_FECHA_RE = re.compile(rf"(?:^|\n)\s*((?:\d{{1,2}}\s+)?(?:{_MONTHS})\s+\d{{4}})\s*(?:\n|$)", re.I)
# Printed page label after "DE AVIACIÓN CIVIL" (61, 67, front matter of 1 and 91)...
_FOOTER_LABEL_RE = re.compile(r"CIVIL[ \t]*(?:\n[ \t]*)?(\d+|[ivxlc]+|[IVXLC]+)[ \t]*(?:\n|$)")
# ...or in the running header as "<division> <n>. <m>" (body of 1 and 91, e.g. "SUBPARTE B 2. 39").
_HEADER_LABEL_RE = re.compile(r"(?:SUBPARTE|AP[EÉ]NDICE|CAP[IÍ]TULO)\s+\S+\s+(\d+)\.\s*(\d+)", re.I)
# Page-check lists pair page labels with division names ("1.1\nSUBPARTE A"); never a title.
_DIVISION_WORD_RE = re.compile(r"^\s*(CAP[IÍ]TULO|SUBPARTE|AP[EÉ]NDICE)\b", re.I)
_DIVISION_RE = re.compile(r"^\s*(CAP[IÍ]TULO|SUBPARTE|AP[EÉ]NDICE)\s+\S+\s+[—–-]")
# Parte 1, Subparte B "Definiciones Generales" is Sección 1.11 (as 1.7 says). Each Definición
# starts at the left margin with its term in bold up to a colon ("Noche: ..."); indented bold
# terms are sub-items of the Definición above them ("Nieve seca:" under "Nieve").
DEFINICIONES_PARTE = "1"
DEFINICIONES_SECCION = "1.11"
_MIN_DEFINICIONES = 100
_BOLD = 16  # PyMuPDF span flag
_MARGIN_TOLERANCE = 5.0
# Lines that close the Definiciones: the next division, or the title lines printed above it.
_DEFINICIONES_END_RE = re.compile(
    r"^\s*(?:(?:CAP[IÍ]TULO|SUBPARTE|AP[EÉ]NDICE)\s+\S+\s+[—–-]|REGULACIONES ARGENTINAS|PARTE\s+\S+\s+[—–-])", re.I
)


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True)
class PageVersion:
    """Edición/Enmienda and date as one page footer prints them."""

    edicion: str
    enmienda: str | None  # None when the footer prints none
    fecha: str | None  # e.g. "27 febrero 2026", "mayo 2026"


@dataclass
class SeccionPage:
    pdf_page: int  # 1-based physical page
    printed_page: str | None
    text: str
    rects: list[Rect] = field(default_factory=list)


@dataclass
class Seccion:
    id: str  # e.g. "61.535"
    title: str
    pages: list[SeccionPage]
    remisiones: list[Remision] = field(default_factory=list, repr=False, compare=False)

    @property
    def pdf_page_start(self) -> int:
        return self.pages[0].pdf_page

    @property
    def pdf_page_end(self) -> int:
        return self.pages[-1].pdf_page

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pages)


@dataclass
class Definicion:
    """A term defined in Parte 1 and its text, verbatim from its PDF pages."""

    term: str  # as printed, e.g. "Aeródromo (AD)", "Noche"
    seccion_id: str  # the Sección that holds it, "1.11"
    pages: list[SeccionPage]

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pages)


@dataclass
class ParsedParte:
    code: str
    edicion: str  # the most common footer Edición/Enmienda, for logs only (ADR 0004)
    enmienda: str | None
    page_count: int
    printed_pages: list[str | None]  # index i -> printed label of PDF page i+1
    page_versions: list[PageVersion | None]  # index i -> footer of PDF page i+1
    secciones: list[Seccion]
    content_hash: str
    definiciones: list[Definicion] = field(default_factory=list)  # Parte 1 only

    def page_version(self, pdf_page: int) -> PageVersion | None:
        return self.page_versions[pdf_page - 1]

    def seccion(self, seccion_id: str) -> Seccion:
        for s in self.secciones:
            if s.id == seccion_id:
                return s
        raise KeyError(seccion_id)

    def secciones_on_pages(self, pdf_pages: set[int]) -> list[Seccion]:
        return [s for s in self.secciones if any(p.pdf_page in pdf_pages for p in s.pages)]


def parse(pdf: bytes) -> ParsedParte:
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    code = None
    versions: list[PageVersion | None] = []
    printed: list[str | None] = []
    bodies: list[list[tuple]] = []
    for page in doc:
        body = []
        header_text = ""
        footer_text = ""
        for block in page.get_text("blocks", sort=True):
            text = block[4]
            if block[6] != 0:  # image block
                continue
            m = _HEADER_RE.match(text)
            if m:
                code = code or m.group(1)
                header_text += text
                continue
            if block[1] > _FOOTER_ZONE * page.rect.height and _FOOTER_RE.match(text):
                footer_text += text + "\n"
                continue
            body.append(block)
        versions.append(_footer_version(footer_text))
        printed.append(_printed_label(footer_text, header_text))
        bodies.append(body)

    if code is None:
        raise ParseError("Parte code not found in any page header ('RAAC PARTE <code>')")
    found = Counter((v.edicion, v.enmienda) for v in versions if v)
    if not found:
        raise ParseError(f"Parte {code}: Edición/Enmienda not found in any page footer")
    (edicion, enmienda), _ = found.most_common(1)[0]

    secciones = _split_secciones(code, bodies, printed)
    if not secciones:
        raise ParseError(f"Parte {code}: no Secciones found")
    for s in secciones:
        s.remisiones = extract_remisiones(s, code)
    definiciones = _definiciones(doc, secciones, printed) if code == DEFINICIONES_PARTE else []
    return ParsedParte(
        code=code,
        edicion=edicion,
        enmienda=enmienda,
        page_count=doc.page_count,
        printed_pages=printed,
        page_versions=versions,
        secciones=secciones,
        content_hash=hashlib.sha256(pdf).hexdigest(),
        definiciones=definiciones,
    )


def _definiciones(doc: pymupdf.Document, secciones: list[Seccion], printed: list[str | None]) -> list[Definicion]:
    seccion = next((s for s in secciones if s.id == DEFINICIONES_SECCION), None)
    if seccion is None:
        raise ParseError(f"Parte {DEFINICIONES_PARTE}: Sección {DEFINICIONES_SECCION} (Definiciones) not found")
    definiciones: list[Definicion] = []
    started = False
    for page_index in range(seccion.pdf_page_start - 1, seccion.pdf_page_end):
        page = doc[page_index]
        lines = list(_body_lines(page))
        margin = min((x0 for x0, *_ in lines), default=0.0)
        for x0, bbox, text, bold_start in lines:
            if not started:
                started = re.match(rf"^{re.escape(DEFINICIONES_SECCION)}\b", text) is not None
                continue
            if _DEFINICIONES_END_RE.match(text):
                break
            term, colon, _ = text.partition(":")
            if bold_start and colon and x0 <= margin + _MARGIN_TOLERANCE and term.strip()[:1].isalpha():
                definiciones.append(Definicion(term=_clean(term), seccion_id=seccion.id, pages=[]))
            if definiciones:
                _append(definiciones[-1], page_index, printed, text, bbox)
        else:
            continue
        break
    if len(definiciones) < _MIN_DEFINICIONES:
        raise ParseError(
            f"Parte {DEFINICIONES_PARTE}: only {len(definiciones)} Definiciones found in Sección {DEFINICIONES_SECCION}"
        )
    return definiciones


def _body_lines(page: pymupdf.Page):
    """(x0, bbox, text, starts bold) per text line, without the running header and the footer."""
    for block in page.get_text("dict", sort=True)["blocks"]:
        if block.get("type") != 0:
            continue
        block_text = "\n".join("".join(s["text"] for s in line["spans"]) for line in block["lines"])
        if _HEADER_RE.match(block_text):
            continue
        if block["bbox"][1] > _FOOTER_ZONE * page.rect.height and _FOOTER_RE.match(block_text):
            continue
        for line in block["lines"]:
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = _clean("".join(s["text"] for s in line["spans"]))
            yield spans[0]["bbox"][0], tuple(line["bbox"]), text, bool(spans[0]["flags"] & _BOLD)


def _footer_version(footer: str) -> PageVersion | None:
    """Edición and Enmienda as roman numerals ("Enmienda 1" -> "I"), and the printed date."""
    m = _EDICION_RE.search(footer)
    if not m:
        return None
    edicion = _roman(int(m.group(1))) if m.group(1) else m.group(2).upper()
    e = _ENMIENDA_RE.search(footer)
    enmienda = None if e is None else _roman(int(e.group(1))) if e.group(1).isdigit() else e.group(1).upper()
    f = _FECHA_RE.search(footer)
    return PageVersion(edicion, enmienda, _clean(f.group(1)).lower() if f else None)


def _printed_label(footer: str, header: str) -> str | None:
    m = _FOOTER_LABEL_RE.search(footer)
    if m:
        return m.group(1)
    m = _HEADER_LABEL_RE.search(header)
    return f"{m.group(1)}.{m.group(2)}" if m else None


def _roman(n: int) -> str:
    out = ""
    for value, numeral in ((10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        while n >= value:
            out, n = out + numeral, n - value
    return out


@dataclass
class _Line:
    page_index: int
    block: tuple
    text: str


@dataclass
class _Heading:
    at: int  # index into the line stream of the id line
    id: str | None  # None for a range "<id> al <id> Reservado", which only closes a Sección
    title: str
    body_at: int  # first line after the title


def _split_secciones(code: str, bodies: list[list[tuple]], printed: list[str | None]) -> list[Seccion]:
    """Split body text into Secciones, line by line over the whole document.

    A heading is "<id>" on its own line followed by its title (61, 67), "<id> Title" on
    one line, and may sit mid-block after the previous Sección's text (91). Índice
    entries look the same but are followed at once by the next entry, not by text,
    and they can share a block with body text (91's per-Subparte índices).
    """
    sid = re.escape(code) + r"\.\d+ª?"
    id_only = re.compile(rf"^\s*({sid})\s*$")
    id_title = re.compile(rf"^\s*({sid})\s*(?:[–-]\s*|\s)(\S.*)$")
    id_range = re.compile(rf"^\s*{sid}\s+al\s+{sid}\b", re.I)
    range_tail = re.compile(r"^\s*al\b", re.I)

    lines = [
        _Line(i, block, line)
        for i, blocks in enumerate(bodies)
        for block in blocks
        for line in block[4].splitlines()
        if line.strip()
    ]
    headings: list[_Heading] = []
    for n, line in enumerate(lines):
        following = lines[n + 1].text if n + 1 < len(lines) else ""
        if id_range.match(line.text) or id_only.match(line.text) and range_tail.match(following):
            headings.append(_Heading(n, None, "", n + 1))
            continue
        m = id_only.match(line.text)
        # A title on its own line may start in lowercase (91.103 "información sobre vuelos").
        if m and following.strip()[:1].isalpha() and not _DIVISION_WORD_RE.match(following):
            title, body_at = _title(following, lines, n + 2)
            headings.append(_Heading(n, m.group(1), title, body_at))
            continue
        m = id_title.match(line.text)
        if m and _is_title(m.group(2)):
            title, body_at = _title(m.group(2), lines, n + 1)
            headings.append(_Heading(n, m.group(1), title, body_at))

    # Índice entries come in runs, each followed by the next id within a line or two.
    # The run's last entry looks like a body heading, but its id comes again later.
    index = set()
    k = 0
    while k < len(headings):
        j = k
        while j + 1 < len(headings) and headings[j + 1].at - headings[j].body_at <= 1:
            j += 1
        if j - k + 1 >= 3:
            index.update(range(k, j))
        k = j + 1
    last = {h.id: k for k, h in enumerate(headings) if h.id}
    index.update(k for k, h in enumerate(headings) if h.id and last[h.id] != k)
    starts = {h.at: h for k, h in enumerate(headings) if k not in index}
    index_lines = {
        n for k in index
        for n in range(headings[k].at, headings[k + 1].at if k + 1 < len(headings) else headings[k].body_at)
    }

    secciones: list[Seccion] = []
    current: Seccion | None = None
    for n, line in enumerate(lines):
        if n in starts:
            h = starts[n]
            current = Seccion(id=h.id, title=h.title, pages=[]) if h.id else None
            if current is not None:
                secciones.append(current)
        elif n in index_lines:
            continue
        elif _DIVISION_RE.match(line.text):
            current = None  # a Capítulo/Subparte/Apéndice heading closes the Sección
            continue
        if current is not None:
            _append(current, line.page_index, printed, line.text, line.block)
    return secciones


def _title(first: str, lines: list[_Line], n: int) -> tuple[str, int]:
    """A title is its first line plus wrapped lines that continue in lowercase."""
    title = first
    while n < len(lines) and lines[n].text.strip()[:1].islower():
        title += " " + lines[n].text
        n += 1
    return _clean(title), n


def _is_title(line: str) -> bool:
    line = line.strip()
    return bool(line) and line[0].isalpha() and line[0].isupper() and not _DIVISION_WORD_RE.match(line)


def _append(seccion: Seccion | Definicion, page_index: int, printed: list[str | None], text: str, block: tuple) -> None:
    pdf_page = page_index + 1
    if not seccion.pages or seccion.pages[-1].pdf_page != pdf_page:
        seccion.pages.append(SeccionPage(pdf_page=pdf_page, printed_page=printed[page_index], text=""))
    page = seccion.pages[-1]
    page.text = (page.text + "\n" + _clean_block(text)).strip("\n")
    rect = Rect(*(round(v, 1) for v in block[:4]))
    if rect not in page.rects:
        page.rects.append(rect)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _clean_block(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)
