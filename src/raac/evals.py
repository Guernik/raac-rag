"""Eval harness: run eval cases against any Pipeline and score each stage separately.

Scores per case:
- retrieval: which expected Secciones are among the retrieved ones (in-scope cases); the
  Sección holding an attached Parte 1 Definición counts as retrieved;
- grounding: share of the Answer's claims that carried a Citation: its cited sentences over
  those plus the unsupported claims the Answerer dropped; uncited Framing is not a claim
  (ADR 0003) (answered cases; a refusal cites nothing);
- refusal: whether the pipeline refused exactly when the case is out of scope;
- correctness: an LLM judge's verdict on the Answer against the case's reference Answer
  (judge.py), with its rationale kept in the report; only when a judge is given.

A run covers one stage or both (STAGES): `retrieval` runs routing + tree search and scores
retrieval only, recording each case's Retrieval; `answer` replays the Retrievals recorded in
an earlier report into the Answerer and scores grounding, refusal and correctness only. Scores
of a stage that did not run are None. The full run (both stages) stays the gate before a change
is kept, and is the only one a plain `Pipeline` (e.g. PageIndex Cloud) supports.
"""

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .answerer import Answer
from .judge import Correctness, CorrectnessJudge
from .pipeline import DefinicionRef, IndexVersion, Pipeline, PipelineRetrieval, SeccionRef, StagedPipeline
from .usage import STAGES as USAGE_STAGES
from .usage import StageUsage, merge, total_cost

REPORT_SCHEMA_VERSION = 5  # 4: tokens and cost per stage; 5: Framing, coverage and dropped claims in the Answer
_REPLAYABLE_SCHEMAS = {4, 5}  # same Retrieval fields
RETRIEVAL, ANSWER = "retrieval", "answer"
STAGES = {"full": (RETRIEVAL, ANSWER), RETRIEVAL: (RETRIEVAL,), ANSWER: (ANSWER,)}
_CASE_KEYS = {"id", "question", "expected_secciones", "reference_answer", "out_of_scope", "source"}


class EvalCaseError(ValueError):
    pass


class ReplayError(ValueError):
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


def select_cases(cases: list[EvalCase], ids: list[str] | None) -> list[EvalCase]:
    """The named cases, in file order; an unknown id fails instead of silently running fewer."""
    if not ids:
        return cases
    known = {c.id for c in cases}
    if unknown := [i for i in ids if i not in known]:
        raise EvalCaseError(f"unknown case ids {unknown}")
    return [c for c in cases if c.id in set(ids)]


@dataclass
class CaseScores:
    retrieval_hit: bool | None  # any expected Sección retrieved; None when out of scope
    retrieval_recall: float | None  # share of expected Secciones retrieved
    grounding: float | None  # cited sentences / (cited sentences + dropped claims); None when refused
    refusal_correct: bool | None  # None when the answer stage did not run
    correctness: float | None = None  # judge score (1 / 0.5 / 0); None when not judged or the judge failed


def score(case: EvalCase, retrieval: PipelineRetrieval | None, answer: Answer | None) -> CaseScores:
    """Score the stages that ran: pass None for a stage that did not."""
    hit = recall = grounding = refusal = None
    if retrieval is not None and not case.out_of_scope:
        retrieved = set(retrieval.retrieved_secciones) | {SeccionRef(d.parte, d.seccion) for d in retrieval.definiciones}
        found = [e for e in case.expected_secciones if e in retrieved]
        hit = bool(found)
        recall = len(found) / len(case.expected_secciones)
    if answer is not None:
        if not answer.refused:
            cited = sum(1 for s in answer.sentences if s.citations)
            claims = cited + len(answer.dropped)
            grounding = cited / claims if claims else 0.0
        refusal = answer.refused == case.out_of_scope
    return CaseScores(retrieval_hit=hit, retrieval_recall=recall, grounding=grounding, refusal_correct=refusal)


@dataclass
class CaseResult:
    id: str
    question: str
    out_of_scope: bool
    expected_secciones: list[SeccionRef]
    scores: CaseScores | None  # None when the pipeline raised
    latency_s: float
    routed_partes: list[str] = field(default_factory=list)
    visited_nodes: list[str] = field(default_factory=list)
    retrieved_secciones: list[SeccionRef] = field(default_factory=list)  # replayed ones in the answer stage
    definiciones: list[DefinicionRef] = field(default_factory=list)
    cited_secciones: list[SeccionRef] = field(default_factory=list)
    answer: dict[str, Any] | None = None
    served_model: str | None = None
    judge: Correctness | None = None  # verdict and rationale, for spot-checking the judge
    error: str | None = None
    usage: dict[str, StageUsage] = field(default_factory=dict)  # per stage, also for a case that raised
    cost_usd: float | None = None  # all stages; None when any stage's cost is unknown


