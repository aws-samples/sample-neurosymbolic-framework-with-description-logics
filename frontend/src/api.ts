// Typed client for the Ontology QA backend (ontology_qa_cli.webapp).
// Every call maps 1:1 to a FastAPI route; in dev, Vite proxies /api to :8000.
import { authHeaders, withAuthParam, handleUnauthorized } from "./auth";

export interface OntologyInfo {
  name: string;
  has_program: boolean;
  /** Canned question texts for the "I'm feeling lazy!" quick-pick dropdown. */
  questions?: string[];
}

export type SelectResult =
  | { status: "ready"; ontology: string }
  | { status: "building"; job_id: string };

export interface BuildStatus {
  status: "running" | "finished" | "failed";
  ontology: string;
  log: string;
  ready?: boolean;
}

export interface ChatResult {
  answer: string | null;
  resolved_question: string;
  was_rewritten: boolean;
}

export interface TestSetItem {
  question: string;
  expected_answer: string;
}

export interface ProgramInfo {
  ontology: string;
  path: string;
  source: string;
  /**
   * The question/expected-answer pairs that directed this program's evolution
   * (the builder's fixed evaluation set). Empty when none could be recovered.
   */
  test_set?: TestSetItem[];
}

async function post<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  if (res.status === 401) {
    handleUnauthorized();
    throw new Error("Session expired. Redirecting to sign in…");
  }
  if (!res.ok) {
    const detail = await res
      .json()
      .then((e) => (e as { detail?: string }).detail)
      .catch(() => undefined);
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

async function get<T>(url: string): Promise<T> {
  const res = await fetch(url, { headers: { ...authHeaders() } });
  if (res.status === 401) {
    handleUnauthorized();
    throw new Error("Session expired. Redirecting to sign in…");
  }
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  // CloudFront's SPA error-page fallback can rewrite an API 4xx into a 200 that
  // serves index.html (text/html) from the S3 origin. Parsing that as JSON throws
  // an opaque "string did not match the expected pattern". Detect a non-JSON body
  // and surface a clear message instead.
  const contentType = res.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) {
    throw new Error("not available yet");
  }
  return res.json() as Promise<T>;
}

export const api = {
  ontologies: () =>
    get<{ ontologies: OntologyInfo[] }>("/api/ontologies").then((r) => r.ontologies),
  select: (ontology: string) => post<SelectResult>("/api/select", { ontology }),
  rebuild: (ontology: string) => post<{ status: "building"; job_id: string }>(
    "/api/rebuild",
    { ontology },
  ),
  status: (jobId: string) => get<BuildStatus>(`/api/status/${jobId}`),
  conversation: (ontology: string) =>
    post<{ conversation_id: string; ontology: string }>("/api/conversation", { ontology }),
  chat: (conversation_id: string, message: string) =>
    post<ChatResult>("/api/chat", { conversation_id, message }),
  program: (ontology: string) =>
    get<ProgramInfo>(`/api/program/${encodeURIComponent(ontology)}`),
};

export interface BuildDone {
  status: "finished" | "failed";
  ready: boolean;
  ontology: string;
}

/**
 * Stream a build's live output via Server-Sent Events.
 * Calls `onLine` for each output line and `onDone` when the build ends.
 * Returns a cleanup function that closes the stream.
 */
export function streamBuild(
  jobId: string,
  onLine: (line: string) => void,
  onDone: (done: BuildDone) => void,
  onError: (message: string) => void,
): () => void {
  const es = new EventSource(withAuthParam(`/api/stream/${jobId}`));
  es.onmessage = (e) => onLine(e.data);
  es.addEventListener("done", (e) => {
    es.close();
    try {
      onDone(JSON.parse((e as MessageEvent).data) as BuildDone);
    } catch {
      onError("Malformed build-complete event.");
    }
  });
  es.onerror = () => {
    // EventSource auto-reconnects; only surface an error if it never opened.
    if (es.readyState === EventSource.CLOSED) {
      onError("Lost connection to the build stream.");
    }
  };
  return () => es.close();
}

export type SelectDone =
  | { status: "ready"; ready: boolean; ontology: string }
  | { status: "building"; job_id: string; ontology: string }
  | { status: "failed"; ready: boolean; ontology: string; error?: string };

/**
 * Select an ontology while streaming each load phase via Server-Sent Events.
 * Calls `onPhase` with a human-readable label per phase ("Fetching program…",
 * "Finding best program…", "Loading ontology…"), then `onDone` with the final
 * status (ready | building | failed). Returns a cleanup function.
 */
export function streamSelect(
  ontology: string,
  onPhase: (label: string) => void,
  onDone: (done: SelectDone) => void,
  onError: (message: string) => void,
): () => void {
  const qs = new URLSearchParams({ ontology });
  const es = new EventSource(withAuthParam(`/api/select/stream?${qs.toString()}`));
  es.onmessage = (e) => onPhase(e.data);
  es.addEventListener("done", (e) => {
    es.close();
    try {
      onDone(JSON.parse((e as MessageEvent).data) as SelectDone);
    } catch {
      onError("Malformed select-complete event.");
    }
  });
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) {
      onError("Lost connection while selecting the ontology.");
    }
  };
  return () => es.close();
}

export interface ChatDone {
  answer: string | null;
  resolved_question: string;
  was_rewritten: boolean;
}

/**
 * Answer a question while streaming the running program's output via SSE.
 * Calls `onLine` for each stdout/stderr line, `onDone` with the final answer,
 * and `onError` on failure. Returns a cleanup function that closes the stream.
 */
export function streamChat(
  conversationId: string,
  message: string,
  onLine: (line: string) => void,
  onDone: (done: ChatDone) => void,
  onError: (message: string) => void,
): () => void {
  const qs = new URLSearchParams({ conversation_id: conversationId, message });
  const es = new EventSource(withAuthParam(`/api/chat/stream?${qs.toString()}`));
  es.onmessage = (e) => onLine(e.data);
  es.addEventListener("done", (e) => {
    es.close();
    try {
      onDone(JSON.parse((e as MessageEvent).data) as ChatDone);
    } catch {
      onError("Malformed chat-complete event.");
    }
  });
  es.addEventListener("error", (e) => {
    // A server-sent `error` event carries a detail payload; a transport error
    // has no data and only matters once the connection is closed for good.
    const data = (e as MessageEvent).data;
    if (data) {
      es.close();
      try {
        onError((JSON.parse(data) as { detail?: string }).detail || "Query failed.");
      } catch {
        onError("Query failed.");
      }
    } else if (es.readyState === EventSource.CLOSED) {
      onError("Lost connection to the answer stream.");
    }
  });
  return () => es.close();
}
