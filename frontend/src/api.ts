// Typed client for the Ontology QA backend (ontology_qa_cli.webapp).
// Every call maps 1:1 to a FastAPI route; in dev, Vite proxies /api to :8000.

export interface OntologyInfo {
  name: string;
  has_program: boolean;
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

export interface ProgramInfo {
  ontology: string;
  path: string;
  source: string;
}

async function post<T>(url: string, body: unknown): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
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
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
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
  const es = new EventSource(`/api/stream/${jobId}`);
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
