import { useEffect, useRef } from "react";
import type { ChatMessage } from "./types";
import { BuildProgress } from "./BuildProgress";
import { Markdown } from "./Markdown";

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
        return (
          <div key={m.id} className={`msg ${m.role}${m.pending ? " pending" : ""}`}>
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