def run_case(
    pipeline: Pipeline,
    case: EvalCase,
    stages: tuple[str, ...],
    recorded: PipelineRetrieval | None = None,
    judge: CorrectnessJudge | None = None,
) -> CaseResult:
    """Run `stages` for one case. The answer stage alone replays `recorded` instead of retrieving."""
    started = time.monotonic()
    result = CaseResult(
        id=case.id,
        question=case.question,
        out_of_scope=case.out_of_scope,
        expected_secciones=case.expected_secciones,
        scores=None,
        latency_s=0.0,
    )
    retrieval, answer, staged = recorded, None, None
    try:
        if stages == STAGES["full"]:
            run = pipeline.run(case.question)
            retrieval = PipelineRetrieval(run.routed_partes, run.retrieved_secciones, run.visited_nodes, run.definiciones)
            answer = run.answer
            result.usage = run.usage
        else:
            staged = _staged(pipeline)
            staged.take_usage()  # drop anything recorded outside this case
            if RETRIEVAL in stages:
                retrieval = staged.retrieve(case.question)
            if retrieval is None:
                raise ReplayError(f"case {case.id!r}: the answer stage needs a recorded Retrieval")
            if ANSWER in stages:
                answer = staged.answer(case.question, retrieval.retrieved_secciones, retrieval.definiciones)
            result.usage = staged.take_usage()
    except Exception as e:  # one broken case must not lose the rest of the run
        result.error = f"{type(e).__name__}: {e}"
        result.latency_s = time.monotonic() - started
        result.usage = (staged.take_usage() if staged else getattr(e, "usage", None)) or {}
        result.cost_usd = total_cost(result.usage)
        return result
    result.cost_usd = total_cost(result.usage)
    result.routed_partes = retrieval.routed_partes
    result.visited_nodes = retrieval.visited_nodes
    result.retrieved_secciones = retrieval.retrieved_secciones
    result.definiciones = retrieval.definiciones
    if answer is not None:
        cited = []
        for sentence in answer.sentences:
            for c in sentence.citations:
                ref = SeccionRef(c.parte, c.seccion)
                if ref not in cited:
                    cited.append(ref)
        result.cited_secciones = cited
        result.answer = answer.to_dict()
        result.served_model = answer.model
    result.scores = score(case, retrieval if RETRIEVAL in stages else None, answer)
    result.latency_s = time.monotonic() - started
    if judge is not None and answer is not None:
        result.judge = judge.judge(case.question, case.reference_answer, answer)
        result.scores.correctness = result.judge.score
    return result


def _staged(pipeline: Pipeline) -> StagedPipeline:
    if not all(hasattr(pipeline, m) for m in ("retrieve", "answer", "take_usage")):
        raise ReplayError(f"pipeline {pipeline.name!r} runs end to end only; it cannot run one stage")
    return pipeline  # type: ignore[return-value]


