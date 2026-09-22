import { useCallback, useEffect, useRef, useState } from "react";
import { api, type OntologyInfo } from "./api";

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
    error: null,
    sending: false,
  });

  const conversationId = useRef<string | null>(null);
  const pollTimer = useRef<number | null>(null);

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
      if (pollTimer.current) window.clearTimeout(pollTimer.current);
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

  const pollBuild = useCallback(
    async (jobId: string, ontology: string) => {
      try {
        const s = await api.status(jobId);
        patch({ buildLog: s.log });
        if (s.status === "running") {
          pollTimer.current = window.setTimeout(() => pollBuild(jobId, ontology), 2000);
        } else if (s.status === "finished") {
          addMessage({ role: "system", text: `Build finished for ${ontology}.` });
          await startConversation(ontology);
        } else {
          patch({ phase: "error", error: `Build failed for ${ontology}.` });
          addMessage({ role: "system", text: `Build failed for ${ontology}. See server log.` });
        }
      } catch (e) {
        patch({ phase: "error", error: (e as Error).message });
      }
    },
    [patch, addMessage, startConversation],
  );

  const selectOntology = useCallback(
    async (ontology: string) => {
      if (!ontology) return;
      conversationId.current = null;
      patch({ ontology, phase: "idle", messages: [], buildLog: "", error: null });
      addMessage({ role: "system", text: `Switching to ${ontology}…` });
      try {
        const res = await api.select(ontology);
        if (res.status === "ready") {
          await startConversation(ontology);
        } else {
          patch({ phase: "building" });
          pollBuild(res.job_id, ontology);
        }
      } catch (e) {
        patch({ phase: "error", error: (e as Error).message });
        addMessage({ role: "system", text: `Error selecting ontology: ${(e as Error).message}` });
      }
    },
    [patch, addMessage, startConversation, pollBuild],
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

  return { state, selectOntology, send, reset };
}
