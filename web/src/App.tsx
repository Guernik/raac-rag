import { type FormEvent, useEffect, useRef, useState } from "react";
import { type AnswerOut, type AskEvent, type Citation, type Sentence, ask } from "./api";
import { strings } from "./strings";

interface Exchange {
  id: number;
  question: string;
  progress: string | null;
  sentences: Sentence[]; // streamed so far; replaced by the final Answer's
  answer: AnswerOut | null;
  error: string | null;
}

export function App() {
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [exchanges]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q || busy) return;
    const id = Date.now();
    const update = (fn: (x: Exchange) => Exchange) => setExchanges((xs) => xs.map((x) => (x.id === id ? fn(x) : x)));
    setExchanges((xs) => [...xs, { id, question: q, progress: null, sentences: [], answer: null, error: null }]);
    setQuestion("");
    setBusy(true);
    try {
      await ask(q, (event: AskEvent) => {
        switch (event.type) {
          case "progress":
            return update((x) => ({ ...x, progress: event.message }));
          case "sentence":
            return update((x) => ({ ...x, sentences: [...x.sentences, event.sentence] }));
          case "answer":
            return update((x) => ({ ...x, answer: event.answer, sentences: event.answer.sentences, progress: null }));
          case "error":
            return update((x) => ({ ...x, error: event.message, progress: null }));
        }
      });
    } catch {
      update((x) => ({ ...x, error: strings.networkError, progress: null }));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="app">
      <header className="top">
        <h1>{strings.appTitle}</h1>
        <p className="notice" role="note">
          {strings.notice}
        </p>
      </header>

      <main className="thread">
        {exchanges.length === 0 && (
          <div className="empty">
            <p className="empty-title">{strings.emptyTitle}</p>
            <p>{strings.emptyBody}</p>
            <p>{strings.emptyExample}</p>
          </div>
        )}
        {exchanges.map((x) => (
          <ExchangeView key={x.id} exchange={x} />
        ))}
        <div ref={endRef} />
      </main>

      <form className="ask" onSubmit={submit}>
        <textarea
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) submit(e);
          }}
          placeholder={strings.placeholder}
          rows={2}
          maxLength={2000}
          enterKeyHint="send"
        />
        <button type="submit" disabled={busy || !question.trim()}>
          {busy ? strings.sending : strings.send}
        </button>
      </form>
    </div>
  );
}

type Block = { type: "paragraph"; sentences: Sentence[] } | { type: "list"; items: Sentence[][] };

/** Group sentences into paragraphs and lists by where each one starts. */
function layout(sentences: Sentence[]): Block[] {
  const blocks: Block[] = [];
  for (const s of sentences) {
    const last = blocks.at(-1);
    if (s.starts === "item") {
      if (last?.type === "list") last.items.push([s]);
      else blocks.push({ type: "list", items: [[s]] });
    } else if (s.starts === "paragraph" || !last) {
      blocks.push({ type: "paragraph", sentences: [s] });
    } else if (last.type === "list") {
      last.items[last.items.length - 1].push(s);
    } else {
      last.sentences.push(s);
    }
  }
  return blocks;
}

function citationKey(c: Citation) {
  return `${c.parte}|${c.seccion}|${c.pdf_page_start}|${c.pdf_page_end}|${c.cited_text}`;
}

function ExchangeView({ exchange }: { exchange: Exchange }) {
  const [open, setOpen] = useState<string | null>(null);
  const { answer } = exchange;

  const opened = exchange.sentences.flatMap((s) => s.citations).find((c) => citationKey(c) === open);

  return (
    <article className="exchange">
      <p className="question">{exchange.question}</p>
      {exchange.progress && (
        <p className="progress" aria-live="polite">
          {exchange.progress}
        </p>
      )}
      {answer?.incomplete && <p className="caveat">{strings.incomplete}</p>}
      {answer?.refused && <p className="caveat">{strings.refusal}</p>}
      {exchange.sentences.length > 0 && (
        <div className="answer">
          {layout(exchange.sentences).map((block, i) => {
            const run = (sentences: Sentence[]) =>
              sentences.map((s, j) => (
                <span key={j}>
                  {s.text}
                  {s.citations.map((c) => {
                    const key = citationKey(c);
                    return (
                      <button
                        key={key}
                        type="button"
                        className={`chip${open === key ? " chip-open" : ""}`}
                        aria-expanded={open === key}
                        onClick={() => setOpen(open === key ? null : key)}
                      >
                        {c.seccion}
                      </button>
                    );
                  })}{" "}
                </span>
              ));
            return block.type === "paragraph" ? (
              <p key={i}>{run(block.sentences)}</p>
            ) : (
              <ul key={i}>
                {block.items.map((item, j) => (
                  <li key={j}>{run(item)}</li>
                ))}
              </ul>
            );
          })}
        </div>
      )}
      {opened && <CitationView citation={opened} onClose={() => setOpen(null)} />}
      {answer?.likely_partes.map((lp) => (
        <p key={lp.parte} className="caveat">
          {strings.likelyParte(lp.parte, lp.citation.seccion)}
        </p>
      ))}
      {exchange.error && (
        <p className="error" role="alert">
          {exchange.error}
        </p>
      )}
    </article>
  );
}

function CitationView({ citation: c, onClose }: { citation: Citation; onClose: () => void }) {
  const version = strings.version(c.edicion, c.enmienda, c.fecha);
  return (
    <aside className="citation">
      <div className="citation-head">
        <div>
          <p className="citation-title">{strings.citationTitle(c)}</p>
          <p className="citation-sub">{c.definicion ? strings.definicion(c.definicion) : c.seccion_title}</p>
        </div>
        <button type="button" className="close" onClick={onClose} aria-label={strings.close}>
          ×
        </button>
      </div>
      <blockquote>{c.cited_text}</blockquote>
      <p className="citation-meta">
        {strings.pages(c.pdf_page_start, c.pdf_page_end, c.printed_page_start, c.printed_page_end)}
        {version && ` · ${version}`}
      </p>
      <a href={c.source_url} target="_blank" rel="noreferrer">
        {strings.openSource}
      </a>
    </aside>
  );
}
