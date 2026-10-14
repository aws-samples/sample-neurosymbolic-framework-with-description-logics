import { useEffect, useRef, useState } from "react";
import type { ChatMessage } from "./types";
import { BuildProgress } from "./BuildProgress";
import { Markdown } from "./Markdown";

/**
 * Copy-to-clipboard control pinned to the top-right of a bot answer bubble.
 * Copies the raw Markdown source of the answer (not the rendered HTML), and
 * shows a brief "Copied!" confirmation.
 */
function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout>>();

  useEffect(() => () => clearTimeout(timer.current), []);

  const onCopy = async () => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
      } else {
        // Fallback for insecure contexts / older browsers.
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      setCopied(true);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard denied — leave the icon unchanged */
    }
  };

  return (
    <button
      type="button"
      className={`copy-md${copied ? " copied" : ""}`}
      onClick={onCopy}
      title="Copy raw Markdown"
      aria-label={copied ? "Copied" : "Copy raw Markdown"}
    >
      {copied ? (
        <span aria-hidden>✓</span>
      ) : (
        <svg
          width="15"
          height="15"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden
        >
          <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
        </svg>
      )}
    </button>
  );
}

interface Props {
  messages: ChatMessage[];
  building: boolean;
  buildFailed: boolean;
  rebuilding: boolean;
  ontology: string;
  buildLog: string;
  onToggleWork: (messageId: string) => void;
}

export function Transcript({
  messages,
  building,
  buildFailed,
  rebuilding,
  ontology,
  buildLog,
  onToggleWork,
}: Props) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, building, buildFailed, buildLog]);

  return (
    <main className="transcript">
      {messages.map((m) => {
        // A bot bubble shows the collapsible "Working..." disclosure whenever it
        // has (or is accumulating) program output: while pending it replaces the
        // bubble text; after the answer arrives it stays available to expand.
        const hasWork = m.role === "bot" && m.workLog !== undefined;
        // Show the copy control on finished bot answers only (not pending, not
        // the centered system/status bubbles). Copies the raw Markdown source.
        const showCopy = m.role === "bot" && !m.pending && m.text.trim().length > 0;
        return (
          <div key={m.id} className={`msg ${m.role}${m.pending ? " pending" : ""}`}>
            {showCopy && <CopyButton text={m.text} />}
            {m.rewrite && <div className="rewrite">interpreted as: {m.rewrite}</div>}
            {hasWork ? (
              <>
                <button
                  type="button"
                  className="work-toggle"
                  aria-expanded={!!m.workExpanded}
                  onClick={() => onToggleWork(m.id)}
                >
                  <span className={`flippy${m.workExpanded ? " open" : ""}`} aria-hidden>
                    ▶
                  </span>
                  <span className="work-label">
                    {m.pending ? "Working..." : "Working output"}
                  </span>
                </button>
                {m.workExpanded && (
                  <pre className="work-log">{m.workLog || "starting…"}</pre>
                )}
                {!m.pending && (
                  <div className="work-answer">
                    <Markdown text={m.text} />
                  </div>
                )}
              </>
            ) : (
              m.text
            )}
          </div>
        );
      })}
      {(building || buildFailed) && (
        <BuildProgress ontology={ontology} log={buildLog} failed={buildFailed} rebuilding={rebuilding} />
      )}
      <div ref={endRef} />
    </main>
  );
}
