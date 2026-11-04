import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { authHeaders } from "./auth";
import { Markdown } from "./Markdown";

interface Props {
  ontology: string;
  /** Canned questions for the selected ontology (first one seeds the input). */
  questions?: string[];
}

// The MCP tool name the remote server exposes for an ontology, mirroring
// mcp_server.tool_name_for (answer_question_with_<NAME>).
const toolNameFor = (ontology: string) => `answer_question_with_${ontology}`;

// Build the JSON-RPC 2.0 tools/call body the MCP server expects. Pretty-printed
// so it reads well inside the curl.
function callBody(ontology: string, question: string): string {
  return JSON.stringify(
    {
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: {
        name: toolNameFor(ontology),
        arguments: { question },
      },
    },
    null,
    2,
  );
}

// The sample curl shown to the user. The MCP endpoint is Cognito-gated, so the
// command carries an Authorization: Bearer header — shown as a $TOKEN
// placeholder rather than the user's live token (a real token is a secret, it
// expires, and printing it in the page is a leak). A one-line note tells the
// user how to obtain one.
function sampleCurl(origin: string, ontology: string, question: string): string {
  const url = `${origin}/mcp`;
  // Escape single quotes for the single-quoted shell heredoc-free form.
  const body = callBody(ontology, question).replace(/'/g, `'\\''`);
  return [
    `curl -sS -X POST '${url}' \\`,
    `  -H 'Authorization: Bearer '"$TOKEN"'' \\`,
    `  -H 'Content-Type: application/json' \\`,
    `  -H 'Accept: application/json' \\`,
    `  -d '${body}'`,
  ].join("\n");
}

// Pull the human-readable answer out of the MCP tools/call JSON-RPC response.
// The server returns result.content[0].text as a JSON string (the answer
// payload). We pretty-print the whole response so the user sees exactly what
// the curl would print to stdout — newlines and all.
function formatStdout(json: unknown): string {
  return JSON.stringify(json, null, 2);
}

// Pull the markdown answer prose out of the MCP tools/call response so it can
// be rendered (separate from the Raw output JSON). The server nests the answer
// at result.content[0].text as a JSON string like {"answered":true,
// "answer":"<markdown>", ...}; we return that answer string, or null if the
// shape doesn't match (e.g. an error payload or an unexpected schema).
function extractAnswer(json: unknown): string | null {
  try {
    const content = (json as { result?: { content?: unknown } })?.result?.content;
    if (!Array.isArray(content)) return null;
    const first = content.find(
      (c): c is { type?: string; text?: string } =>
        !!c && typeof (c as { text?: unknown }).text === "string",
    );
    if (!first?.text) return null;
    // The text block is itself a JSON string carrying the answer payload.
    try {
      const payload = JSON.parse(first.text) as { answer?: unknown };
      if (typeof payload.answer === "string" && payload.answer.trim()) {
        return payload.answer;
      }
    } catch {
      // Not JSON — treat the raw text block as the answer markdown.
      return first.text;
    }
    return null;
  } catch {
    return null;
  }
}

// Per-ontology persistence of the last MCP run, so the question, Raw output and
// Rendered answer survive a page reload (and switching between the Program and
// MCP tabs, which unmounts this view). Best-effort, like useChat's last-ontology
// memory: if localStorage is unavailable or full, the view just starts empty.
interface SavedRun {
  question?: string;
  stdout?: string | null;
  answer?: string | null;
}

const savedKey = (ontology: string) => `ndl.mcp.last.${ontology}`;

function loadSaved(ontology: string): SavedRun {
  try {
    const raw = localStorage.getItem(savedKey(ontology));
    return raw ? (JSON.parse(raw) as SavedRun) : {};
  } catch {
    return {};
  }
}

function saveRun(ontology: string, patch: SavedRun | null): void {
  try {
    if (patch === null) {
      localStorage.removeItem(savedKey(ontology));
      return;
    }
    localStorage.setItem(
      savedKey(ontology),
      JSON.stringify({ ...loadSaved(ontology), ...patch }),
    );
  } catch {
    // Unavailable (private mode / disabled) or quota exceeded — best-effort.
  }
}

export function McpView({ ontology, questions }: Props) {
  const seed = (questions && questions[0]) || "";
  const [question, setQuestion] = useState(seed);
  const [stdout, setStdout] = useState<string | null>(null);
  const [answer, setAnswer] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const copyTimer = useRef<number | null>(null);

  // On mount and on ontology switch, restore that ontology's last question and
  // result if one was saved; otherwise seed the first canned question.
  useEffect(() => {
    const saved = ontology ? loadSaved(ontology) : {};
    setQuestion(saved.question ?? ((questions && questions[0]) || ""));
    setStdout(saved.stdout ?? null);
    setAnswer(saved.answer ?? null);
    setError(null);
  }, [ontology]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => () => {
    if (copyTimer.current) window.clearTimeout(copyTimer.current);
  }, []);

  const origin = typeof window !== "undefined" ? window.location.origin : "";
  const curl = useMemo(
    () => sampleCurl(origin, ontology, question || "<your question>"),
    [origin, ontology, question],
  );

  const copyCurl = useCallback(() => {
    void navigator.clipboard?.writeText(curl).then(() => {
      setCopied(true);
      if (copyTimer.current) window.clearTimeout(copyTimer.current);
      copyTimer.current = window.setTimeout(() => setCopied(false), 1500);
    });
  }, [curl]);

  const run = useCallback(() => {
    const q = question.trim();
    if (!ontology || !q || running) return;
    setRunning(true);
    setError(null);
    setStdout(null);
    setAnswer(null);
    // Drop the previous saved result so a failed run can't resurrect it on reload.
    saveRun(ontology, { question: q, stdout: null, answer: null });
    void fetch(`${origin}/mcp`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        ...authHeaders(),
      },
      body: callBody(ontology, q),
    })
      .then(async (res) => {
        const text = await res.text();
        if (!res.ok) {
          throw new Error(`${res.status} ${res.statusText}\n${text}`.trim());
        }
        // Try to pretty-print JSON; fall back to the raw text if it isn't JSON.
        try {
          const parsed = JSON.parse(text);
          const out = formatStdout(parsed);
          const ans = extractAnswer(parsed);
          setStdout(out);
          setAnswer(ans);
          saveRun(ontology, { question: q, stdout: out, answer: ans });
        } catch {
          setStdout(text);
          setAnswer(null);
          saveRun(ontology, { question: q, stdout: text, answer: null });
        }
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setRunning(false));
  }, [ontology, question, running, origin]);

  if (!ontology) {
    return (
      <div className="mcp">
        <div className="program-empty">Choose an ontology to see its MCP tool.</div>
      </div>
    );
  }

  const body: ReactNode = (
    <>
      <div className="mcp-section">
        <div className="mcp-section-head">
          <span className="mcp-section-title">
            Call the <code>{toolNameFor(ontology)}</code> tool
          </span>
        </div>
        <p className="mcp-note">
          This ontology is served by the remote MCP server at <code>POST /mcp</code>{" "}
          (Model Context Protocol, Streamable HTTP). The endpoint is
          authenticated, so the request carries a bearer token — replace{" "}
          <code>$TOKEN</code> with a valid Cognito access token (your logged-in
          session already holds one; <b>Run</b> below uses it automatically).
        </p>
        <label className="mcp-q-label">
          Question
          <input
            className="mcp-q-input"
            type="text"
            value={question}
            placeholder="Ask a question of this ontology…"
            onChange={(e) => {
              setQuestion(e.target.value);
              saveRun(ontology, { question: e.target.value });
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") run();
            }}
          />
        </label>
      </div>

      <div className="mcp-section">
        <div className="mcp-section-head">
          <span className="mcp-section-title">Sample curl</span>
        </div>
        <div className="mcp-code-wrap">
          <button
            className="mcp-copy-inline"
            onClick={copyCurl}
            title={copied ? "Copied" : "Copy the curl command to the clipboard"}
            aria-label={copied ? "Copied curl command" : "Copy curl command"}
          >
            {copied ? (
              /* checkmark icon */
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path
                  d="M5 13l4 4L19 7"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  fill="none"
                />
              </svg>
            ) : (
              /* clipboard icon */
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <rect x="9" y="9" width="11" height="11" rx="2" stroke="currentColor" strokeWidth="2" />
                <path
                  d="M5 15V5a2 2 0 0 1 2-2h10"
                  stroke="currentColor"
                  strokeWidth="2"
                  fill="none"
                />
              </svg>
            )}
          </button>
          <pre className="mcp-code">{curl}</pre>
        </div>
      </div>

      <div className="mcp-section">
        <div className="mcp-section-head">
          <span className="mcp-section-title">Raw output</span>
          <button className="mcp-run" onClick={run} disabled={!question.trim() || running}>
            {running ? "Running…" : "Run"}
          </button>
        </div>
        {error ? (
          <pre className="mcp-code mcp-error">{error}</pre>
        ) : stdout !== null ? (
          <pre className="mcp-code">{stdout}</pre>
        ) : (
          <div className="program-empty">
            Press <b>Run</b> to call the tool and show its output here.
          </div>
        )}
      </div>

      {!error && stdout !== null && (
        <div className="mcp-section">
          <div className="mcp-section-head">
            <span className="mcp-section-title">Rendered answer</span>
          </div>
          {answer !== null ? (
            <div className="mcp-rendered">
              <Markdown text={answer} />
            </div>
          ) : (
            <div className="program-empty">
              No markdown answer in this response (see Raw output above).
            </div>
          )}
        </div>
      )}
    </>
  );

  return <div className="mcp">{body}</div>;
}
