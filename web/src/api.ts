// Client for the raac API. Types mirror components.schemas in the API's /openapi.json (AskEvent and what it references).

export interface Citation {
  parte: string;
  seccion: string;
  seccion_title: string;
  pdf_page_start: number;
  pdf_page_end: number;
  printed_page_start: string | null;
  printed_page_end: string | null;
  edicion: string | null;
  enmienda: string | null;
  fecha: string | null;
  source_url: string;
  cited_text: string;
  definicion: string | null;
}

export interface Sentence {
  text: string;
  citations: Citation[];
}

export interface LikelyParte {
  parte: string;
  citation: Citation;
}

export interface AnswerOut {
  sentences: Sentence[];
  refused: boolean;
  incomplete: boolean;
  likely_partes: LikelyParte[];
}

export type AskEvent =
  | { type: "progress"; message: string }
  | { type: "sentence"; sentence: Sentence }
  | { type: "answer"; answer: AnswerOut }
  | { type: "error"; message: string };

/** POST /api/ask and hand each Server-Sent Event to `onEvent` as it arrives. */
export async function ask(question: string, onEvent: (event: AskEvent) => void, signal?: AbortSignal): Promise<void> {
  const response = await fetch("/api/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ question }),
    signal,
  });
  if (!response.ok || !response.body) {
    throw new Error(`HTTP ${response.status}`);
  }
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let end: number;
    while ((end = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      const data = frame
        .split("\n")
        .filter((line) => line.startsWith("data: "))
        .map((line) => line.slice(6))
        .join("\n");
      if (data) onEvent(JSON.parse(data) as AskEvent);
    }
  }
}
