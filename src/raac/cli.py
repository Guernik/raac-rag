"""CLI: `raac ask "<pregunta>"` answers from Parte 61 with Citations; `raac fetch` downloads the RAAC vigente;
`raac eval` runs the eval set."""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

from . import corpus, evals, strings
from .answerer import Answer, Citation
from .config import load_env
from .pipeline import build_local_pipeline

DEFAULT_CASES = Path("evals/cases.jsonl")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="raac")
    sub = ap.add_subparsers(dest="command", required=True)
    ask = sub.add_parser("ask", help="Answer a question with Citations")
    ask.add_argument("question")
    ask.add_argument("--parte", default="61")
    ask.add_argument("--cache-dir", type=Path, default=Path(".raac"))
    ask.add_argument("--record", type=Path, help="Write the question, document Secciones and raw Citations API response to this file")
    ask.add_argument("--json", action="store_true", help="Print the Answer as JSON")
    ev = sub.add_parser("eval", help="Run the eval set against the local pipeline and write a report")
    ev.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    ev.add_argument("--parte", action="append", help="Parte to load (repeatable); default 61")
    ev.add_argument("--cache-dir", type=Path, default=Path(".raac"))
    ev.add_argument("--out", type=Path, help="Report path; default evals/reports/<UTC timestamp>-local.json")
    fetch = sub.add_parser("fetch", help="Discover all Partes on the ANAC page and download their PDFs")
    fetch.add_argument("--dir", type=Path, default=Path(".raac/corpus"))
    fetch.add_argument("--parte", action="append", help="Only this Parte (repeatable)")
    args = ap.parse_args(argv)
    load_env()
    if args.command == "fetch":
        return fetch_corpus(args.dir, args.parte)

    def progress(msg: str) -> None:
        print(msg, file=sys.stderr)

    if args.command == "eval":
        cases = evals.load_cases(args.cases)  # fail on a malformed case before spending on indexing
        pipeline = build_local_pipeline(args.parte or ["61"], args.cache_dir, progress)
        report = evals.run_eval(pipeline, cases, args.cases)
        out = args.out or Path("evals/reports") / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{pipeline.name}.json"
        evals.write_report(report, out)
        print(evals.summarize(report))
        print(f"report: {out}")
        return 0

    pipeline = build_local_pipeline([args.parte], args.cache_dir, progress)
    result = pipeline.run(args.question, record_path=args.record).answer
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render(result))
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
        lines.append(
            strings.CITATION.format(
                n=n,
                parte=c.parte,
                seccion=c.seccion,
                titulo=c.seccion_title,
                paginas=paginas,
                edicion=c.edicion,
                enmienda=c.enmienda,
                url=c.source_url,
            )
        )
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
