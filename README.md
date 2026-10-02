# raac-rag

Ask a free-text question about the Argentine civil aviation regulations (RAAC) and get an Answer in which every sentence cites the Parte, Sección, PDF page and Edición it comes from, with a link to the official ANAC PDF.

Not an official source. It does not replace AIP, NOTAMs or ANAC.

- Domain terms (Parte, Sección, Remisión, Edición, Enmienda, ...): [`CONTEXT.md`](CONTEXT.md)
- Architecture decisions: [`docs/adr/`](docs/adr/)
- Contributor and agent rules: [`AGENTS.md`](AGENTS.md)
- Commands (install, test, run): [`pyproject.toml`](pyproject.toml)

## Status

Today the pipeline runs end to end from the command line (`raac fetch`, `raac ask`) over Parte 61, caching PDFs and PageIndex trees under `.raac/`. The web API, object storage, frontend, Postgres registry and automatic promotion of new Enmiendas are planned. The diagrams below mark planned pieces with dashed borders.

## High-level architecture

```mermaid
flowchart TB
    subgraph ANAC["ANAC public sources"]
        direction LR
        page["RAAC web page<br/>(Poncho table config)"] -- idSpread --> sheet["Google Sheet<br/>'RAAC Vigentes' (CSV)"]
        nc["Nextcloud share links<br/>(one PDF per Parte)"]
    end

    subgraph ingest["Ingestion"]
        direction LR
        corpus["corpus.py<br/>discover + download"] --> parser["parser.py<br/>PDF → Secciones,<br/>pages, footer"] --> indexer["indexer.py<br/>PageIndex tree<br/>per Parte"]
    end

    subgraph storage["Storage"]
        direction LR
        cache[(".raac/ local cache<br/>PDFs + trees")]
        pdfs[("Object storage<br/>source PDFs")]
        pg[("Postgres<br/>index registry,<br/>trees, logs")]
    end

    subgraph serve["Answering"]
        direction LR
        retriever["retriever.py<br/>tree search"] --> answerer["answerer.py<br/>Citations API"]
    end

    subgraph clients["Clients"]
        direction LR
        cli["cli.py<br/>raac ask / fetch"]
        api["FastAPI<br/>streaming answers"] --> web["React + PDF.js<br/>phone-first web app"]
    end

    claude{{"Claude API<br/>Haiku 4.5 · Opus 5.5"}}

    sheet -- share links --> corpus
    nc -- PDF bytes --> corpus
    ingest --> storage
    storage --> serve
    answerer --> cli
    answerer --> api
    ingest <-. node summaries .-> claude
    serve <-. search + cited answer .-> claude

    classDef planned stroke-dasharray: 5 5
    class api,web,pg,pdfs planned
```

Retrieval uses PageIndex in local mode (an LLM reads a tree of each Parte, no vector store, [ADR 0001](docs/adr/0001-pageindex-local-for-retrieval.md)). Answers are written with the Claude Citations API so every Citation is data returned by the API, never text parsed out of the model's prose ([ADR 0002](docs/adr/0002-answers-via-claude-citations.md)). The model for each stage is set in [`src/raac/models.toml`](src/raac/models.toml).

## Corpus discovery and download

`raac fetch` finds every Parte in the RAAC vigente and downloads its PDF. Nothing about the sheet is hardcoded: its ID is read from the ANAC page on every run.

```mermaid
flowchart TD
    A["GET ANAC RAAC page"] --> B{"ponchoTableOpciones<br/>has idSpread?"}
    B -- no --> X["fail loudly"]
    B -- yes --> C["GET sheet export?format=csv"]
    C --> D["normalize rows<br/>(skip Spanish header row, strip **, whitespace,<br/>trailing newlines; codes like HL allowed)"]
    D --> E{"row valid?"}
    E -- no --> X
    E -- yes --> F{"share link type"}
    F -- "file share" --> G["GET &lt;share&gt;/download"]
    F -- "folder share<br/>(?path=…&openfile=…)" --> H["public WebDAV:<br/>resolve fileid → PDF"]
    G --> I["check %PDF, sha256"]
    H --> I
    I --> J{"hash vs manifest"}
    J -- "no entry" --> K["new"]
    J -- different --> L["changed"]
    J -- same --> M["unchanged"]
    K & L & M --> N["&lt;root&gt;/&lt;parte&gt;/&lt;sha256&gt;.pdf<br/>+ manifest.json"]
```

The share link is the stable citation URL. It carries no version, so a new Enmienda shows up only as a new content hash.

## Indexing a Parte

