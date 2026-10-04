"""Correctness scorer: an LLM judge compares an Answer with the eval case's reference Answer.

Correctness is scored apart from retrieval and grounding: the judge sees only the question,
the reference Answer and the Answer text as the user reads it (refusal or incompleteness
notice included), never the Citations. Its verdict and rationale go into the eval report
so the owner can spot-check the judge. The judge model is the `judge` stage in models.toml.
"""

import json
from dataclasses import dataclass
from typing import Any

import anthropic

from . import strings
from .answerer import Answer

VERDICTS = {"correcta": 1.0, "parcial": 0.5, "incorrecta": 0.0}

SYSTEM = (
    "Evaluás respuestas sobre las Regulaciones Argentinas de Aviación Civil (RAAC). "
    "Recibís una pregunta, una respuesta de referencia escrita por un experto y la respuesta "
    "a evaluar. Juzgá solo si la respuesta a evaluar es correcta frente a la referencia: "
    "\"correcta\" si transmite lo esencial de la referencia (requisitos, condiciones, números, "
    "plazos y la conclusión) sin contradecirla; \"parcial\" si acierta en parte pero omite algo "
    "esencial de la referencia o agrega una afirmación dudosa; \"incorrecta\" si la contradice, "
    "falla en lo central o se niega a responder cuando la referencia sí responde. Si la referencia "
    "dice que la respuesta debe declarar que no encontró respaldo, una respuesta que lo declara "
    "es correcta y una que responde igual es incorrecta. No penalices la redacción, el orden, el "
    "voseo ni el detalle adicional que no contradiga la referencia. No uses conocimiento propio "
    "sobre la RAAC: la referencia manda. Explicá tu veredicto en una o dos oraciones en español."
)


@dataclass(frozen=True)
class Correctness:
    score: float | None  # 1.0 correcta, 0.5 parcial, 0.0 incorrecta; None when the judge failed
    verdict: str | None
    rationale: str | None
    model: str | None = None  # model that served the judge call
    error: str | None = None


def answer_text(answer: Answer) -> str:
    """The Answer as the user reads it, without Citation markers."""
    parts = []
    if answer.refused:
        parts.append(strings.REFUSAL)
    elif answer.incomplete:
        parts.append(strings.INCOMPLETE)
    parts += [s.text for s in answer.sentences]
    parts += [
        strings.LIKELY_PARTE.format(parte=lp.parte, seccion=lp.citation.seccion) for lp in answer.likely_partes
    ]
    return " ".join(parts)


def build_request(model: str, question: str, reference_answer: str, answer: Answer) -> dict[str, Any]:
    return {
        "model": model,
        "max_tokens": 1024,
        "system": SYSTEM,
        "messages": [
            {
                "role": "user",
                "content": (
                    f"<pregunta>\n{question}\n</pregunta>\n\n"
                    f"<referencia>\n{reference_answer}\n</referencia>\n\n"
                    f"<respuesta>\n{answer_text(answer)}\n</respuesta>"
                ),
            }
        ],
        "output_config": {
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "fundamento": {"type": "string"},
                        "veredicto": {"type": "string", "enum": list(VERDICTS)},
                    },
                    "required": ["fundamento", "veredicto"],
                    "additionalProperties": False,
                },
            }
        },
    }


def parse_verdict(response: dict[str, Any]) -> Correctness:
    text = next((b["text"] for b in response.get("content", []) if b.get("type") == "text"), None)
    if text is None:
        raise ValueError(f"Judge response has no text block (stop_reason={response.get('stop_reason')!r})")
    data = json.loads(text)
    verdict = data["veredicto"]
    if verdict not in VERDICTS:
        raise ValueError(f"Unknown judge verdict {verdict!r}")
    return Correctness(VERDICTS[verdict], verdict, data["fundamento"].strip(), model=response.get("model"))


class CorrectnessJudge:
    def __init__(self, client: anthropic.Anthropic, model: str):
        self._client = client
        self.model = model

    def judge(self, question: str, reference_answer: str, answer: Answer) -> Correctness:
        """Never raises: a failed judge call is recorded on the case instead of losing the run."""
        try:
            request = build_request(self.model, question, reference_answer, answer)
            response = self._client.messages.create(**request)
            return parse_verdict(response.to_dict())
        except Exception as e:
            return Correctness(None, None, None, error=f"{type(e).__name__}: {e}")
