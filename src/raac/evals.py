"""Eval harness: run eval cases against any Pipeline and score each stage separately.

Scores per case:
- retrieval: which expected Secciones are among the retrieved ones (in-scope cases); the
  Sección holding an attached Parte 1 Definición counts as retrieved;
- grounding: share of the Answer's sentences that carried a Citation, counting the
  uncited sentences the Answerer dropped (answered cases; a refusal cites nothing);
- refusal: whether the pipeline refused exactly when the case is out of scope.
Correctness against the reference Answer is scored separately (LLM judge, not here).
"""

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .pipeline import DefinicionRef, Pipeline, PipelineResult, SeccionRef

REPORT_SCHEMA_VERSION = 1
_CASE_KEYS = {"id", "question", "expected_secciones", "reference_answer", "out_of_scope", "source"}


class EvalCaseError(ValueError):
    pass


@dataclass(frozen=True)
class EvalCase:
    id: str
    question: str
    expected_secciones: list[SeccionRef]
    reference_answer: str
    out_of_scope: bool
    source: str = "owner"


def load_cases(path: Path) -> list[EvalCase]:
    """Read one JSON case per line; reject anything malformed instead of guessing."""
    cases: list[EvalCase] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        where = f"{path}:{n}"
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as e:
            raise EvalCaseError(f"{where}: invalid JSON ({e})") from e
        if not isinstance(raw, dict):
            raise EvalCaseError(f"{where}: a case must be a JSON object")
        if unknown := set(raw) - _CASE_KEYS:
            raise EvalCaseError(f"{where}: unknown keys {sorted(unknown)}")
        for key in ("id", "question", "reference_answer"):
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                raise EvalCaseError(f"{where}: {key!r} must be a non-empty string")
        if not isinstance(raw.get("out_of_scope"), bool):
            raise EvalCaseError(f"{where}: 'out_of_scope' must be true or false")
        expected = raw.get("expected_secciones")
        if not isinstance(expected, list) or not all(
            isinstance(e, dict) and set(e) == {"parte", "seccion"} and all(isinstance(v, str) and v for v in e.values())
            for e in expected
        ):
            raise EvalCaseError(f"{where}: 'expected_secciones' must be a list of {{parte, seccion}}")
        if raw["out_of_scope"] == bool(expected):
            raise EvalCaseError(f"{where}: out-of-scope cases have no expected Secciones; in-scope cases need some")
        case = EvalCase(
            id=raw["id"],
            question=raw["question"],
            expected_secciones=[SeccionRef(e["parte"], e["seccion"]) for e in expected],
            reference_answer=raw["reference_answer"],
            out_of_scope=raw["out_of_scope"],
            source=raw.get("source", "owner"),
        )
        if any(c.id == case.id for c in cases):
            raise EvalCaseError(f"{where}: duplicate case id {case.id!r}")
        cases.append(case)
    if not cases:
        raise EvalCaseError(f"{path}: no eval cases")
    return cases


@dataclass
class CaseScores:
    retrieval_hit: bool | None  # any expected Sección retrieved; None when out of scope
    retrieval_recall: float | None  # share of expected Secciones retrieved
    grounding: float | None  # cited sentences / all sentences written; None when refused
    refusal_correct: bool


def score(case: EvalCase, result: PipelineResult) -> CaseScores:
    retrieved = set(result.retrieved_secciones) | {SeccionRef(d.parte, d.seccion) for d in result.definiciones}
    hit = recall = None
    if not case.out_of_scope:
        found = [e for e in case.expected_secciones if e in retrieved]
        hit = bool(found)
        recall = len(found) / len(case.expected_secciones)
    answer = result.answer
    grounding = None
    if not answer.refused:
        cited = sum(1 for s in answer.sentences if s.citations)
        written = len(answer.sentences) + len(answer.dropped_uncited)
        grounding = cited / written
    return CaseScores(
        retrieval_hit=hit,
        retrieval_recall=recall,
        grounding=grounding,
        refusal_correct=answer.refused == case.out_of_scope,
    )


@dataclass
class CaseResult:
    id: str
    question: str
    out_of_scope: bool
    expected_secciones: list[SeccionRef]
    scores: CaseScores | None  # None when the pipeline raised
    latency_s: float
    routed_partes: list[str] = field(default_factory=list)
    retrieved_secciones: list[SeccionRef] = field(default_factory=list)
    definiciones: list[DefinicionRef] = field(default_factory=list)
    cited_secciones: list[SeccionRef] = field(default_factory=list)
    answer: dict[str, Any] | None = None
    served_model: str | None = None
    error: str | None = None


