"""Remisiones to other Partes found in the text of a Sección.

Only what the grounding step needs: which Partes a Sección's text refers to, and
the verbatim line(s) where it does, so a refusal can name the likely Parte and
cite the Remisión. Remisiones to specific Secciones and following them are #8.
"""

import re
from dataclasses import dataclass

from .parser import Seccion

# A Parte code: a number (not the start of a Sección id such as 61.535) or a short
# letter code such as HL or VLA.
_CODE = r"\b(?:\d{1,3}|[A-Z]{2,3})(?!\w|\.\d)"
_HEAD = r"(?:\bRAAC\s+(?:(?i:Partes?)\s+)?|\b(?i:Partes?)\s+)"
# "RAAC 67", "RAAC Parte 67", "Parte 91 de estas regulaciones", "RAAC 121 o 135",
# "RAAC 61, RAAC 63 y RAAC 65", "RAAC Partes 141 o 142".
_REMISION = re.compile(
    _HEAD + rf"({_CODE}(?:\s*(?:,|\by\b|\bo\b|\bó\b)\s*(?:RAAC\s+)?(?:(?i:Partes?)\s+)?{_CODE})*)"
)
_CODE_RE = re.compile(_CODE)
_NOT_CODES = {"DE", "DEL", "EL", "EN", "LA", "LAS", "LOS"}
# PDF text breaks lines mid-clause; a clause ends a line with . ; or :, and an inciso
# marker such as "(ii)" sits on its own line.
_CLAUSE_END = re.compile(r"[.;:]\s*$")
_INCISO = re.compile(r"^\s*\(?[a-z0-9]{1,4}\)\s*$")


@dataclass(frozen=True)
class Remision:
    to_parte: str
    seccion: Seccion
    page_index: int  # into seccion.pages
    text: str  # the clause holding the Remisión, verbatim from the page text


def parte_codes(text: str) -> list[str]:
    """Parte codes named in `text`, in order of first appearance."""
    codes: list[str] = []
    for m in _REMISION.finditer(text):
        for code in _CODE_RE.findall(m.group(1)):
            if code not in _NOT_CODES and code not in codes:
                codes.append(code)
    return codes


def find_remisiones(seccion: Seccion, own_parte: str) -> list[Remision]:
    """Remisiones from `seccion` to other Partes, first occurrence per target Parte."""
    found: dict[str, Remision] = {}
    for i, page in enumerate(seccion.pages):
        for m in _REMISION.finditer(page.text):
            line = _clause(page.text, m.start(), m.end(), heading={seccion.id, *seccion.title.split()})
            for code in _CODE_RE.findall(m.group(1)):
                if code in _NOT_CODES or code == own_parte or code in found:
                    continue
                found[code] = Remision(to_parte=code, seccion=seccion, page_index=i, text=line)
    return list(found.values())


def _clause(text: str, start: int, end: int, heading: set[str]) -> str:
    """The lines holding text[start:end], widened to the clause they belong to, never into the heading."""
    lines = text.split("\n")
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)
    first = max(i for i, o in enumerate(offsets[:-1]) if o <= start)
    last = max(i for i, o in enumerate(offsets[:-1]) if o < max(end, start + 1))
    def is_boundary(line: str) -> bool:
        words = line.split()
        return bool(_CLAUSE_END.search(line) or _INCISO.match(line) or (words and set(words) <= heading))

    while first > 0 and not is_boundary(lines[first - 1]):
        first -= 1
    while last < len(lines) - 1 and not _CLAUSE_END.search(lines[last]) and not _INCISO.match(lines[last + 1]):
        last += 1
    return "\n".join(lines[first : last + 1]).strip()
