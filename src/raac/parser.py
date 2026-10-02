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
_FOOTER_RE = re.compile(r"^\s*ADMINISTRACI[ÓO]N\s+NACIONAL", re.I)
# Footer block, e.g. "ADMINISTRACIÓN NACIONAL\nVI Edición\nmayo 2026\nDE AVIACIÓN CIVIL\n 10\nEnmienda I"
_FOOTER_FIELDS_RE = re.compile(
    r"([IVXLC]+)\s+Edici[óo]n.*?CIVIL\s*\n\s*(\S+)\s*\n\s*Enmienda\s+([IVXLC]+)",
    re.S | re.I,
)
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
    enmienda: str
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
    footers: list[tuple[str, str, str] | None] = []
    bodies: list[list[tuple]] = []
    for page in doc:
        body = []
        footer = None
        for block in page.get_text("blocks", sort=True):
            text = block[4]
            if block[6] != 0:  # image block
                continue
            m = _HEADER_RE.match(text)
            if m:
                code = code or m.group(1)
                continue
            if _FOOTER_RE.match(text):
                fm = _FOOTER_FIELDS_RE.search(text)
                footer = fm.groups() if fm else None
                continue
            body.append(block)
        footers.append(footer)
        bodies.append(body)

    if code is None:
        raise ParseError("Parte code not found in any page header ('RAAC PARTE <code>')")
    versions = {(f[0], f[2]) for f in footers if f}
    if not versions:
        raise ParseError(f"Parte {code}: Edición/Enmienda not found in any page footer")
    if len(versions) > 1:
        raise ParseError(f"Parte {code}: footers disagree on Edición/Enmienda: {sorted(versions)}")
    edicion, enmienda = versions.pop()
    printed = [f[1] if f else None for f in footers]

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


def _split_secciones(code: str, bodies: list[list[tuple]], printed: list[str | None]) -> list[Seccion]:
    sid = re.escape(code) + r"\.\d+"
    # A heading is a block holding exactly "<id>\n<title>" (the Índice packs many ids per block).
    heading_re = re.compile(rf"^\s*({sid})\s*\n(.+?)\s*$", re.S)
    any_id_re = re.compile(rf"(?m)^\s*{sid}\b")

    secciones: list[Seccion] = []
    current: Seccion | None = None
    for i, blocks in enumerate(bodies):
        if sum(len(any_id_re.findall(b[4])) for b in blocks) > 2 and _is_index_page(blocks, any_id_re):
            continue
        for block in blocks:
            text = block[4]
            m = heading_re.match(text)
            if m and "\n" not in m.group(2).strip():
                current = Seccion(id=m.group(1), title=_clean(m.group(2)), pages=[])
                secciones.append(current)
                _append(current, i, printed, text, block)
                continue
            if _DIVISION_RE.match(text):
                current = None  # a Capítulo/Subparte/Apéndice heading closes the Sección
                continue
            if current is not None and text.strip():
                _append(current, i, printed, text, block)
    return secciones


def _is_index_page(blocks: list[tuple], any_id_re: re.Pattern) -> bool:
    # Índice pages list several ids in a single block; body pages never do.
    return any(len(any_id_re.findall(b[4])) > 1 for b in blocks)


def _append(seccion: Seccion, page_index: int, printed: list[str | None], text: str, block: tuple) -> None:
    pdf_page = page_index + 1
    if not seccion.pages or seccion.pages[-1].pdf_page != pdf_page:
        seccion.pages.append(SeccionPage(pdf_page=pdf_page, printed_page=printed[page_index], text=""))
    page = seccion.pages[-1]
    page.text = (page.text + "\n" + _clean_block(text)).strip("\n")
    page.rects.append(Rect(*(round(v, 1) for v in block[:4])))


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _clean_block(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)
