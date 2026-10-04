"""CLI: `raac ask "<pregunta>"` answers from Partes 1, 61, 67 and 91 with Citations; `raac route` shows or
scores Parte routing; `raac fetch` downloads the RAAC vigente; `raac eval` runs the eval set."""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from . import corpus, evals, strings
from .answerer import Answer, Citation
from .config import load_env
from .pipeline import DEFAULT_PARTES, build_local_pipeline
from .router import routing_report

DEFAULT_CASES = Path("evals/cases.jsonl")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="raac")
    sub = ap.add_subparsers(dest="command", required=True)
    ask = sub.add_parser("ask", help="Answer a question with Citations")
    ask.add_argument("question")
    _add_corpus_args(ask)
    ask.add_argument("--record", type=Path, help="Write the question, document Secciones and raw Citations API response to this file")
    ask.add_argument("--record-routing", type=Path, help="Write the question and raw Parte routing response to this file")
    ask.add_argument("--json", action="store_true", help="Print the Retrieval and the Answer as JSON")
    route = sub.add_parser("route", help="Show which Partes a question is routed to, or score routing on eval cases")
    route.add_argument("question", nargs="?")
    route.add_argument("--cases", type=Path, help="JSONL eval cases; expected Partes come from expected_secciones")
    _add_corpus_args(route)
    ev = sub.add_parser("eval", help="Run the eval set against the local pipeline and write a report")
    ev.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    _add_corpus_args(ev)
    ev.add_argument("--out", type=Path, help="Report path; default evals/reports/<UTC timestamp>-local.json")
    fetch = sub.add_parser("fetch", help="Discover all Partes on the ANAC page and download their PDFs")
    fetch.add_argument("--dir", type=Path, default=Path(".raac/corpus"))
    fetch.add_argument("--parte", action="append", help="Only this Parte (repeatable)")
    args = ap.parse_args(argv)
    if args.command == "route" and not (args.question or args.cases):
        ap.error("route needs a question or --cases")
    load_env()
    if args.command == "fetch":
        return fetch_corpus(args.dir, args.parte)

    def progress(msg: str) -> None:
        print(msg, file=sys.stderr)

    if args.command == "eval":
        cases = evals.load_cases(args.cases)  # fail on a malformed case before spending on indexing
        pipeline = build_local_pipeline(args.parte or DEFAULT_PARTES, args.cache_dir, progress)
        report = evals.run_eval(pipeline, cases, args.cases)
        out = args.out or Path("evals/reports") / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{pipeline.name}.json"
        evals.write_report(report, out)
        print(evals.summarize(report))
        print(f"report: {out}")
        return 0

    pipeline = build_local_pipeline(
        args.parte or DEFAULT_PARTES,
        args.cache_dir,
        progress,
        routing_record_path=getattr(args, "record_routing", None),
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
            "followed_remisiones": run.followed_remisiones,
            "secciones": [f"{r.parte}:{r.seccion}" for r in run.retrieved_secciones],
            "answer": run.answer.to_dict(),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(render(run.answer))
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


def _add_corpus_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--parte", action="append", help=f"Parte to load (repeatable; default {' '.join(DEFAULT_PARTES)})")
    p.add_argument("--cache-dir", type=Path, default=Path(".raac"))
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
            key = (c.parte, c.seccion, c.pdf_page_start, c.pdf_page_end)
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
