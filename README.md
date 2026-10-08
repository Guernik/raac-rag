# raac-rag

Ask a free-text question about the Argentine civil aviation regulations (RAAC) and get an Answer in which every sentence cites the Parte, Sección, PDF page and Edición it comes from, with a link to the official ANAC PDF.

Not an official source. It does not replace AIP, NOTAMs or ANAC.

- Domain terms (Parte, Sección, Remisión, Edición, Enmienda, ...): [`CONTEXT.md`](CONTEXT.md)
- Architecture decisions: [`docs/adr/`](docs/adr/)
- Contributor and agent rules: [`AGENTS.md`](AGENTS.md)
- Commands (install, test, run): [`pyproject.toml`](pyproject.toml)

## Status

Today the pipeline runs end to end from the command line (`raac fetch`, `raac ask`, `raac eval`) over Partes 1, 61, 67 and 91, caching PDFs and PageIndex trees under `.raac/`. The web API, object storage, frontend, Postgres registry and automatic promotion of new Enmiendas are planned. The diagrams below mark planned pieces with dashed borders. Their Mermaid sources are in [`docs/diagrams/`](docs/diagrams/); after editing one, re-render the images with `just diagrams`.

## High-level architecture

<p align="center"><img src="docs/diagrams/architecture.png" alt="High-level architecture" width="480"></p>

Retrieval uses PageIndex in local mode (an LLM reads a tree of each Parte, no vector store, [ADR 0001](docs/adr/0001-pageindex-local-for-retrieval.md)). Answers are written with the Claude Citations API so every Citation is data returned by the API, never text parsed out of the model's prose ([ADR 0002](docs/adr/0002-answers-via-claude-citations.md)). Models per stage (in italics above) are set in [`src/raac/models.toml`](src/raac/models.toml). Eval reports record tokens and USD cost per stage (routing, search, answer) and in total, priced from [`src/raac/prices.toml`](src/raac/prices.toml).

## Corpus discovery and download

`raac fetch` finds every Parte in the RAAC vigente and downloads its PDF. Nothing about the sheet is hardcoded: its ID is read from the ANAC page on every run.

<p align="center"><img src="docs/diagrams/corpus-fetch.png" alt="Corpus discovery and download" width="440"></p>

The share link is the stable citation URL. It carries no version, so a new Enmienda shows up only as a new content hash.

## Indexing a Parte

<p align="center"><img src="docs/diagrams/indexing.png" alt="Indexing a Parte" width="780"></p>

The parser reads the Parte code from the page header and the Edición, Enmienda, date and printed page label from each page footer. Footers of one Parte may disagree, so they are kept per page and a Citation shows the cited page's own (ADR 0004). It keeps the physical PDF page (for `#page=N` links) and the printed page label apart: in Parte 61, Sección 61.535 is on PDF page 67 but printed page 10.

## Answering a question

<p align="center"><img src="docs/diagrams/answering.png" alt="Answering a question" width="780"></p>

Each retrieved Sección is sent as its own document whose content blocks are its PDF pages, so a citation's block range maps straight back to a Sección and its PDF pages.

### Parte routing

The RAAC is a shelf of rulebooks, one per Parte. Searching a whole Parte is expensive, because Opus walks its PageIndex tree. So before searching, a cheap model (Haiku 4.5) acts like a librarian and decides which books are worth opening ([`src/raac/router.py`](src/raac/router.py)).

1. **Cards (built once at load).** For each loaded Parte, `parte_card` makes an index card with the code, the título, the root summary PageIndex wrote for the document (cut to 1500 chars), and the titles of the tree's top-level nodes (its Subpartes).
2. **Ask.** The Retriever sends Haiku every card plus the question, with the instruction: "pick every Parte that probably answers this or part of it, most relevant first, or none."
3. **Can't make one up.** The reply is forced to a JSON schema where each item must be one of the loaded Parte codes. Haiku can't name a Parte that isn't indexed. `routed_partes` also removes duplicates and unknown codes.
4. **Search only those.** The Retriever runs tree search only in the chosen Partes. If the list is empty, nothing gets searched, so there's no retrieval and the answer is a refusal (grounded or silent).
5. **Shortcut.** If only one Parte is loaded, there's nothing to choose and Haiku isn't called.

## Grounded or silent

Every claim shown carries a Citation; only Framing (a lead-in, a connective, a sí/no/depende verdict) may go uncited ([ADR 0003](docs/adr/0003-uncited-framing-in-answers.md)). This is how the Answerer decides what to show:

<p align="center"><img src="docs/diagrams/grounding.png" alt="Grounded or silent: what the Answerer shows" width="480"></p>

## Domain model

<p align="center"><img src="docs/diagrams/domain-model.png" alt="Domain model" width="780"></p>

## Index lifecycle (planned)

Re-ingesting a Parte replaces its index instead of adding to it. A changed PDF is indexed beside the live one and only promoted when automated checks pass.

<p align="center"><img src="docs/diagrams/index-lifecycle.png" alt="Index lifecycle" width="400"></p>

The API keeps live trees in memory and reloads them on promotion. Every exchange is logged (question, routed Partes, visited nodes, Secciones, Answer with Citations, latency, tokens, cost, model IDs, index version) under a random conversation ID, so it can be replayed.
