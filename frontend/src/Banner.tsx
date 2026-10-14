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
  /** Whether the selected ontology is served by the MCP server (shows the MCP tab). */
  mcpEnabled: boolean;
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
  mcpEnabled,
}: Props) {
  return (
    <header className="banner">
      <h1>Ontology QA</h1>
      <select
        id="ontology"
        aria-label="Ontology"
        value={ontology}
        onChange={(e) => onSelect(e.target.value)}
      >
        <option value="">Choose an ontology…</option>
        {ontologies.map((o) => {
          const full = o.display_name ? `${o.display_name} (${o.name})` : o.name;
          return (
            <option key={o.name} value={o.name}>
              {o.has_program ? full : `${full} (needs build)`}
            </option>
          );
        })}
      </select>

      <div
        className="view-toggle"
        role="tablist"
        aria-label="Switch between chat, the query program, and MCP"
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
        {mcpEnabled && (
          <button
            role="tab"
            aria-selected={view === "mcp"}
            className={view === "mcp" ? "active" : ""}
            onClick={() => onViewChange("mcp")}
            title="This ontology is wired up with the MCP server"
          >
            MCP
          </button>
        )}
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
      {view === "chat" && (
        <button className="ghost" onClick={onReset} disabled={phase !== "ready"}>
          Reset chat
        </button>
      )}
    </header>
  );
}
