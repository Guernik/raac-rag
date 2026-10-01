# Reasoning-based retrieval with PageIndex local, no vector store

RAAC Partes are deeply nested legal text (Parte > Capítulo > Sección > inciso) whose wording rarely matches how users phrase questions, which is where embedding similarity is weakest. We retrieve with PageIndex OSS in local mode: one tree index per Parte, searched by an LLM. PageIndex Cloud was tested and worked well, but we rejected it to own the core. Its cross-document search, block-level citations and OCR are cloud-only, so we build our own equivalents: a **Parte routing** step that picks which Partes to search, and **Sección** geometry extracted at ingestion that drives highlighting. The RAAC PDFs are text-based, so no OCR is needed.

## Consequences

- PageIndex sits behind our own retrieval interface. If the eval set shows it falling short, a vector or hybrid retriever replaces it behind the same interface.
- Gate: before committing, Local plus our routing must match Cloud on the first ~30 eval questions.
- Each question costs several sequential LLM calls, so answers are streamed with visible progress.
