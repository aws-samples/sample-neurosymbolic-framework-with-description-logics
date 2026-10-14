import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { authHeaders } from "./auth";

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

export function McpView({ ontology, questions }: Props) {
  const seed = (questions && questions[0]) || "";
  const [question, setQuestion] = useState(seed);
  const [stdout, setStdout] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const copyTimer = useRef<number | null>(null);

  // Reset the input to the new ontology's first canned question on switch.
  useEffect(() => {
    setQuestion((questions && questions[0]) || "");
    setStdout(null);
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
          setStdout(formatStdout(JSON.parse(text)));
        } catch {
          setStdout(text);
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
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") run();
            }}
          />
        </label>
      </div>

      <div className="mcp-section">
        <div className="mcp-section-head">
          <span className="mcp-section-title">Sample curl</span>
          <button
            className="mcp-copy"
            onClick={copyCurl}
            title="Copy the curl command to the clipboard"
            aria-label="Copy curl command"
          >
            {/* clipboard icon */}
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <rect x="9" y="9" width="11" height="11" rx="2" stroke="currentColor" strokeWidth="2" />
              <path
                d="M5 15V5a2 2 0 0 1 2-2h10"
                stroke="currentColor"
                strokeWidth="2"
                fill="none"
              />
            </svg>
            <span>{copied ? "Copied" : "Copy"}</span>
          </button>
        </div>
        <pre className="mcp-code">{curl}</pre>
      </div>

      <div className="mcp-section">
        <div className="mcp-section-head">
          <span className="mcp-section-title">Output</span>
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
    </>
  );

  return <div className="mcp">{body}</div>;
}