def run_case(pipeline: Pipeline, case: EvalCase) -> CaseResult:
    started = time.monotonic()
    try:
        result = pipeline.run(case.question)
    except Exception as e:  # one broken case must not lose the rest of the run
        return CaseResult(
            id=case.id,
            question=case.question,
            out_of_scope=case.out_of_scope,
            expected_secciones=case.expected_secciones,
            scores=None,
            latency_s=time.monotonic() - started,
            error=f"{type(e).__name__}: {e}",
        )
    cited = []
    for sentence in result.answer.sentences:
        for c in sentence.citations:
            ref = SeccionRef(c.parte, c.seccion)
            if ref not in cited:
                cited.append(ref)
    return CaseResult(
        id=case.id,
        question=case.question,
        out_of_scope=case.out_of_scope,
        expected_secciones=case.expected_secciones,
        scores=score(case, result),
        latency_s=time.monotonic() - started,
        routed_partes=result.routed_partes,
        retrieved_secciones=result.retrieved_secciones,
        definiciones=result.definiciones,
        cited_secciones=cited,
        answer=result.answer.to_dict(),
        served_model=result.answer.model,
    )


def aggregate(results: list[CaseResult]) -> dict[str, Any]:
    """Aggregate scores. A case that errored counts as a failure in every rate it belongs to."""
    in_scope = [r for r in results if not r.out_of_scope]
    out_scope = [r for r in results if r.out_of_scope]

    def rate(rows: list[CaseResult], ok) -> float | None:
        return sum(1 for r in rows if r.scores is not None and ok(r.scores)) / len(rows) if rows else None

    def mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    answered = [r for r in results if r.scores is None or r.scores.grounding is not None]
    return {
        "cases": len(results),
        "in_scope": len(in_scope),
        "out_of_scope": len(out_scope),
        "errors": sum(1 for r in results if r.error),
        "retrieval_hit_rate": rate(in_scope, lambda s: s.retrieval_hit),
        "retrieval_recall_mean": mean([r.scores.retrieval_recall if r.scores else 0.0 for r in in_scope]),
        "grounded_rate": rate(answered, lambda s: s.grounding == 1.0),
        "uncited_sentences": sum(len(r.answer["dropped_uncited"]) for r in results if r.answer),
        "refusal_rate_out_of_scope": rate(out_scope, lambda s: s.refusal_correct),
        "false_refusal_rate_in_scope": rate(in_scope, lambda s: not s.refusal_correct),
        "latency_s_mean": mean([r.latency_s for r in results]),
    }


def run_eval(pipeline: Pipeline, cases: list[EvalCase], cases_path: Path | None = None) -> dict[str, Any]:
    """Run every case and return the report (JSON-serializable, stable keys across pipelines)."""
    results = [run_case(pipeline, case) for case in cases]
    served = sorted({r.served_model for r in results if r.served_model})
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "pipeline": {
            "name": pipeline.name,
            "models": pipeline.models(),
            "served_answer_models": served,
            "index_versions": [asdict(v) for v in pipeline.index_versions()],
        },
        "cases_file": str(cases_path) if cases_path else None,
        "cases_sha256": _cases_hash(cases),
        "aggregate": aggregate(results),
        "cases": [asdict(r) for r in results],
    }


def write_report(report: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def summarize(report: dict[str, Any]) -> str:
    def fmt(value: Any) -> str:
        if value is None:
            return "-"
        return f"{value:.2f}" if isinstance(value, float) else str(value)

    lines = [f"{'case':<32} {'hit':>5} {'recall':>6} {'ground':>6} {'refusal':>7}  error"]
    for c in report["cases"]:
        s = c["scores"] or {}
        lines.append(
            f"{c['id']:<32} {fmt(s.get('retrieval_hit')):>5} {fmt(s.get('retrieval_recall')):>6} "
            f"{fmt(s.get('grounding')):>6} {fmt(s.get('refusal_correct')):>7}  {c['error'] or ''}"
        )
    lines.append("")
    lines += [f"{k}: {fmt(v)}" for k, v in report["aggregate"].items()]
    p = report["pipeline"]
    lines.append(f"pipeline: {p['name']} models={p['models']} served={p['served_answer_models']}")
    lines += [f"index: Parte {v['parte']} {v['content_hash'][:16]} Edición {v['edicion']} Enmienda {v['enmienda']}" for v in p["index_versions"]]
    return "\n".join(lines)


def _cases_hash(cases: list[EvalCase]) -> str:
    return hashlib.sha256(json.dumps([asdict(c) for c in cases], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
