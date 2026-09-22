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
  status: (jobId: string) => get<BuildStatus>(`/api/status/${jobId}`),
  conversation: (ontology: string) =>
    post<{ conversation_id: string; ontology: string }>("/api/conversation", { ontology }),
  chat: (conversation_id: string, message: string) =>
    post<ChatResult>("/api/chat", { conversation_id, message }),
};
