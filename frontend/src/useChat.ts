import { useCallback, useEffect, useRef, useState } from "react";
import { api, streamBuild, type OntologyInfo } from "./api";

export type Phase = "idle" | "building" | "ready" | "error";

export interface ChatMessage {
  id: string;
  role: "user" | "bot" | "system";
  text: string;
  rewrite?: string; // shown when a follow-up was rewritten
  pending?: boolean; // bot bubble awaiting the answer
}

export interface ChatState {
  ontologies: OntologyInfo[];
  ontology: string;
  phase: Phase;
  messages: ChatMessage[];
  buildLog: string;
  buildFailed: boolean; // keep the build log visible after a failure
  error: string | null;
  sending: boolean;
}

const uid = () => Math.random().toString(36).slice(2);

export function useChat() {
  const [state, setState] = useState<ChatState>({
    ontologies: [],
    ontology: "",
    phase: "idle",
    messages: [],
    buildLog: "",
    buildFailed: false,
    error: null,
    sending: false,
  });

  const conversationId = useRef<string | null>(null);
  const closeStream = useRef<(() => void) | null>(null);

  const patch = useCallback(
    (p: Partial<ChatState>) => setState((s) => ({ ...s, ...p })),
    [],
  );

  const addMessage = useCallback((m: Omit<ChatMessage, "id">) => {
    const msg = { ...m, id: uid() };
    setState((s) => ({ ...s, messages: [...s.messages, msg] }));
    return msg.id;
  }, []);

  // Initial load of the ontology list.
  useEffect(() => {
    api
      .ontologies()
      .then((ontologies) => patch({ ontologies }))
      .catch((e) => patch({ error: `Failed to load ontologies: ${e.message}` }));
    return () => {
      if (closeStream.current) closeStream.current();
    };
  }, [patch]);

  const startConversation = useCallback(
    async (ontology: string) => {
      const { conversation_id } = await api.conversation(ontology);
      conversationId.current = conversation_id;
      patch({ phase: "ready" });
      addMessage({ role: "system", text: `Ready. Ask questions about ${ontology}.` });
    },
    [patch, addMessage],
  );

  const streamBuildOutput = useCallback(
    (jobId: string, ontology: string) => {
      // Reset the live log for this build.
      setState((s) => ({ ...s, buildLog: "", buildFailed: false }));
      if (closeStream.current) closeStream.current();

      closeStream.current = streamBuild(
        jobId,
        // onLine: append each output line live.
        (line) =>
          setState((s) => ({ ...s, buildLog: s.buildLog ? `${s.buildLog}\n${line}` : line })),
        // onDone: switch to chat on success, or surface the failure (log stays visible).
        async (done) => {
          closeStream.current = null;
          if (done.status === "finished") {
            addMessage({ role: "system", text: `Build finished for ${ontology}.` });
            try {
              await startConversation(ontology);
            } catch (e) {
              patch({ phase: "error", error: (e as Error).message, buildFailed: true });
            }
          } else {
            patch({ phase: "error", buildFailed: true, error: `Build failed for ${ontology}.` });
            addMessage({
              role: "system",
              text: `Build failed for ${ontology}. See the build output above.`,
            });
          }
        },
        // onError: connection issue with the stream itself.
        (message) => patch({ phase: "error", buildFailed: true, error: message }),
      );
    },
    [patch, addMessage, startConversation],
  );

  const selectOntology = useCallback(
    async (ontology: string) => {
      if (!ontology) return;
      conversationId.current = null;
      if (closeStream.current) {
        closeStream.current();
        closeStream.current = null;
      }
      patch({
        ontology,
        phase: "idle",
        messages: [],
        buildLog: "",
        buildFailed: false,
        error: null,
      });
      addMessage({ role: "system", text: `Switching to ${ontology}…` });
      try {
        const res = await api.select(ontology);
        if (res.status === "ready") {
          await startConversation(ontology);
        } else {
          patch({ phase: "building" });
          streamBuildOutput(res.job_id, ontology);
        }
      } catch (e) {
        patch({ phase: "error", error: (e as Error).message });
        addMessage({ role: "system", text: `Error selecting ontology: ${(e as Error).message}` });
      }
    },
    [patch, addMessage, startConversation, streamBuildOutput],
  );

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || !conversationId.current) return;
      addMessage({ role: "user", text: trimmed });
      const botId = addMessage({ role: "bot", text: "…", pending: true });
      patch({ sending: true });
      try {
        const res = await api.chat(conversationId.current, trimmed);
        setState((s) => ({
          ...s,
          sending: false,
          messages: s.messages.map((m) =>
            m.id === botId
              ? {
                  ...m,
                  pending: false,
                  text: res.answer ?? "(no answer produced)",
                  rewrite: res.was_rewritten ? res.resolved_question : undefined,
                }
              : m,
          ),
        }));
      } catch (e) {
        setState((s) => ({
          ...s,
          sending: false,
          messages: s.messages.map((m) =>
            m.id === botId ? { ...m, pending: false, text: `Error: ${(e as Error).message}` } : m,
          ),
        }));
      }
    },
    [addMessage, patch],
  );

  const reset = useCallback(async () => {
    if (!state.ontology) return;
    patch({ messages: [] });
    await startConversation(state.ontology); // fresh conversation id => cleared history
  }, [state.ontology, patch, startConversation]);

  const rebuild = useCallback(async () => {
    const ontology = state.ontology;
    if (!ontology) return;
    conversationId.current = null;
    if (closeStream.current) {
      closeStream.current();
      closeStream.current = null;
    }
    patch({ phase: "building", messages: [], buildLog: "", buildFailed: false, error: null });
    addMessage({ role: "system", text: `Forcing a rebuild of the query program for ${ontology}…` });
    try {
      const res = await api.rebuild(ontology);
      streamBuildOutput(res.job_id, ontology);
    } catch (e) {
      patch({ phase: "error", error: (e as Error).message });
      addMessage({ role: "system", text: `Error starting rebuild: ${(e as Error).message}` });
    }
  }, [state.ontology, patch, addMessage, streamBuildOutput]);

  return { state, selectOntology, send, reset, rebuild };
}
