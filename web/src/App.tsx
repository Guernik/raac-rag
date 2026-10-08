import { type FormEvent, type ReactNode, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { type AnswerOut, type AskEvent, type Citation, type Sentence, ask } from "./api";
import { Header } from "./Header";
import { strings } from "./strings";

interface Exchange {
  id: number;
  question: string;
  startedAt: number;
  progress: string | null;
  sentences: Sentence[]; // streamed so far; replaced by the final Answer's
  answer: AnswerOut | null;
  error: string | null;
}

/** What the Citation sheet or panel shows, and which chips and sentences it lights up. */
interface Opened {
  title: string;
  citations: Citation[];
  chip: string | null; // the chip that opened it
  lit: Set<string>; // chip keys whose sentences are highlighted
}

export function App() {
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [opened, setOpened] = useState<Opened | null>(null);
  const opener = useRef<HTMLElement | null>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const desktop = useDesktop();

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [exchanges]);

  async function run(q: string) {
    if (!q || busy) return;
    const id = Date.now();
    const update = (fn: (x: Exchange) => Exchange) => setExchanges((xs) => xs.map((x) => (x.id === id ? fn(x) : x)));
    setExchanges((xs) => [
      ...xs,
      { id, question: q, startedAt: Date.now(), progress: null, sentences: [], answer: null, error: null },
    ]);
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

  function retry(x: Exchange) {
    setExchanges((xs) => xs.filter((y) => y.id !== x.id));
    run(x.question);
  }

  function open(next: Opened, from: HTMLElement) {
    if (opened?.chip && opened.chip === next.chip) return close();
    opener.current = from;
    setOpened(next);
  }

  function close() {
    setOpened(null);
    opener.current?.focus({ preventScroll: true });
    opener.current = null;
  }

  useEffect(() => {
    if (!opened) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  });

  const empty = exchanges.length === 0;
  const latest = [...exchanges].reverse().find((x) => x.sentences.length > 0 || x.answer);
  const composer = (
    <Composer
      value={question}
      onChange={setQuestion}
      onSubmit={() => run(question.trim())}
      busy={busy}
      hero={empty && desktop}
    />
  );

  return (
    <div className={`app${empty ? " is-empty" : ""}`}>
      <div className="main">
        <Header />
        <main className="thread">
          <div className="thread-in">
            {empty ? (
              <EmptyState onAsk={run}>{desktop && composer}</EmptyState>
            ) : (
              exchanges.map((x) => (
                <ExchangeView key={x.id} exchange={x} opened={opened} onOpen={open} onRetry={() => retry(x)} />
              ))
            )}
            <div ref={endRef} />
          </div>
        </main>
        {!(empty && desktop) && composer}
      </div>
      {!(empty && desktop) && (
        <CitationPanel
          opened={opened}
          desktop={desktop}
          onClose={close}
          index={latest ? <SeccionIndex exchange={latest} opened={opened} onOpen={open} /> : null}
        />
      )}
    </div>
  );
}

function EmptyState({ onAsk, children }: { onAsk: (q: string) => void; children: ReactNode }) {
  return (
    <section className="empty">
      <h1>{strings.emptyTitle}</h1>
      <p>{strings.emptyBody}</p>
      {children}
      <h2 className="label">{strings.examplesLabel}</h2>
      <div className="examples">
        {strings.examples.map((q) => (
          <button key={q} type="button" className="example" onClick={() => onAsk(q)}>
            {q}
          </button>
        ))}
      </div>
    </section>
  );
}

function Composer(props: {
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
  busy: boolean;
  hero: boolean;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);

  // Grow with the text up to the CSS max-height.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight + 2}px`;
  }, [props.value]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    props.onSubmit();
  };

  return (
    <form className={`ask${props.hero ? " ask-hero" : ""}`} onSubmit={submit}>
      <div className="ask-row">
        <textarea
          ref={ref}
          value={props.value}
          onChange={(e) => props.onChange(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) submit(e);
          }}
          placeholder={strings.placeholder}
          aria-label={strings.question}
          rows={props.hero ? 3 : 1}
          maxLength={2000}
          enterKeyHint="send"
          autoFocus={props.hero}
        />
        <button type="submit" className="send" disabled={props.busy || !props.value.trim()} aria-label={strings.send}>
          <span className="send-label">{strings.send}</span>
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M5 12h14M13 6l6 6-6 6" />
          </svg>
        </button>
      </div>
      <p className="hint">{strings.hint}</p>
    </form>
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

