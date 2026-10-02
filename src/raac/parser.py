"""ParteParser: pure PDF -> ParsedParte.

Reads the Parte code from the running header, the Edición/Enmienda and the
printed page label from each page footer, and splits the body into Secciones
(id, title, PDF pages, printed page labels, text per page, text rectangles).
"""

import hashlib
import re
from dataclasses import dataclass, field

import pymupdf

_HEADER_RE = re.compile(r"^\s*RAAC\s+PARTE\s+(\S+)", re.I)
# Footers vary by Parte, e.g. "ADMINISTRACIÓN NACIONAL\nVI Edición\nmayo 2026\nDE AVIACIÓN CIVIL\n 10\nEnmienda I"
# (61), "...\n5º Edición\n..." (1, no Enmienda printed), or split into a separate
# "4º Edición\n22 mayo 2026" block (91).
_FOOTER_RE = re.compile(r"^\s*(ADMINISTRACI[ÓO]N\s+NACIONAL|(\d+\s*[º°]|[IVXLC]+)\s+Edici[óo]n\b)", re.I)
_FOOTER_ZONE = 0.85  # footer blocks start below this fraction of the page height
_EDICION_RE = re.compile(r"(?:^|\n)\s*(\d+)\s*[º°]?\s+Edici[óo]n|(?:^|\n)\s*([IVXLC]+)\s+Edici[óo]n", re.I)
_ENMIENDA_RE = re.compile(r"(?:^|\n)\s*Enmienda\s+([IVXLC]+)\s*(?:\n|$)", re.I)
# Printed page label after "DE AVIACIÓN CIVIL" (61, 67, front matter of 1 and 91)...
_FOOTER_LABEL_RE = re.compile(r"CIVIL[ \t]*(?:\n[ \t]*)?(\d+|[ivxlc]+|[IVXLC]+)[ \t]*(?:\n|$)")
# ...or in the running header as "<division> <n>. <m>" (body of 1 and 91, e.g. "SUBPARTE B 2. 39").
_HEADER_LABEL_RE = re.compile(r"(?:SUBPARTE|AP[EÉ]NDICE|CAP[IÍ]TULO)\s+\S+\s+(\d+)\.\s*(\d+)", re.I)
# Page-check lists pair page labels with division names ("1.1\nSUBPARTE A"); never a title.
_DIVISION_WORD_RE = re.compile(r"^\s*(CAP[IÍ]TULO|SUBPARTE|AP[EÉ]NDICE)\b", re.I)
_DIVISION_RE = re.compile(r"^\s*(CAP[IÍ]TULO|SUBPARTE|AP[EÉ]NDICE)\s+\S+\s+[—–-]")


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class Rect:
    x0: float
    y0: float
    x1: float
    y1: float


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
class ParsedParte:
    code: str
    edicion: str
    enmienda: str | None  # None when the footer prints only the Edición
    page_count: int
    printed_pages: list[str | None]  # index i -> printed label of PDF page i+1
    secciones: list[Seccion]
    content_hash: str

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
    versions: list[tuple[str, str | None] | None] = []
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
    found = {v for v in versions if v}
    if not found:
        raise ParseError(f"Parte {code}: Edición/Enmienda not found in any page footer")
    if len(found) > 1:
        raise ParseError(f"Parte {code}: footers disagree on Edición/Enmienda: {sorted(found, key=str)}")
    edicion, enmienda = found.pop()

    secciones = _split_secciones(code, bodies, printed)
    if not secciones:
        raise ParseError(f"Parte {code}: no Secciones found")
    return ParsedParte(
        code=code,
        edicion=edicion,
        enmienda=enmienda,
        page_count=doc.page_count,
        printed_pages=printed,
        secciones=secciones,
        content_hash=hashlib.sha256(pdf).hexdigest(),
    )


def _footer_version(footer: str) -> tuple[str, str | None] | None:
    """(Edición, Enmienda) as roman numerals; Enmienda is None when the footer prints none."""
    m = _EDICION_RE.search(footer)
    if not m:
        return None
    edicion = _roman(int(m.group(1))) if m.group(1) else m.group(2).upper()
    e = _ENMIENDA_RE.search(footer)
    return edicion, e.group(1).upper() if e else None


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


def _append(seccion: Seccion, page_index: int, printed: list[str | None], text: str, block: tuple) -> None:
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
