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


@dataclass(frozen=True)
class Remision:
    to_parte: str
    seccion: Seccion
    page_index: int  # into seccion.pages
    text: str  # the whole line(s) holding the Remisión, verbatim from the page text


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
            line_start = page.text.rfind("\n", 0, m.start()) + 1
            line_end = page.text.find("\n", m.end())
            line = page.text[line_start : len(page.text) if line_end == -1 else line_end]
            for code in _CODE_RE.findall(m.group(1)):
                if code in _NOT_CODES or code == own_parte or code in found:
                    continue
                found[code] = Remision(to_parte=code, seccion=seccion, page_index=i, text=line)
    return list(found.values())
