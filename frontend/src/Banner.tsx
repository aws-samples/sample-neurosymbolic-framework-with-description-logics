import type { OntologyInfo, ChatState } from "./types";

interface Props {
  ontologies: OntologyInfo[];
  ontology: string;
  phase: ChatState["phase"];
  onSelect: (ontology: string) => void;
  onReset: () => void;
}

const pillLabel: Record<ChatState["phase"], string> = {
  idle: "idle",
  building: "building",
  ready: "ready",
  error: "error",
};

export function Banner({ ontologies, ontology, phase, onSelect, onReset }: Props) {
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
      <span className={`pill ${phase}`}>
        {phase === "ready" && ontology ? `ready · ${ontology}` : pillLabel[phase]}
      </span>
      <span className="spacer" />
      <button className="ghost" onClick={onReset} disabled={phase !== "ready"}>
        Reset chat
      </button>
    </header>
  );
}