```mermaid
flowchart LR
    pdf["Parte PDF"] --> parse["ParteParser<br/>(PyMuPDF)"]
    parse --> pp["ParsedParte<br/>code · Edición · Enmienda<br/>content hash · Secciones"]
    pdf --> pi["PageIndex local<br/>(Haiku 4.5 summaries)"]
    pi --> tree["tree of nodes<br/>(page ranges + summaries)"]
    pp --> attach["attach Sección ids<br/>to each node"]
    tree --> attach
    attach --> idx["ParteIndex<br/>cached by Parte + content hash"]
```

The parser reads the Parte code from the page header and the Edición, Enmienda and printed page label from each page footer. It keeps the physical PDF page (for `#page=N` links) and the printed page label apart: in Parte 61, Sección 61.535 is on PDF page 67 but printed page 10.

## Answering a question

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant C as CLI / API
    participant R as Retriever
    participant P as PageIndex agent<br/>(Opus 5.5)
    participant A as Answerer
    participant L as Claude Citations API<br/>(Opus 5.5)

    U->>C: question (Spanish)
    Note over C: planned: rewrite into a Standalone question<br/>and route to likely Partes (Haiku 4.5)
    C->>R: Standalone question
    loop each loaded Parte
        R->>P: chat(question, tree of the Parte)
        P-->>R: tool calls get_page_content(pages)
    end
    R->>R: PDF pages read → Secciones
    R->>A: Retrieval (Secciones + visited nodes)
    A->>L: one document per Sección<br/>(one content block per PDF page), citations on
    L-->>A: text blocks with cited_text + block ranges
    A->>A: split into sentences, map Citations,<br/>drop uncited sentences
    A-->>C: Answer (sentences + Citations)
    C-->>U: Answer with numbered Citations
```

Each retrieved Sección is sent as its own document whose content blocks are its PDF pages, so a citation's block range maps straight back to a Sección and its PDF pages.

## Grounded or silent

Every sentence shown carries a Citation. This is how the Answerer decides what to show:

```mermaid
flowchart TD
    S["Retrieval"] --> E{"any Secciones?"}
    E -- no --> R1["Refusal<br/>(cites nothing)"]
    E -- yes --> M["Citations API answer"]
    M --> SP["split text into sentences<br/>(never inside a cited span)"]
    SP --> Q{"sentence has a Citation?"}
    Q -- no --> D["dropped<br/>(logged, never shown)"]
    Q -- yes --> K["kept"]
    K --> N{"any kept sentences?"}
    N -- no --> R2["Refusal"]
    N -- yes --> G{"model wrote a<br/>'SIN RESPALDO:' line?"}
    G -- no --> OK["Answer"]
    G -- yes --> INC["Answer marked incomplete"]
    R2 --> LP{"gap names a Parte that a retrieved<br/>Sección has a Remisión to?"}
    INC --> LP
    LP -- yes --> LPY["also show the likely Parte,<br/>cited from the Remisión's text"]
    LP -- no --> END["show as is"]
```

## Domain model

```mermaid
classDiagram
    direction LR
    class ParsedParte {
        code
        edicion
        enmienda
        content_hash
    }
    class Seccion {
        id
        title
    }
    class SeccionPage {
        pdf_page
        printed_page
        text
        rects
    }
    class ParteIndex {
        parte
        content_hash
        doc_id
        tree
    }
    class Answer {
        refused
        incomplete
        dropped_uncited
        model
    }
    class Sentence {
        text
    }
    class Citation {
        parte
        seccion
        pdf_page_start / end
        printed_page_start / end
        edicion
        enmienda
        source_url
        cited_text
    }
    class LikelyParte {
        parte
    }
    ParsedParte "1" *-- "many" Seccion
    Seccion "1" *-- "many" SeccionPage
    ParteIndex ..> ParsedParte : indexes (same content hash)
    Answer "1" *-- "many" Sentence
    Sentence "1" *-- "1..*" Citation
    Answer "1" *-- "0..*" LikelyParte
    LikelyParte --> Citation : cited Remisión
    Citation ..> Seccion : points to
```

## Index lifecycle (planned)

Re-ingesting a Parte replaces its index instead of adding to it. A changed PDF is indexed beside the live one and only promoted when automated checks pass.

```mermaid
stateDiagram-v2
    [*] --> Candidate: new content hash fetched
    Candidate --> Checking: parsed + indexed
    Checking --> Live: Edición/Enmienda parsed from footer,<br/>sane page count, Parte's eval cases green
    Checking --> Rejected: any check fails
    Rejected --> [*]: old index keeps serving,<br/>human alerted
    Live --> Retired: newer candidate promoted
    Retired --> [*]
```

The API keeps live trees in memory and reloads them on promotion. Every exchange is logged (question, routed Partes, visited nodes, Secciones, Answer with Citations, latency, tokens, cost, model IDs, index version) under a random conversation ID, so it can be replayed.
