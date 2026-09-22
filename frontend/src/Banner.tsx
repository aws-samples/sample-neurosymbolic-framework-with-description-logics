import type { OntologyInfo, ChatState } from "./types";
import type { View } from "./App";

interface Props {
  ontologies: OntologyInfo[];
  ontology: string;
  phase: ChatState["phase"];
  onSelect: (ontology: string) => void;
  onReset: () => void;
  onRebuild: () => void;
  view: View;
  onViewChange: (view: View) => void;
}

const pillLabel: Record<ChatState["phase"], string> = {
  idle: "idle",
  building: "building",
  ready: "ready",
  error: "error",
};

export function Banner({
  ontologies,
  ontology,
  phase,
  onSelect,
  onReset,
  onRebuild,
  view,
  onViewChange,
}: Props) {
  return (
    <header className="banner">
      <h1>Ontology QA</h1>
      <label htmlFor="ontology">Ontology</label>
      <select
        id="ontology"
        value={ontology}
        onChange={(e) => onSelect(e.target.value)}
      >
        <option value="">Choose an ontology…</option>
        {ontologies.map((o) => (
          <option key={o.name} value={o.name}>
            {o.has_program ? o.name : `${o.name} (needs build)`}
          </option>
        ))}
      </select>

      <div
        className="view-toggle"
        role="tablist"
        aria-label="Switch between chat and the query program"
      >
        <button
          role="tab"
          aria-selected={view === "chat"}
          className={view === "chat" ? "active" : ""}
          onClick={() => onViewChange("chat")}
        >
          Chat
        </button>
        <button
          role="tab"
          aria-selected={view === "program"}
          className={view === "program" ? "active" : ""}
          onClick={() => onViewChange("program")}
        >
          Query program
        </button>
      </div>

      <span className={`pill ${phase}`}>
        {phase === "ready" && ontology ? `ready · ${ontology}` : pillLabel[phase]}
      </span>
      <span className="spacer" />
      <button
        className="rebuild-btn"
        onClick={onRebuild}
        disabled={phase !== "ready"}
        title={
          phase === "ready"
            ? "Discard the current program and evolve a new one"
            : "Available once a query program exists for the selected ontology"
        }
      >
        Force rebuild of query program
      </button>
      <button className="ghost" onClick={onReset} disabled={phase !== "ready"}>
        Reset chat
      </button>
    </header>
  );
}
