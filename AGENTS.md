# raac-rag

A RAG web app: the user asks a free-text question about Argentine civil aviation regulations (RAAC) in a chat box and gets an answer backed by **citations** (RAAC Parte, section, page, link to the source PDF).

Python backend (FastAPI, streaming answers; ingestion and retrieval in Python because PageIndex and PyMuPDF are). Separate TypeScript frontend (React + PDF.js) that talks only to the API, the same API future clients will use. One managed Postgres holds the index registry (live and candidate version per Parte), PageIndex trees, and logs; the API keeps live trees in memory and reloads on promotion. Source PDFs live in object storage. Hosting must not cut off long streaming responses. Build/test/run commands live in the manifest files, not here.

## Where things live

- `CONTEXT.md` - domain glossary (RAAC, Parte, Subparte, Enmienda, DNAR, ...). Read it before naming anything in code, prompts, or UI; use its terms verbatim.
- `docs/adr/` - architecture decisions. Read the relevant ADR before changing retrieval, indexing, Parte routing, the LLM, or corpus scope.
- `research/` - findings on sources, tools, and approaches. Check here before researching something again.
- `docs/diagrams/` - Mermaid sources for the README diagrams. Any change that adds, removes, or rewires a component, pipeline stage, model, or store updates the affected `.mmd` files in the same change, and re-renders them with `just diagrams`. Commit only the PNGs whose source changed.

## Invariants

- **Grounded or silent.** Every claim in an answer carries a citation to a retrieved Sección; only Framing (lead-ins, connectives, a sí/no/depende verdict) may go uncited (ADR 0003). When retrieval finds no support, the answer says so and cites nothing. A plausible uncited claim is a bug, since pilots may act on it.
- **Citations are data, not text.** Each Citation comes from the Claude Citations API (`cited_text` + page range, verbatim from the source) joined with Parte, Sección, edition, and source URL from the index built at ingestion. Never parse citations out of model prose (ADR 0002).
- **Pages are PDF pages.** Store both the physical PDF page index (for `#page=N` deep links) and the printed page label if the document has one. Keep them separate.
- **Versioned corpus.** RAAC Partes get amended (see the ANAC "registro de enmiendas" and "RAAC históricas" pages). The version of a Parte is the PDF ANAC publishes, identified by its content hash; page footers may print different Ediciones and are kept per page, and a Citation shows the cited page's own (ADR 0004). Re-ingesting a Parte replaces its old index instead of adding to it. A changed PDF is indexed beside the live one and promoted only after automated checks pass (the PDF parses, sane page count, that Parte's eval cases green); otherwise the old index keeps serving and a human is alerted.
- **Every exchange is replayable.** Log the raw message, Standalone question, routed Partes, visited nodes, retrieved Secciones, Answer with Citations, per-step latency, tokens, cost, model IDs, and index version (Parte + content hash), keyed by a random conversation ID with no user identity. Retention is 90 days; thumbs-down exchanges are copied to eval candidates before expiry.
- **Spanish source, Spanish-first.** The corpus is Spanish legal text. Keep it untranslated in storage. Test retrieval with Spanish queries and with aviation jargon (English acronyms such as VFR, IFR, METAR, and Spanish terms such as "reglas de vuelo visual").

## UI

- Phone first. Tapping a Citation opens a sheet with the cited text in context and a "Ver en PDF" action that opens the PDF page (PDF.js) with the span highlighted; on desktop the same component is a side panel.
- Spanish only. All user-facing strings go through a strings file.
- A fixed notice states the app is not an official source and does not replace AIP, NOTAMs, or ANAC.
- Invite-only for now, with a global daily spend cap that switches the app to a "volvé mañana" state.

## Corpus source

- Human-facing index: https://www.argentina.gob.ar/anac/raac-dnar-regulaciones-argentinas-de-aviacion-civil/raac. Its table is rendered client-side from a public Google Sheet ("RAAC Vigentes").
- Machine index: the page HTML embeds a Poncho `ponchoTableOpciones` config whose `idSpread` is that sheet's ID (as of 2026-09: `1Xu4sfqfp29hLAHOC2i-FjfwFkKnxQj0uDoXFuT-ILOg`). Read `idSpread` from the page on every run and fetch `https://docs.google.com/spreadsheets/d/<idSpread>/export?format=csv`; never hardcode the ID, and fail loudly if the config is missing. Columns are `parte,titulo,btn-Ver,orden`. It is hand-maintained: row 2 repeats the headers in Spanish, titles are wrapped in `**` with stray whitespace, links can carry trailing newlines, `orden` is meaningless, and Parte codes are not always numeric (`HL`). Normalize every row and reject anything unexpected loudly.
- Each `btn-Ver` is a Nextcloud share link (`https://docs.anac.gob.ar/index.php/s/<token>`). Appending `/download` returns the PDF. A few (Parte 77 as of 2026-10) share a whole folder and point at the PDF with `?path=<folder>&openfile=<fileid>`; there `/download` returns a ZIP of the folder, so the PDF is resolved by fileid over public WebDAV (`public.php/webdav`, token as user).
- Share links are the stable citation URL. The PDF has no version in its URL, so detect changes by content hash.
- Related sections, currently out of scope unless an ADR adds them: DNAR, RAAC históricas, exenciones, RAIAAC.

## Quality

Retrieval quality is measured, not eyeballed. An eval set of question -> expected (Parte, section) pairs lives in the repo, and any change to parsing, indexing, routing, or prompts is checked against it before it is kept.

## Pull requests

Every PR description opens with what the PR does and why, in 2 lines at most. The rest of the description follows as usual.

## Issue tracking

Issues live on the GitHub project board https://github.com/users/Guernik/projects/1 (Status: Backlog, blocked, Ready, In progress, In review, Done). Keep it in sync whenever an issue changes state: claimed -> In progress, PR opened -> In review, closed -> Done. When an issue closes, re-check every open issue whose "Blocked by" lists it and move it from blocked to Ready once all its blockers are closed. Ready covers both `ready-for-agent` and `hitl` issues. `gh project` needs the `project` token scope (`gh auth refresh -s project`).
