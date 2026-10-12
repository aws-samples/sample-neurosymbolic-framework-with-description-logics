// Typed client for the Ontology QA backend (ontology_qa_cli.webapp).
// Every call maps 1:1 to a FastAPI route; in dev, Vite proxies /api to :8000.
import { authHeaders, withAuthParam, handleUnauthorized, freshIdToken } from "./auth";

export interface OntologyInfo {
  name: string;
  /** Human-readable full name (e.g. "Gene Ontology"); may be absent. */
  display_name?: string | null;
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
 * Open an authenticated EventSource after ensuring a FRESH token.
 *
 * `withAuthParam` reads the token synchronously and sends none when it has
 * expired, which 401s the authenticated SSE endpoints and closes the stream
 * with no completion frame. This helper awaits `freshIdToken()` first (which
 * silently refreshes or, failing that, re-logs-in) so the token is valid when
 * the stream opens. If the stream still errors out at the transport level
 * (CLOSED with no server payload) — the signature of a rejected/expired token
 * mid-stream — it calls `handleUnauthorized()` to clear the stale token and
 * re-authenticate, instead of leaving the UI wedged on "Lost connection".
 *
 * `buildUrl` receives the token-decorated base and returns the final URL.
 * `wire` attaches message/done/custom-error listeners to the opened source.
 * Returns a synchronous cleanup function (contract-compatible with the old
 * stream helpers); closing before the async open completes is handled.
 */
function openAuthedStream(
  path: string,
  wire: (es: EventSource) => void,
  onError: (message: string) => void,
  lostMessage: string,
): () => void {
  let es: EventSource | null = null;
  let closed = false;

  void freshIdToken()
    .then(() => {
      if (closed) return; // caller cleaned up before the token resolved
      es = new EventSource(withAuthParam(path));
      wire(es);
      es.addEventListener("error", (e) => {
        // A server-sent `error` event carries a detail payload (handled by
        // `wire`); a bare transport error only matters once CLOSED for good.
        const data = (e as MessageEvent).data;
        if (!data && es && es.readyState === EventSource.CLOSED) {
          es.close();
          // A closed-without-payload stream on an authenticated endpoint is the
          // signature of an expired/rejected token: clear it and re-login so
          // the next attempt carries a valid token, then surface the message.
          handleUnauthorized();
          onError(lostMessage);
        }
      });
    })
    .catch(() => {
      if (!closed) onError(lostMessage);
    });

  return () => {
    closed = true;
    if (es) es.close();
  };
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
  return openAuthedStream(
    `/api/stream/${jobId}`,
    (es) => {
      es.onmessage = (e) => onLine(e.data);
      es.addEventListener("done", (e) => {
        es.close();
        try {
          onDone(JSON.parse((e as MessageEvent).data) as BuildDone);
        } catch {
          onError("Malformed build-complete event.");
        }
      });
    },
    onError,
    "Lost connection to the build stream.",
  );
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
  return openAuthedStream(
    `/api/select/stream?${qs.toString()}`,
    (es) => {
      es.onmessage = (e) => onPhase(e.data);
      es.addEventListener("done", (e) => {
        es.close();
        try {
          onDone(JSON.parse((e as MessageEvent).data) as SelectDone);
        } catch {
          onError("Malformed select-complete event.");
        }
      });
    },
    onError,
    "Lost connection while selecting the ontology.",
  );
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
  return openAuthedStream(
    `/api/chat/stream?${qs.toString()}`,
    (es) => {
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
        // A server-sent `error` event carries a detail payload (an application
        // failure, not an auth/transport problem). Handle it here; the bare
        // transport-error case is handled by openAuthedStream.
        const data = (e as MessageEvent).data;
        if (data) {
          es.close();
          try {
            onError((JSON.parse(data) as { detail?: string }).detail || "Query failed.");
          } catch {
            onError("Query failed.");
          }
        }
      });
    },
    onError,
    "Lost connection to the answer stream.",
  );
}