function chipKey(x: Exchange, c: Citation) {
  return `${x.id}|${c.parte}|${c.seccion}|${c.pdf_page_start}|${c.pdf_page_end}|${c.cited_text}`;
}

function chipOpened(x: Exchange, c: Citation): Opened {
  const key = chipKey(x, c);
  return { title: strings.citationKicker(c), citations: [c], chip: key, lit: new Set([key]) };
}

function Chip(props: { exchange: Exchange; citation: Citation; opened: Opened | null; onOpen: OnOpen }) {
  const c = props.citation;
  const key = chipKey(props.exchange, c);
  return (
    <button
      type="button"
      className="chip"
      aria-expanded={props.opened?.chip === key}
      aria-label={strings.citationLabel(c)}
      onClick={(e) => props.onOpen(chipOpened(props.exchange, c), e.currentTarget)}
    >
      {c.seccion}
    </button>
  );
}

type OnOpen = (opened: Opened, from: HTMLElement) => void;

function ExchangeView(props: { exchange: Exchange; opened: Opened | null; onOpen: OnOpen; onRetry: () => void }) {
  const { exchange: x, opened, onOpen } = props;
  const { answer } = x;
  const streaming = !answer && !x.error;
  const last = x.sentences.at(-1);
  const chip = (c: Citation) => <Chip key={chipKey(x, c)} exchange={x} citation={c} opened={opened} onOpen={onOpen} />;

  const run = (sentences: Sentence[]) =>
    sentences.map((s, j) => {
      const lit = !!opened && s.citations.some((c) => opened.lit.has(chipKey(x, c)));
      const verdict = s === x.sentences[0] && s.citations.length === 0;
      return (
        <span key={j}>
          <span className={`sentence${verdict ? " verdict" : ""}${lit ? " lit" : ""}`}>
            {s.text}
            {s.citations.map(chip)}
          </span>
          {streaming && s === last ? <span className="caret" aria-hidden="true" /> : " "}
        </span>
      );
    });

  return (
    <article className="exchange">
      <h2 className="question">{x.question}</h2>
      {streaming && <Progress message={x.progress} startedAt={x.startedAt} />}
      {answer?.refused && (
        <Note tone="warn" label={strings.refusalLabel}>
          <p>{strings.refusal}</p>
          {answer.likely_partes.map((lp) => (
            <p key={lp.parte}>
              {strings.likelyParte(lp.parte, lp.citation.seccion)}
              {chip(lp.citation)}
            </p>
          ))}
        </Note>
      )}
      {answer?.incomplete && (
        <Note tone="warn" label={strings.incompleteLabel}>
          <p>{strings.incomplete}</p>
        </Note>
      )}
      {x.sentences.length > 0 && (
        <div className="answer">
          {layout(x.sentences).map((block, i) =>
            block.type === "paragraph" ? (
              <p key={i}>{run(block.sentences)}</p>
            ) : (
              <ul key={i}>
                {block.items.map((item, j) => (
                  <li key={j}>{run(item)}</li>
                ))}
              </ul>
            ),
          )}
        </div>
      )}
      {answer &&
        !answer.refused &&
        answer.likely_partes.map((lp) => (
          <Note key={lp.parte} tone="warn">
            <p>
              {strings.likelyParte(lp.parte, lp.citation.seccion)}
              {chip(lp.citation)}
            </p>
          </Note>
        ))}
      {x.error && (
        <Note tone="error" label={strings.errorLabel} alert>
          <p>{x.error}</p>
          <button type="button" className="secondary" onClick={props.onRetry}>
            {strings.retry}
          </button>
        </Note>
      )}
    </article>
  );
}

