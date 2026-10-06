"""Generated eval cases: sample Secciones of a Parte, have an LLM write the question a user would
ask that the Sección answers plus a reference Answer, and let the owner review each candidate.

Candidates live in their own JSONL file with a review status; only accepted ones are appended to
the eval set, marked `"source": "generated"` (hand-written cases are `"owner"`, the default).
"""

import json
import random
import re
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import anthropic

from . import strings
from .parser import ParsedParte, Seccion

SYSTEM = (
    "Escribís casos de evaluación para un asistente que responde preguntas sobre las "
    "Regulaciones Argentinas de Aviación Civil (RAAC). Recibís el texto de una Sección de "
    "una Parte. Escribí:\n"
    "1. pregunta: la pregunta que un piloto, alumno piloto, instructor o profesional "
    "aeronáutico argentino le haría a un colega y que esta Sección responde. Escribila como "
    "habla la gente, en voseo, con jerga y siglas cuando sea natural (PPL, VFR, IFR, PIC, "
    "\"el médico\" por el certificado médico aeronáutico), a menudo desde su propia situación "
    "(\"Soy PPL y...\"). No copies frases del texto regulatorio, no nombres la Sección ni la "
    "Parte por su número y no uses el título de la Sección como pregunta.\n"
    "2. respuesta_referencia: la respuesta correcta y completa a esa pregunta según la "
    "Sección, en castellano llano y voseo, en pocas oraciones. Incluí los requisitos, "
    "números, plazos y excepciones que la Sección da para ese caso, sin agregar nada que "
    "la Sección no diga."
)
_MAX_SECCION_CHARS = 12_000  # Parte 1's Definiciones run to 175k chars; the start is enough to ask about
_MIN_SECCION_CHARS = 120  # shorter is a heading with no rule ("Reservado")
_SKIP_TITLE_RE = re.compile(r"^\s*(reservad|derogad)", re.I)
_COPY_NGRAM = 8  # a question sharing this many consecutive words with the Sección copies its wording;
# shorter runs are often just a defined term ("habilitación de instructor de vuelo de RPAS")
_SECCION_ID_RE = re.compile(r"\b\d+\s*\.\s*\d{2,}\b")

PENDING, ACCEPTED, REJECTED = "pendiente", "aceptado", "rechazado"
STATUSES = (PENDING, ACCEPTED, REJECTED)


class CandidateError(ValueError):
    pass


@dataclass
class Candidate:
    id: str  # "gen-<parte>-<seccion>", also the eval case id once accepted
    parte: str
    seccion: str
    seccion_title: str
    seccion_text: str
    question: str
    reference_answer: str
    model: str
    status: str = PENDING

    def to_case(self) -> dict[str, Any]:
        """The eval case this candidate becomes when accepted."""
        return {
            "id": self.id,
            "question": self.question,
            "expected_secciones": [{"parte": self.parte, "seccion": self.seccion}],
            "reference_answer": self.reference_answer,
            "out_of_scope": False,
            "source": "generated",
        }


def eligible(seccion: Seccion) -> bool:
    return not _SKIP_TITLE_RE.match(seccion.title) and len(seccion.text) >= _MIN_SECCION_CHARS


def sample_secciones(parte: ParsedParte, n: int, exclude: set[str], seed: int | None = None) -> list[Seccion]:
    """Up to n eligible Secciones not in `exclude`, in PDF order; same seed, same sample."""
    pool = [s for s in parte.secciones if eligible(s) and s.id not in exclude]
    picked = random.Random(seed).sample(pool, min(n, len(pool)))
    return sorted(picked, key=lambda s: parte.secciones.index(s))


def build_request(model: str, parte_code: str, seccion: Seccion) -> dict[str, Any]:
    text = seccion.text[:_MAX_SECCION_CHARS]
    return {
        "model": model,
        "max_tokens": 2048,
        "system": SYSTEM,
        "messages": [
            {
                "role": "user",
                "content": f'<seccion parte="{parte_code}" id="{seccion.id}" titulo="{seccion.title}">\n{text}\n</seccion>',
            }
        ],
        "output_config": {
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {"pregunta": {"type": "string"}, "respuesta_referencia": {"type": "string"}},
                    "required": ["pregunta", "respuesta_referencia"],
                    "additionalProperties": False,
                },
            }
        },
    }


def parse_response(response: dict[str, Any]) -> tuple[str, str]:
    text = next((b["text"] for b in response.get("content", []) if b.get("type") == "text"), None)
    if text is None:
        raise CandidateError(f"Response has no text block (stop_reason={response.get('stop_reason')!r})")
    data = json.loads(text)
    question, reference = data["pregunta"].strip(), data["respuesta_referencia"].strip()
    if not question or not reference:
        raise CandidateError("Empty question or reference Answer")
    return question, reference


def copies_wording(question: str, seccion: Seccion) -> str | None:
    """Why the question reads like the regulation instead of a user, or None if it doesn't."""
    if _SECCION_ID_RE.search(question):
        return "nombra una Sección por número"
    if _ngrams(_words(question), _COPY_NGRAM) & _ngrams(_words(seccion.text), _COPY_NGRAM):
        return f"copia {_COPY_NGRAM} o más palabras seguidas del texto"
    return None


