import { useState } from "react";
import { Banner } from "./Banner";
import { Transcript } from "./Transcript";
import { Composer } from "./Composer";
import { ProgramView } from "./ProgramView";
import { McpView } from "./McpView";
import { useChat } from "./useChat";

export type View = "chat" | "program" | "mcp";

export function App() {
  const { state, selectOntology, send, reset, rebuild, toggleWork } = useChat();
  const chatReady = state.phase === "ready";
  const [view, setView] = useState<View>("chat");

  const currentOntology = state.ontologies.find((o) => o.name === state.ontology);
  const mcpEnabled = Boolean(currentOntology?.mcp_enabled);

  // If the MCP tab is active and the user switches to an ontology that is not
  // MCP-wired, fall back to the chat view so we never show an empty MCP tab.
  if (view === "mcp" && !mcpEnabled) {
    // Defer to avoid setState-in-render; harmless because setView is a no-op
    // when the value is unchanged.
    queueMicrotask(() => setView("chat"));
  }

  return (
    <div className="app">
      <Banner
        ontologies={state.ontologies}
        ontology={state.ontology}
        phase={state.phase}
        onSelect={selectOntology}
        onReset={reset}
        onRebuild={() => {
          setView("chat"); // build log streams into the chat view
          rebuild();
        }}
        view={view}
        onViewChange={setView}
        mcpEnabled={mcpEnabled}
      />
      {view === "chat" ? (
        <>
          <Transcript
            messages={state.messages}
            building={state.phase === "building"}
            buildFailed={state.buildFailed}
            rebuilding={state.rebuilding}
            ontology={state.ontology}
            buildLog={state.buildLog}
            onToggleWork={toggleWork}
          />
          {state.phase !== "building" && (
            <Composer
              disabled={!chatReady}
              sending={state.sending}
              onSend={send}
              questions={currentOntology?.questions}
            />
          )}
        </>
      ) : view === "program" ? (
        <ProgramView ontology={state.ontology} />
      ) : (
        <McpView ontology={state.ontology} questions={currentOntology?.questions} />
      )}
    </div>
  );
}
