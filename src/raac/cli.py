"""CLI: `raac ask "<pregunta>"` answers from Partes 1, 61, 67 and 91 with Citations; `raac route` shows or
scores Parte routing; `raac fetch` downloads the RAAC vigente; `raac eval` runs the eval set against the local pipeline
(end to end or one stage) or PageIndex Cloud; `raac generate-cases` writes candidate eval cases and `raac review`
accepts, edits or rejects them; `raac serve` runs the HTTP API."""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import anthropic
import httpx

from . import casegen, cloud, corpus, evals, strings
from .answerer import Answer, Citation
from .config import load_env, load_judge_model, load_tool_model
from .judge import CorrectnessJudge
from .parser import parse
from .pipeline import DEFAULT_PARTES, build_answer_pipeline, build_local_pipeline
from .router import routing_report

DEFAULT_CASES = Path("evals/cases.jsonl")
DEFAULT_CANDIDATES = Path("evals/candidates.jsonl")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="raac")
    sub = ap.add_subparsers(dest="command", required=True)
    ask = sub.add_parser("ask", help="Answer a question with Citations")
    ask.add_argument("question")
    _add_corpus_args(ask)
    ask.add_argument("--record", type=Path, help="Write the question, document Secciones and raw Citations API response to this file")
    ask.add_argument("--record-routing", type=Path, help="Write the question and raw Parte routing response to this file")
    ask.add_argument("--json", action="store_true", help="Print the Retrieval and the Answer as JSON")
    serve = sub.add_parser("serve", help="Run the HTTP API (POST /api/ask streams an Answer with Citations)")
    _add_corpus_args(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    route = sub.add_parser("route", help="Show which Partes a question is routed to, or score routing on eval cases")
    route.add_argument("question", nargs="?")
    route.add_argument("--cases", type=Path, help="JSONL eval cases; expected Partes come from expected_secciones")
    _add_corpus_args(route)
    ev = sub.add_parser("eval", help="Run the eval set against a pipeline and write a report")
    ev.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    ev.add_argument(
        "--pipeline",
        choices=["local", "cloud"],
        default="local",
        help="local (ours) or cloud (PageIndex Cloud baseline; needs PAGEINDEX_API_KEY)",
    )
    _add_corpus_args(ev)
    ev.add_argument("--out", type=Path, help="Report path; default evals/reports/<UTC timestamp>-<pipeline>[-<stage>].json")
    ev.add_argument("--no-judge", action="store_true", help="Skip the correctness judge (no reference Answer comparison)")
    ev.add_argument(
        "--stage",
        choices=list(evals.STAGES),
        default="full",
        help="full (default, the gate before keeping a change); retrieval: routing + tree search only; "
        "answer: replay --retrievals into the Answerer only",
    )
    ev.add_argument("--retrievals", type=Path, help="With --stage answer: a report whose recorded Retrievals are replayed")
    ev.add_argument("--case", action="append", help="Only this case id (repeatable)")
    fetch = sub.add_parser("fetch", help="Discover all Partes on the ANAC page and download their PDFs")
    fetch.add_argument("--dir", type=Path, default=Path(".raac/corpus"))
    fetch.add_argument("--parte", action="append", help="Only this Parte (repeatable)")
    gen = sub.add_parser("generate-cases", help="Write candidate eval cases from sampled Secciones for review")
    gen.add_argument("--parte", action="append", default=[], help="Parte to sample, downloaded from ANAC (repeatable)")
    gen.add_argument("--pdf", action="append", type=Path, default=[], help="Local Parte PDF to sample instead (repeatable)")
    gen.add_argument("-n", type=int, default=5, help="Candidates per Parte (default 5)")
    gen.add_argument("--seed", type=int, help="Sampling seed, for a repeatable sample")
    gen.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    gen.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="Secciones already covered here are skipped")
    rev = sub.add_parser("review", help="Accept, edit or reject pending candidate eval cases")
    rev.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    rev.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="Accepted candidates are appended here")
    args = ap.parse_args(argv)
    if args.command == "generate-cases" and not (args.parte or args.pdf):
        ap.error("generate-cases needs --parte or --pdf")
    if args.command == "route" and not (args.question or args.cases):
        ap.error("route needs a question or --cases")
    if args.command == "eval":
        if (args.stage == evals.ANSWER) != bool(args.retrievals):
            ap.error("--retrievals goes with --stage answer, and --stage answer needs it")
        if args.stage == evals.ANSWER and args.parte:
            ap.error("--stage answer loads the Partes recorded in --retrievals; drop --parte")
        if args.stage != "full" and args.pipeline == "cloud":
            ap.error("--pipeline cloud runs end to end only; drop --stage")
    load_env()
    if args.command == "serve":
        return serve_api(args)
    if args.command == "fetch":
        return fetch_corpus(args.dir, args.parte)
    if args.command == "review":
        casegen.review(args.candidates, args.cases)
        return 0
    if args.command == "generate-cases":
        return generate_cases(args)

    def progress(msg: str) -> None:
        print(msg, file=sys.stderr)

    if args.command == "eval":
        # Fail on a malformed case, unknown --case or unusable recording before spending on indexing.
        cases = evals.select_cases(evals.load_cases(args.cases), args.case)
        judge = None
        if evals.ANSWER in evals.STAGES[args.stage] and not args.no_judge:
            judge = CorrectnessJudge(anthropic.Anthropic(), load_judge_model())
        retrievals = None
        if args.pipeline == "cloud":
            try:
                pipeline = cloud.build_cloud_pipeline(
                    args.parte or DEFAULT_PARTES, args.cache_dir, progress, offline=args.offline
                )
            except cloud.CloudKeyMissing as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
        elif args.stage == evals.ANSWER:
            retrievals = evals.load_retrievals(args.retrievals)
            retrievals.for_cases(cases)
            pipeline = build_answer_pipeline(retrievals.index_versions, args.cache_dir, progress)
        else:
            pipeline = build_local_pipeline(args.parte or DEFAULT_PARTES, args.cache_dir, progress, offline=args.offline)
        report = evals.run_eval(pipeline, cases, args.cases, args.stage, retrievals, judge)
        suffix = "" if args.stage == "full" else f"-{args.stage}"
        out = args.out or Path("evals/reports") / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{pipeline.name}{suffix}.json"
        evals.write_report(report, out)
        print(evals.summarize(report))
        print(f"report: {out}")
        return 0

    pipeline = build_local_pipeline(
        args.parte or DEFAULT_PARTES,
        args.cache_dir,
        progress,
        routing_record_path=getattr(args, "record_routing", None),
        offline=args.offline,
    )
    if args.command == "route":
        if args.cases:
            report = routing_report(pipeline.router, load_cases(args.cases))
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(" ".join(pipeline.router.route(args.question)))
        return 0

    run = pipeline.run(args.question, record_path=args.record)
    if args.json:
        out = {
            "routed_partes": run.routed_partes,
            "visited_nodes": run.visited_nodes,
            "secciones": [f"{r.parte}:{r.seccion}" for r in run.retrieved_secciones],
            "definiciones": [f"{d.parte}:{d.seccion}:{d.term}" for d in run.definiciones],
            "answer": run.answer.to_dict(),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(render(run.answer))
    return 0


def serve_api(args: argparse.Namespace) -> int:
    import logging

    import uvicorn

    from .api import create_app

    logging.basicConfig(level=logging.INFO)
    app = create_app(
        lambda on_progress: build_local_pipeline(
            args.parte or DEFAULT_PARTES, args.cache_dir, on_progress, offline=args.offline
        )
    )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def fetch_corpus(root: Path, only: list[str] | None = None) -> int:
    client = httpx.Client(follow_redirects=True, timeout=120)
    listings = corpus.list_partes(client)
    if only:
        listings = [corpus.find_listing(listings, p) for p in only]
    store = corpus.CorpusStore(root)
    counts = {corpus.CorpusStore.NEW: 0, corpus.CorpusStore.CHANGED: 0, corpus.CorpusStore.UNCHANGED: 0}
    for listing in listings:
        pdf = corpus.download(listing, client)
        status = store.put(listing, pdf)
        counts[status] += 1
        print(
            strings.FETCH_LINE.format(
                parte=listing.parte, estado=strings.FETCH_STATUS[status], sha256=pdf.sha256[:12], titulo=listing.titulo
            ),
            flush=True,
        )
    print(strings.FETCH_SUMMARY.format(total=len(listings), **counts))
    return 0


def generate_cases(args: argparse.Namespace) -> int:
    pdfs = [p.read_bytes() for p in args.pdf]
    pdfs += [corpus.download(listing).data for listing in corpus.fetch_listings(args.parte)] if args.parte else []
    model = load_tool_model("casegen")
    client = anthropic.Anthropic()
    candidates = casegen.load_candidates(args.candidates)
    added = discarded = 0
    for data in pdfs:
        parte = parse(data)
        exclude = casegen.covered_secciones(args.cases, candidates, parte.code)
        new, notes = casegen.generate(
            client, model, parte, args.n, exclude, args.seed, on_progress=lambda m: print(m, file=sys.stderr)
        )
        candidates += new
        casegen.save_candidates(candidates, args.candidates)  # keep what was paid for if a later Parte fails
        added += len(new)
        discarded += len(notes)
        for note in notes:
            print(strings.GENERATE_DISCARDED.format(nota=note), file=sys.stderr)
    print(strings.GENERATE_SUMMARY.format(generados=added, path=args.candidates, descartados=discarded))
    return 0


def _add_corpus_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--parte", action="append", help=f"Parte to load (repeatable; default {' '.join(DEFAULT_PARTES)})")
    p.add_argument("--cache-dir", type=Path, default=Path(".raac"))
    p.add_argument("--offline", action="store_true", help="Skip ANAC and use the PDFs cached under <cache-dir>/corpus")
    p.set_defaults(parte=None)


def load_cases(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def render(result: Answer) -> str:
    numbered: list[Citation] = []
    keys: dict[tuple, int] = {}

    def marked(text: str, citations: list[Citation]) -> str:
        if not citations:
            raise ValueError(f"Uncited sentence must not reach the output: {text!r}")
        marks = []
        for c in citations:
            key = (c.parte, c.seccion, c.definicion, c.pdf_page_start, c.pdf_page_end)
            if key not in keys:
                numbered.append(c)
                keys[key] = len(numbered)
            if keys[key] not in marks:
                marks.append(keys[key])
        return text + " " + "".join(f"[{n}]" for n in marks)

    body = []
    if result.refused:
        body.append(strings.REFUSAL)
    elif result.incomplete:
        body.append(strings.INCOMPLETE)
    body += [marked(s.text, s.citations) for s in result.sentences]
    body += [
        marked(strings.LIKELY_PARTE.format(parte=lp.parte, seccion=lp.citation.seccion), [lp.citation])
        for lp in result.likely_partes
    ]
    lines = [strings.NOTICE, "", " ".join(body)]
    if not numbered:
        return "\n".join(lines)
    lines += ["", strings.CITATIONS_HEADER]
    for n, c in enumerate(numbered, 1):
        if c.pdf_page_start == c.pdf_page_end:
            paginas = strings.PAGES_SINGLE.format(pdf=c.pdf_page_start, impresa=c.printed_page_start)
        else:
            paginas = strings.PAGES_RANGE.format(
                pdf_start=c.pdf_page_start,
                pdf_end=c.pdf_page_end,
                impresa_start=c.printed_page_start,
                impresa_end=c.printed_page_end,
            )
        version = strings.version(c.edicion, c.enmienda, c.fecha)
        fields = dict(n=n, parte=c.parte, seccion=c.seccion, titulo=c.seccion_title, paginas=paginas, url=c.source_url)
        if version:
            lines.append(strings.CITATION.format(version=version, **fields))
        else:
            lines.append(strings.CITATION_NO_VERSION.format(**fields))
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