def generate(
    client: anthropic.Anthropic,
    model: str,
    parte: ParsedParte,
    n: int,
    exclude: set[str],
    seed: int | None = None,
    on_progress: Callable[[str], None] = lambda _msg: None,
) -> tuple[list[Candidate], list[str]]:
    """Candidates for up to n Secciones of `parte`, and a note per Sección whose candidate was discarded.

    A question that copies the regulatory wording gets one retry, told why; if it still copies, it is discarded.
    """
    candidates: list[Candidate] = []
    discarded: list[str] = []
    for seccion in sample_secciones(parte, n, exclude, seed):
        on_progress(f"Parte {parte.code}, Sección {seccion.id}...")
        request = build_request(model, parte.code, seccion)
        why = None
        for _attempt in range(2):
            response = client.messages.create(**request).to_dict()
            try:
                question, reference = parse_response(response)
            except (CandidateError, json.JSONDecodeError, KeyError) as e:
                why = f"respuesta inválida ({e})"
                continue
            why = copies_wording(question, seccion)
            if why is not None:
                request = _retry_request(request, response, why)
            else:
                candidates.append(
                    Candidate(
                        id=f"gen-{parte.code}-{seccion.id}",
                        parte=parte.code,
                        seccion=seccion.id,
                        seccion_title=seccion.title,
                        seccion_text=seccion.text,
                        question=question,
                        reference_answer=reference,
                        model=response.get("model", model),
                    )
                )
                break
        else:
            discarded.append(f"{parte.code}:{seccion.id}: {why}")
    return candidates, discarded


def _retry_request(request: dict[str, Any], response: dict[str, Any], why: str) -> dict[str, Any]:
    text = next(b["text"] for b in response["content"] if b.get("type") == "text")
    feedback = f"La pregunta {why}. Reescribila con las palabras de quien pregunta, sin copiar el texto regulatorio."
    messages = [*request["messages"], {"role": "assistant", "content": text}, {"role": "user", "content": feedback}]
    return {**request, "messages": messages}


def load_candidates(path: Path) -> list[Candidate]:
    if not path.exists():
        return []
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            c = Candidate(**json.loads(line))
        except (json.JSONDecodeError, TypeError) as e:
            raise CandidateError(f"{path}:{n}: malformed candidate ({e})") from e
        if c.status not in STATUSES:
            raise CandidateError(f"{path}:{n}: unknown status {c.status!r}")
        out.append(c)
    return out


def save_candidates(candidates: list[Candidate], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(asdict(c), ensure_ascii=False) + "\n" for c in candidates), encoding="utf-8")
    tmp.replace(path)


def covered_secciones(cases_path: Path, candidates: list[Candidate], parte: str) -> set[str]:
    """Secciones of `parte` that already have an eval case or a candidate (any status)."""
    covered = {c.seccion for c in candidates if c.parte == parte}
    if cases_path.exists():
        for line in cases_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                covered |= {e["seccion"] for e in json.loads(line)["expected_secciones"] if e["parte"] == parte}
    return covered


def accept(candidate: Candidate, cases_path: Path) -> None:
    """Append the candidate to the eval set; refuses a case id that is already there."""
    existing = cases_path.read_text(encoding="utf-8") if cases_path.exists() else ""
    ids = {json.loads(line)["id"] for line in existing.splitlines() if line.strip()}
    if candidate.id in ids:
        raise CandidateError(f"Eval case {candidate.id!r} already exists in {cases_path}")
    sep = "" if not existing or existing.endswith("\n") else "\n"
    with cases_path.open("a", encoding="utf-8") as f:
        f.write(sep + json.dumps(candidate.to_case(), ensure_ascii=False) + "\n")
    candidate.status = ACCEPTED


def _words(text: str) -> list[str]:
    plain = unicodedata.normalize("NFKD", text.lower())
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    return re.findall(r"[a-z0-9]+", plain)


def _ngrams(words: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def review(
    candidates_path: Path,
    cases_path: Path,
    read: Callable[[str], str] = input,
    write: Callable[[str], None] = print,
) -> dict[str, int]:
    """Walk pending candidates; accept, edit (then accept), reject or skip each one.

    Every decision is saved before the next candidate is shown, so quitting midway loses nothing.
    """
    candidates = load_candidates(candidates_path)
    pending = [c for c in candidates if c.status == PENDING]
    counts = {ACCEPTED: 0, REJECTED: 0, "saltado": 0}
    for i, c in enumerate(pending, 1):
        write(strings.REVIEW_CARD.format(
            n=i,
            total=len(pending),
            id=c.id,
            parte=c.parte,
            seccion=c.seccion,
            titulo=c.seccion_title,
            texto=_excerpt(c.seccion_text),
            pregunta=c.question,
            respuesta=c.reference_answer,
        ))
        while True:
            choice = read(strings.REVIEW_PROMPT).strip().lower()
            if choice in ("a", "e", "r", "s", "q"):
                break
            write(strings.REVIEW_UNKNOWN)
        if choice == "q":
            break
        if choice == "s":
            counts["saltado"] += 1
            continue
        if choice == "e":
            c.question = read(strings.REVIEW_EDIT_QUESTION).strip() or c.question
            c.reference_answer = read(strings.REVIEW_EDIT_ANSWER).strip() or c.reference_answer
        if choice == "r":
            c.status = REJECTED
        else:
            accept(c, cases_path)
        counts[c.status] += 1
        save_candidates(candidates, candidates_path)
    write(strings.REVIEW_SUMMARY.format(
        aceptados=counts[ACCEPTED],
        rechazados=counts[REJECTED],
        saltados=counts["saltado"],
        pendientes=sum(1 for c in candidates if c.status == PENDING),
    ))
    return counts


def _excerpt(text: str, limit: int = 1500) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + " [...]"
