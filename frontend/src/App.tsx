import { useState } from "react";
import { Banner } from "./Banner";
import { Transcript } from "./Transcript";
import { Composer } from "./Composer";
import { ProgramView } from "./ProgramView";
import { useChat } from "./useChat";

export type View = "chat" | "program";

export function App() {
  const { state, selectOntology, send, reset, rebuild } = useChat();
  const chatReady = state.phase === "ready";
  const [view, setView] = useState<View>("chat");

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
      />
      {view === "chat" ? (
        <>
          <Transcript
            messages={state.messages}
            building={state.phase === "building"}
            buildFailed={state.buildFailed}
            ontology={state.ontology}
            buildLog={state.buildLog}
          />
          <Composer disabled={!chatReady} sending={state.sending} onSend={send} />
        </>
      ) : (
        <ProgramView ontology={state.ontology} />
      )}
    </div>
  );
}
