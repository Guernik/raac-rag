"""CLI: `raac ask "<pregunta>"` answers from Parte 61 with Citations."""

import argparse
import json
import sys
from pathlib import Path

import anthropic

from . import corpus, indexer, strings
from .answerer import Answer, Citation, answer
from .config import load_models
from .parser import parse
from .retriever import Retriever


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="raac")
    sub = ap.add_subparsers(dest="command", required=True)
    ask = sub.add_parser("ask", help="Answer a question with Citations")
    ask.add_argument("question")
    ask.add_argument("--parte", default="61")
    ask.add_argument("--cache-dir", type=Path, default=Path(".raac"))
    ask.add_argument("--record", type=Path, help="Write the question, document Secciones and raw Citations API response to this file")
    ask.add_argument("--json", action="store_true", help="Print the Answer as JSON")
    args = ap.parse_args(argv)

    def progress(msg: str) -> None:
        print(msg, file=sys.stderr)

    models = load_models()
    args.cache_dir.mkdir(parents=True, exist_ok=True)

    progress(strings.PROGRESS_DOWNLOADING.format(parte=args.parte))
    listing = corpus.fetch_listing(args.parte)
    pdf = corpus.download(listing)
    pdf_path = args.cache_dir / f"raac-{listing.parte}-{pdf.sha256[:16]}.pdf"
    pdf_path.write_bytes(pdf.data)
    parsed = parse(pdf.data)

    progress(strings.PROGRESS_INDEXING.format(parte=parsed.code))
    storage = args.cache_dir / "pageindex"
    storage.mkdir(exist_ok=True)
    client = indexer.pageindex_client(models, storage)
    index = indexer.index(parsed, pdf_path, client, storage)

    retrieval = Retriever(client, [(parsed, index)], on_progress=progress).retrieve(args.question)
    progress(strings.PROGRESS_ANSWERING)
    result = answer(
        anthropic.Anthropic(),
        models.answer,
        args.question,
        retrieval,
        source_urls={parsed.code: listing.share_url},
        record_path=args.record,
    )
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render(result))
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