function Note(props: { tone: "warn" | "error"; label?: string; alert?: boolean; children: ReactNode }) {
  return (
    <div className={`note note-${props.tone}`} role={props.alert ? "alert" : undefined}>
      {props.label && <p className="note-label">{props.label}</p>}
      {props.children}
    </div>
  );
}

function Progress({ message, startedAt }: { message: string | null; startedAt: number }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  return (
    <p className="progress">
      <span className="progress-message" aria-live="polite">
        {message}
      </span>
      <span className="progress-clock">{strings.elapsed(Math.max(0, Math.floor((now - startedAt) / 1000)))}</span>
    </p>
  );
}

/** Desktop panel at rest: the Secciones the latest Answer cites, each opening all its cited spans. */
function SeccionIndex({ exchange: x, opened, onOpen }: { exchange: Exchange; opened: Opened | null; onOpen: OnOpen }) {
  const all = [...x.sentences.flatMap((s) => s.citations), ...(x.answer?.likely_partes.map((lp) => lp.citation) ?? [])];
  const secciones = new Map<string, Citation[]>();
  for (const c of all) {
    const k = `${c.parte}|${c.seccion}`;
    const cs = secciones.get(k) ?? [];
    if (!cs.some((d) => d.cited_text === c.cited_text)) cs.push(c);
    secciones.set(k, cs);
  }
  if (secciones.size === 0) return null;
  return (
    <div className="index">
      <h2 className="label">{strings.citedLabel}</h2>
      {[...secciones.values()].map((cs) => (
        <button
          key={`${cs[0].parte}|${cs[0].seccion}`}
          type="button"
          className="index-row"
          aria-pressed={opened?.title === strings.citationKicker(cs[0]) && !opened.chip}
          onClick={(e) =>
            onOpen(
              {
                title: strings.citationKicker(cs[0]),
                citations: cs,
                chip: null,
                lit: new Set(cs.map((c) => chipKey(x, c))),
              },
              e.currentTarget,
            )
          }
        >
          <b>{cs[0].seccion}</b>
          <span>{cs[0].definicion ? strings.definicion(cs[0].definicion) : cs[0].seccion_title}</span>
        </button>
      ))}
      <p className="index-hint">{strings.citedEmpty}</p>
    </div>
  );
}

/** A bottom sheet on phone, a side panel on desktop. */
function CitationPanel(props: { opened: Opened | null; desktop: boolean; onClose: () => void; index: ReactNode }) {
  const { opened, desktop } = props;
  const closeRef = useRef<HTMLButtonElement>(null);
  const shown = useLastDefined(opened); // keeps content while the sheet slides out

  useEffect(() => {
    if (opened) closeRef.current?.focus({ preventScroll: true });
  }, [opened]);

  const c0 = shown?.citations[0];
  const sheet = !desktop;
  return (
    <>
      {sheet && <div className={`scrim${opened ? " shown" : ""}`} onClick={props.onClose} aria-hidden="true" />}
      <aside
        className={`panel${opened ? " open" : ""}`}
        role={sheet ? "dialog" : "complementary"}
        aria-modal={sheet && opened ? true : undefined}
        aria-labelledby="panel-title"
        inert={sheet && !opened}
      >
        {sheet && <div className="handle" aria-hidden="true" />}
        {!opened && desktop && props.index}
        {shown && c0 && (opened || sheet) && (
          <>
            <div className="panel-head">
              <div>
                <p className="panel-kicker">{shown.title}</p>
                <h2 id="panel-title" className="panel-title">
                  {c0.definicion ? strings.definicion(c0.definicion) : c0.seccion_title}
                </h2>
              </div>
              <button ref={closeRef} type="button" className="icon-button" onClick={props.onClose} aria-label={strings.close}>
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M6 6l12 12M18 6L6 18" />
                </svg>
              </button>
            </div>
            <div className="panel-body">
              {shown.citations.map((c) => (
                <CitedSpan key={`${c.pdf_page_start}|${c.cited_text}`} citation={c} />
              ))}
              <div className="panel-actions">
                <a className="primary" href={c0.source_url} target="_blank" rel="noreferrer">
                  <svg viewBox="0 0 24 24" aria-hidden="true">
                    <path d="M14 3h7v7M21 3l-9 9M19 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h5" />
                  </svg>
                  {strings.openSource}
                </a>
                <CopyButton text={() => citationText(shown)} />
              </div>
            </div>
          </>
        )}
      </aside>
    </>
  );
}

