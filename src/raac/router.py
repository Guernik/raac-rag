"""Parte routing: Standalone question -> which Partes to search (ADR 0001).

One prompt over every indexed Parte's title and root summary (the PageIndex
document description plus its top-level divisions) picks the Partes; tree
search then runs only within them. The answer is constrained to the known
Parte codes with structured outputs, so it never names an unindexed Parte.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic

from .indexer import ParteIndex

SYSTEM = (
    "Elegís en qué Partes de las Regulaciones Argentinas de Aviación Civil (RAAC) "
    "buscar la respuesta a una pregunta. Recibís el código, el título y un resumen de "
    "cada Parte disponible. Elegí todas las Partes cuyo texto probablemente responda "
    "la pregunta o una parte de ella, y solo esas: si la pregunta toca temas que "
    "regulan Partes distintas (por ejemplo, qué licencia se necesita y qué reglas de "
    "vuelo se aplican), elegí cada una. Las preguntas usan jerga aeronáutica y siglas "
    "en inglés (PPL, VFR, IFR, METAR). Ordená las Partes de la más a la menos "
    "pertinente. Si ninguna Parte trata el tema, devolvé una lista vacía."
)
_MAX_SUMMARY_CHARS = 1500


@dataclass(frozen=True)
class ParteCard:
    """What routing sees of one Parte."""

    code: str
    titulo: str
    summary: str
    divisions: list[str] = field(default_factory=list)  # top-level tree node titles


def parte_card(code: str, titulo: str, index: ParteIndex) -> ParteCard:
    return ParteCard(
        code=code,
        titulo=titulo,
        summary=(index.description or "")[:_MAX_SUMMARY_CHARS],
        divisions=[node["title"] for node in index.tree],
    )


def build_request(model: str, standalone_question: str, cards: list[ParteCard]) -> dict[str, Any]:
    catalog = "\n\n".join(
        f"<parte codigo=\"{c.code}\">\nTítulo: {c.titulo}\nResumen: {c.summary}\n"
        f"Divisiones: {'; '.join(c.divisions)}\n</parte>"
        for c in cards
    )
    return {
        "model": model,
        "max_tokens": 512,
        "system": SYSTEM,
        "messages": [
            {
                "role": "user",
                "content": f"<partes>\n{catalog}\n</partes>\n\nPregunta: {standalone_question}",
            }
        ],
        "output_config": {
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "partes": {
                            "type": "array",
                            "items": {"type": "string", "enum": [c.code for c in cards]},
                        }
                    },
                    "required": ["partes"],
                    "additionalProperties": False,
                },
            }
        },
    }


def routed_partes(response: dict[str, Any], cards: list[ParteCard]) -> list[str]:
    """Parte codes from a routing response, in the model's order, known codes only."""
    text = next((b["text"] for b in response.get("content", []) if b.get("type") == "text"), None)
    if text is None:
        raise ValueError(f"Routing response has no text block (stop_reason={response.get('stop_reason')!r})")
    known = {c.code for c in cards}
    routed: list[str] = []
    for code in json.loads(text)["partes"]:
        if code in known and code not in routed:
            routed.append(code)
    return routed


class ParteRouter:
    def __init__(
        self,
        client: anthropic.Anthropic,
        model: str,
        cards: list[ParteCard],
        record_path: Path | None = None,
    ):
        self._client = client
        self._model = model
        self._cards = cards
        self._record_path = record_path

    @property
    def model(self) -> str:
        return self._model

    @property
    def cards(self) -> list[ParteCard]:
        return self._cards

    def route(self, standalone_question: str) -> list[str]:
        if len(self._cards) == 1:
            return [self._cards[0].code]  # nothing to choose
        response = self._client.messages.create(**build_request(self._model, standalone_question, self._cards))
        raw = response.to_dict()
        if self._record_path:
            self._record_path.write_text(
                json.dumps({"question": standalone_question, "response": raw}, ensure_ascii=False, indent=2)
            )
        return routed_partes(raw, self._cards)


def routing_report(router: "ParteRouter", cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Routing accuracy: a case passes when every expected Parte is routed."""
    rows = []
    per_parte: dict[str, list[int]] = {}
    for case in cases:
        expected = sorted({s["parte"] for s in case["expected_secciones"]})
        routed = router.route(case["question"])
        missing = [p for p in expected if p not in routed]
        rows.append({
            "id": case["id"],
            "expected": expected,
            "routed": routed,
            "hit": not missing,
            "extra": [p for p in routed if p not in expected],
        })
        for p in expected:
            per_parte.setdefault(p, [0, 0])
            per_parte[p][0] += p in routed
            per_parte[p][1] += 1
    return {
        "model": router.model,
        "cases": rows,
        "accuracy": sum(r["hit"] for r in rows) / len(rows) if rows else None,
        "exact": sum(r["hit"] and not r["extra"] for r in rows) / len(rows) if rows else None,
        "recall_per_parte": {p: hits / total for p, (hits, total) in sorted(per_parte.items())},
    }
