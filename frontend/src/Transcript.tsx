import { useEffect, useRef } from "react";
import type { ChatMessage } from "./types";
import { BuildProgress } from "./BuildProgress";

interface Props {
  messages: ChatMessage[];
  building: boolean;
  ontology: string;
  buildLog: string;
}

export function Transcript({ messages, building, ontology, buildLog }: Props) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, building, buildLog]);

  return (
    <main className="transcript">
      {messages.map((m) => (
        <div key={m.id} className={`msg ${m.role}${m.pending ? " pending" : ""}`}>
          {m.rewrite && <div className="rewrite">interpreted as: {m.rewrite}</div>}
          {m.text}
        </div>
      ))}
      {building && <BuildProgress ontology={ontology} log={buildLog} />}
      <div ref={endRef} />
    </main>
  );
}
