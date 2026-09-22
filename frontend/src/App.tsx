import { Banner } from "./Banner";
import { Transcript } from "./Transcript";
import { Composer } from "./Composer";
import { useChat } from "./useChat";

export function App() {
  const { state, selectOntology, send, reset } = useChat();
  const chatReady = state.phase === "ready";

  return (
    <div className="app">
      <Banner
        ontologies={state.ontologies}
        ontology={state.ontology}
        phase={state.phase}
        onSelect={selectOntology}
        onReset={reset}
      />
      <Transcript
        messages={state.messages}
        building={state.phase === "building"}
        ontology={state.ontology}
        buildLog={state.buildLog}
      />
      <Composer disabled={!chatReady} sending={state.sending} onSend={send} />
    </div>
  );
}