def aggregate(results: list[CaseResult], stages: tuple[str, ...] = STAGES["full"], judged: bool = False) -> dict[str, Any]:
    """Aggregate scores. A case that errored counts as a failure in every rate it belongs to.

    Rates of a stage that did not run are None. Correctness is averaged over cases the judge
    scored, plus pipeline errors as 0; a case whose judge call failed is left out and counted
    in `judge_errors`.
    """
    in_scope = [r for r in results if not r.out_of_scope]
    out_scope = [r for r in results if r.out_of_scope]

    def rate(rows: list[CaseResult], ok) -> float | None:
        return sum(1 for r in rows if r.scores is not None and ok(r.scores)) / len(rows) if rows else None

    def mean(values: list[float]) -> float | None:
        return sum(values) / len(values) if values else None

    answered = [r for r in results if r.scores is None or r.scores.grounding is not None]
    retrieval = RETRIEVAL in stages
    answer = ANSWER in stages
    correctness = None
    if judged:
        correctness = {
            "correctness_mean": mean(
                [r.scores.correctness if r.scores else 0.0 for r in results if r.scores is None or r.scores.correctness is not None]
            ),
            "judge_errors": sum(1 for r in results if r.judge and r.judge.error),
        }
    usage_stages = list(USAGE_STAGES) + sorted({s for r in results for s in r.usage} - set(USAGE_STAGES))
    usage = {s: merge([r.usage[s] for r in results if s in r.usage]) for s in usage_stages}
    cost = total_cost(usage)
    return {
        "cases": len(results),
        "in_scope": len(in_scope),
        "out_of_scope": len(out_scope),
        "errors": sum(1 for r in results if r.error),
        "retrieval_hit_rate": rate(in_scope, lambda s: s.retrieval_hit) if retrieval else None,
        "retrieval_recall_mean": mean([r.scores.retrieval_recall if r.scores else 0.0 for r in in_scope]) if retrieval else None,
        "grounded_rate": rate(answered, lambda s: s.grounding == 1.0) if answer else None,
        "dropped_claims": sum(len(r.answer["dropped"]) for r in results if r.answer) if answer else None,
        "refusal_rate_out_of_scope": rate(out_scope, lambda s: s.refusal_correct) if answer else None,
        "false_refusal_rate_in_scope": rate(in_scope, lambda s: s.refusal_correct is False) if answer else None,
        **(correctness or {}),
        "latency_s_mean": mean([r.latency_s for r in results]),
        "cost_usd": cost,
        "cost_usd_mean": cost / len(results) if cost is not None and results else None,
        "usage": {s: asdict(u) for s, u in usage.items()},
    }


def run_eval(
    pipeline: Pipeline,
    cases: list[EvalCase],
    cases_path: Path | None = None,
    stage: str = "full",
    retrievals: "RecordedRetrievals | None" = None,
    judge: CorrectnessJudge | None = None,
) -> dict[str, Any]:
    """Run every case through `stage` and return the report (JSON-serializable, stable keys across pipelines).

    The answer stage alone replays `retrievals` (see load_retrievals) and needs one for every case.
    The judge only runs with the answer stage.
    """
    stages = STAGES[stage]
    if ANSWER not in stages:
        judge = None
    recorded: dict[str, PipelineRetrieval] = {}
    if RETRIEVAL not in stages:
        if retrievals is None:
            raise ReplayError("the answer stage needs recorded Retrievals (--retrievals <report>)")
        recorded = retrievals.for_cases(cases)
    results = [run_case(pipeline, case, stages, recorded.get(case.id), judge) for case in cases]
    served = sorted({r.served_model for r in results if r.served_model})
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "stages": list(stages),
        "retrievals_from": str(retrievals.path) if retrievals and RETRIEVAL not in stages else None,
        "pipeline": {
            "name": pipeline.name,
            "models": pipeline.models(),
            "served_answer_models": served,
            "index_versions": [asdict(v) for v in pipeline.index_versions()],
        },
        "cases_file": str(cases_path) if cases_path else None,
        "cases_sha256": _cases_hash(cases),
        "judge": None
        if judge is None
        else {
            "model": judge.model,
            "served_models": sorted({r.judge.model for r in results if r.judge and r.judge.model}),
        },
        "aggregate": aggregate(results, stages, judged=judge is not None),
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

    def usd(value: float | None) -> str:
        return "?" if value is None else f"${value:.4f}"

    lines = [f"stages: {', '.join(report['stages'])}"]
    if report.get("retrievals_from"):
        lines.append(f"retrievals replayed from: {report['retrievals_from']}")
    lines += [f"{'case':<32} {'hit':>5} {'recall':>6} {'ground':>6} {'refusal':>7} {'correct':>7} {'cost':>9}  error"]
    for c in report["cases"]:
        s = c["scores"] or {}
        error = c["error"] or ((c.get("judge") or {}).get("error") and f"judge: {c['judge']['error']}") or ""
        lines.append(
            f"{c['id']:<32} {fmt(s.get('retrieval_hit')):>5} {fmt(s.get('retrieval_recall')):>6} "
            f"{fmt(s.get('grounding')):>6} {fmt(s.get('refusal_correct')):>7} {fmt(s.get('correctness')):>7} "
            f"{usd(c['cost_usd']):>9}  {error}"
        )
    judged = [c for c in report["cases"] if c.get("judge") and c["judge"]["verdict"]]
    if judged:
        lines.append("")
        lines += [f"{c['id']}: {c['judge']['verdict']} - {c['judge']['rationale']}" for c in judged]
    lines.append("")
    agg = report["aggregate"]
    lines += [f"{k}: {fmt(v)}" for k, v in agg.items() if k not in ("usage", "cost_usd", "cost_usd_mean")]
    lines += ["", f"{'stage':<10} {'calls':>5} {'input':>9} {'cache_w':>9} {'cache_r':>9} {'output':>8} {'cost':>9}  models"]
    for stage, u in agg["usage"].items():
        notes = [*u["models"]]
        if u["unpriced_models"]:
            notes.append(f"unpriced: {', '.join(u['unpriced_models'])}")
        if u["unmetered_requests"]:
            notes.append(f"{u['unmetered_requests']} calls unmetered")
        lines.append(
            f"{stage:<10} {u['requests']:>5} {u['input_tokens']:>9} {u['cache_creation_input_tokens']:>9} "
            f"{u['cache_read_input_tokens']:>9} {u['output_tokens']:>8} {usd(u['cost_usd']):>9}  {'; '.join(notes)}"
        )
    lines.append(f"total cost: {usd(agg['cost_usd'])} ({usd(agg['cost_usd_mean'])} per case)")
    p = report["pipeline"]
    lines.append(f"pipeline: {p['name']} models={p['models']} served={p['served_answer_models']}")
    if report.get("judge"):
        lines.append(f"judge: {report['judge']['model']} served={report['judge']['served_models']}")
    lines += [
        f"index: Parte {v['parte']} {v['content_hash'][:16]} Edición {v['edicion']} Enmienda {v['enmienda']}"
        + (" (cached PDF)" if v["from_cache"] else "")
        for v in p["index_versions"]
    ]
    return "\n".join(lines)