function CopyButton({ text }: { text: () => string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(t);
  }, [copied]);
  const copy = () =>
    navigator.clipboard.writeText(text()).then(
      () => setCopied(true),
      () => {}, // clipboard refused: nothing to undo
    );
  return (
    <button type="button" className="secondary" onClick={copy} aria-live="polite">
      <svg viewBox="0 0 24 24" aria-hidden="true">
        {copied ? <path d="M5 12l5 5L20 7" /> : <path d="M9 9h10v12H9zM5 15H4V3h11v1" />}
      </svg>
      {copied ? strings.copied : strings.copy}
    </button>
  );
}

/** The opened Citation as plain text, for pasting into a briefing or a message. */
function citationText(opened: Opened) {
  const c0 = opened.citations[0];
  const spans = opened.citations.map((c) => {
    const pages = [`${strings.pdfPage} ${strings.range(c.pdf_page_start, c.pdf_page_end)}`];
    if (c.printed_page_start)
      pages.push(`${strings.printedPage} ${strings.range(c.printed_page_start, c.printed_page_end ?? c.printed_page_start)}`);
    const version = strings.version(c.edicion, c.enmienda, c.fecha);
    return `«${reflow(c.cited_text)}»\n${[...pages, ...(version ? [version] : [])].join(" · ")}`;
  });
  const title = c0.definicion ? strings.definicion(c0.definicion) : c0.seccion_title;
  return [`${strings.citationLabel(c0)}: ${title}`, ...spans, c0.source_url].join("\n\n");
}

function CitedSpan({ citation: c }: { citation: Citation }) {
  const version = strings.version(c.edicion, c.enmienda, c.fecha);
  return (
    <figure className="span">
      <blockquote>{reflow(c.cited_text)}</blockquote>
      <dl className="meta">
        <dt>{strings.pdfPage}</dt>
        <dd>{strings.range(c.pdf_page_start, c.pdf_page_end)}</dd>
        {c.printed_page_start && (
          <>
            <dt>{strings.printedPage}</dt>
            <dd>{strings.range(c.printed_page_start, c.printed_page_end ?? c.printed_page_start)}</dd>
          </>
        )}
        {version && (
          <>
            <dt>{strings.versionLabel}</dt>
            <dd>{version}</dd>
          </>
        )}
      </dl>
    </figure>
  );
}

/** Undo the PDF's line wrapping for reading: rejoin hyphenated words, keep list markers like "(a)" on their own line. */
function reflow(text: string) {
  return text
    .replace(/-\n/g, "")
    .replace(/\n(\([a-zA-Z0-9]+\))\n/g, "\u0000$1 ")
    .replace(/\n/g, " ")
    .replaceAll("\u0000", "\n");
}

function useLastDefined<T>(value: T | null): T | null {
  const last = useRef(value);
  if (value) last.current = value;
  return last.current;
}

// Keep in step with the desktop breakpoint in styles.css.
const desktopQuery = window.matchMedia("(min-width: 960px)");

function useDesktop(): boolean {
  return useSyncExternalStore(
    (onChange) => {
      desktopQuery.addEventListener("change", onChange);
      return () => desktopQuery.removeEventListener("change", onChange);
    },
    () => desktopQuery.matches,
  );
}
