import { useEffect, useRef } from "react";
import type { ChatMessage } from "./types";
import { BuildProgress } from "./BuildProgress";

interface Props {
  messages: ChatMessage[];
  building: boolean;
  buildFailed: boolean;
  ontology: string;
  buildLog: string;
}

export function Transcript({ messages, building, buildFailed, ontology, buildLog }: Props) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, building, buildFailed, buildLog]);

  return (
    <main className="transcript">
      {messages.map((m) => (
        <div key={m.id} className={`msg ${m.role}${m.pending ? " pending" : ""}`}>
          {m.rewrite && <div className="rewrite">interpreted as: {m.rewrite}</div>}
          {m.text}
        </div>
      ))}
      {(building || buildFailed) && (
        <BuildProgress ontology={ontology} log={buildLog} failed={buildFailed} />
      )}
      <div ref={endRef} />
    </main>
  );
}
