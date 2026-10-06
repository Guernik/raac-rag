"""Definiciones from Parte 1 that a Sección's text uses.

A term matches as a whole phrase, ignoring case, line breaks and a plural ending on its
words ("aeródromos controlados" uses "Aeródromo controlado"). Parentheticals in the term are not part of the
phrase ("Aeródromo (AD)" matches "aeródromo"). A Definición that only points to another
("Vuelo nocturno: (Ver noche).") brings that one along.

Short generic terms ("Aeronave", "Piloto") match nearly every Sección, so a one-word term
inside a longer matched one is skipped and each Sección contributes at most `per_seccion`
Definiciones, the most specific first: longer phrases, then terms used in fewer of the
retrieved Secciones.
"""

import re
from collections.abc import Iterable
from functools import lru_cache

from .parser import Definicion

PER_SECCION = 8
MAX_TOTAL = 20

_PARENTHETICAL = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_POINTER = re.compile(r"^[^:]+:\s*\(?\s*(?:Ver|V[ée]ase)\s+[“\"]?([^”\")]+?)[”\"]?\s*\)?\s*\.?\s*$", re.I)


def phrase(term: str) -> str:
    return re.sub(r"\s+", " ", _PARENTHETICAL.sub(" ", term)).strip().casefold()


@lru_cache(maxsize=None)
def _pattern(p: str) -> re.Pattern[str]:
    words = [re.escape(w) + r"(?:s|es)?" for w in p.split(" ")]
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.I)


def uses(text: str, definicion: Definicion) -> bool:
    p = phrase(definicion.term)
    return bool(p) and _pattern(p).search(text) is not None


def pointed_to(definicion: Definicion, by_phrase: dict[str, Definicion]) -> Definicion | None:
    """The Definición a pointer-only Definición refers to ("Piloto al mando: Véase “Comandante de aeronave”.")."""
    m = _POINTER.match(re.sub(r"\s+", " ", definicion.text))
    return by_phrase.get(phrase(m.group(1))) if m else None


def used_by(
    texts: Iterable[str],
    definiciones: list[Definicion],
    per_seccion: int = PER_SECCION,
    max_total: int = MAX_TOTAL,
) -> list[Definicion]:
    """Definiciones the given Sección texts use, in Sección order, capped per Sección and in total."""
    texts = list(texts)
    by_phrase: dict[str, Definicion] = {}
    for d in definiciones:
        by_phrase.setdefault(phrase(d.term), d)
    matches = [[d for d in by_phrase.values() if uses(text, d)] for text in texts]
    frequency = {id(d): sum(d in m for m in matches) for m in matches for d in m}
    chosen: list[Definicion] = []
    for found in matches:
        # A one-word term inside a longer matched one ("Vuelo" in "Vuelo VFR") adds nothing.
        phrases = [phrase(d.term) for d in found]
        specific = [
            d for d, p in zip(found, phrases) if " " in p or not any(p != q and _pattern(p).search(q) for q in phrases)
        ]
        specific.sort(key=lambda d: (-len(phrase(d.term).split()), frequency[id(d)], phrase(d.term)))
        for d in specific[:per_seccion]:
            for item in (d, pointed_to(d, by_phrase)):
                if item is not None and item not in chosen and len(chosen) < max_total:
                    chosen.append(item)
    return chosen
