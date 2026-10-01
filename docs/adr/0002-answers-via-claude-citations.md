# Answers are written with the Claude Citations API, not PageIndex chat

PageIndex decides which pages of which Partes to read; our own answer call then passes those pages to Claude as document blocks with `citations` enabled. The API returns each claim with `cited_text` and a page range drawn verbatim from the source, which gives us grounded citations as data (no parsing citations out of prose), an exact span to highlight in the PDF, and a visible signal when a sentence has no support. PageIndex's built-in chat was rejected because its citations are page-level text we would have to trust and parse.

## Consequences

- Citations are incompatible with structured outputs (`output_config.format`), so the answer call returns text blocks, not JSON.
- Models: Haiku 4.5 for indexing summaries, Standalone question rewriting and Parte routing; Opus 5.5 for tree search and answers. Swapping models, including Sonnet 5.5 for tree search, is an eval-gated config change.