def _cases_hash(cases: list[EvalCase]) -> str:
    return hashlib.sha256(json.dumps([asdict(c) for c in cases], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@dataclass
class RecordedRetrievals:
    """The per-case Retrievals of an earlier report that ran the retrieval stage, for replay."""

    path: Path
    index_versions: list[IndexVersion]
    by_case: dict[str, tuple[str, PipelineRetrieval]]  # case id -> (question, Retrieval)
    errored: set[str]

    def for_cases(self, cases: list[EvalCase]) -> dict[str, PipelineRetrieval]:
        """One recorded Retrieval per case; fail loudly on a missing, errored or changed case."""
        missing = [c.id for c in cases if c.id not in self.by_case and c.id not in self.errored]
        errored = [c.id for c in cases if c.id in self.errored]
        changed = [c.id for c in cases if c.id in self.by_case and self.by_case[c.id][0] != c.question]
        problems = []
        if missing:
            problems.append(f"no recorded Retrieval for {missing}")
        if errored:
            problems.append(f"the recorded run errored for {errored} (leave them out with --case)")
        if changed:
            problems.append(f"the question changed since the recording for {changed}")
        if problems:
            raise ReplayError(f"{self.path}: " + "; ".join(problems))
        return {c.id: self.by_case[c.id][1] for c in cases}


def load_retrievals(path: Path) -> RecordedRetrievals:
    """Read the Retrievals recorded in a report that ran the retrieval stage (alone or end to end)."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ReplayError(f"{path}: cannot read report ({e})") from e
    if report.get("schema_version") not in _REPLAYABLE_SCHEMAS:
        raise ReplayError(f"{path}: report schema {report.get('schema_version')}, expected one of {sorted(_REPLAYABLE_SCHEMAS)}")
    if RETRIEVAL not in report["stages"]:
        raise ReplayError(f"{path}: this report did not run the retrieval stage, so it holds no Retrieval to replay")
    by_case = {}
    errored = set()
    for c in report["cases"]:
        if c["error"]:
            errored.add(c["id"])
            continue
        by_case[c["id"]] = (
            c["question"],
            PipelineRetrieval(
                routed_partes=c["routed_partes"],
                retrieved_secciones=[SeccionRef(**r) for r in c["retrieved_secciones"]],
                visited_nodes=c["visited_nodes"],
                definiciones=[DefinicionRef(**d) for d in c["definiciones"]],
            ),
        )
    versions = [IndexVersion(**v) for v in report["pipeline"]["index_versions"]]
    return RecordedRetrievals(path, versions, by_case, errored)
