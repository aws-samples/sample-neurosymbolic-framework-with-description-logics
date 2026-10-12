import { useCallback, useEffect, useRef, useState } from "react";
import { api, streamBuild, streamChat, streamSelect, type OntologyInfo } from "./api";

export type Phase = "idle" | "building" | "ready" | "error";

export interface ChatMessage {
  id: string;
  role: "user" | "bot" | "system";
  text: string;
  rewrite?: string; // shown when a follow-up was rewritten
  pending?: boolean; // bot bubble awaiting the answer
  workLog?: string; // live stdout/stderr of the running program
  workExpanded?: boolean; // whether the "Working..." disclosure is open
}

export interface ChatState {
  ontologies: OntologyInfo[];
  ontology: string;
  phase: Phase;
  messages: ChatMessage[];
  buildLog: string;
  buildFailed: boolean; // keep the build log visible after a failure
  rebuilding: boolean; // true when the current build is a forced rebuild (a program already exists)
  error: string | null;
  sending: boolean;
}

const uid = () => Math.random().toString(36).slice(2);

// Remember the last-selected ontology across page reloads.
const LAST_ONTOLOGY_KEY = "ndl.lastOntology";
const loadLastOntology = (): string | null => {
  try {
    return localStorage.getItem(LAST_ONTOLOGY_KEY);
  } catch {
    return null;
  }
};
const saveLastOntology = (ontology: string) => {
  try {
    localStorage.setItem(LAST_ONTOLOGY_KEY, ontology);
  } catch {
    // localStorage unavailable (private mode / disabled) — persistence is best-effort.
  }
};

export function useChat() {
  const [state, setState] = useState<ChatState>({
    ontologies: [],
    ontology: "",
    phase: "idle",
    messages: [],
    buildLog: "",
    buildFailed: false,
    rebuilding: false,
    error: null,
    sending: false,
  });

  const conversationId = useRef<string | null>(null);
  const closeStream = useRef<(() => void) | null>(null);
  const restored = useRef(false);

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
    (ontology: string) => {
      if (!ontology) return;
      saveLastOntology(ontology);
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
      const statusId = addMessage({ role: "system", text: `Switching to ${ontology}…` });

      // Update the switching message's text in place as each phase arrives.
      const setStatus = (text: string) =>
        setState((s) => ({
          ...s,
          messages: s.messages.map((m) => (m.id === statusId ? { ...m, text } : m)),
        }));

      closeStream.current = streamSelect(
        ontology,
        // onPhase: show what the load is doing right now.
        (label) => setStatus(`${ontology}: ${label}`),
        // onDone: ready -> open the chat; building -> stream the build; failed -> error.
        (done) => {
          closeStream.current = null;
          if (done.status === "ready") {
            setStatus(`Switched to ${ontology}.`);
            void startConversation(ontology);
          } else if (done.status === "building") {
            patch({ phase: "building", rebuilding: false });
            streamBuildOutput(done.job_id, ontology);
          } else {
            patch({ phase: "error", error: done.error ?? "Failed to load program." });
            setStatus(`Error selecting ontology: ${done.error ?? "Failed to load program."}`);
          }
        },
        // onError: connection/transport failure on the select stream.
        (message) => {
          closeStream.current = null;
          patch({ phase: "error", error: message });
          setStatus(`Error selecting ontology: ${message}`);
        },
      );
    },
    [patch, addMessage, startConversation, streamBuildOutput],
  );

  // Restore the last-selected ontology once the list has loaded (one-shot).
  useEffect(() => {
    if (restored.current) return;
    if (state.ontologies.length === 0) return;
    restored.current = true;
    const last = loadLastOntology();
    if (last && state.ontologies.some((o) => o.name === last)) {
      selectOntology(last);
    }
  }, [state.ontologies, selectOntology]);

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || !conversationId.current) return;
      addMessage({ role: "user", text: trimmed });
      const botId = addMessage({
        role: "bot",
        text: "Working...",
        pending: true,
        workLog: "",
        workExpanded: false,
      });
      patch({ sending: true });

      // Stream the running program's stdout/stderr into the pending bubble; the
      // final `done` event carries the answer. streamChat never rejects — it
      // reports terminal state through onDone / onError.
      streamChat(
        conversationId.current,
        trimmed,
        // onLine: append each program output line to this bubble's work log.
        (line) =>
          setState((s) => ({
            ...s,
            messages: s.messages.map((m) =>
              m.id === botId
                ? { ...m, workLog: m.workLog ? `${m.workLog}\n${line}` : line }
                : m,
            ),
          })),
        // onDone: replace the placeholder with the answer (work log preserved,
        // still collapsed so the user can expand it after the fact).
        (done) =>
          setState((s) => ({
            ...s,
            sending: false,
            messages: s.messages.map((m) =>
              m.id === botId
                ? {
                    ...m,
                    pending: false,
                    text: done.answer ?? "(no answer produced)",
                    rewrite: done.was_rewritten ? done.resolved_question : undefined,
                  }
                : m,
            ),
          })),
        // onError: surface the failure in the bubble; keep the work log.
        (message) =>
          setState((s) => ({
            ...s,
            sending: false,
            messages: s.messages.map((m) =>
              m.id === botId ? { ...m, pending: false, text: `Error: ${message}` } : m,
            ),
          })),
      );
    },
    [addMessage, patch],
  );

  const toggleWork = useCallback((messageId: string) => {
    setState((s) => ({
      ...s,
      messages: s.messages.map((m) =>
        m.id === messageId ? { ...m, workExpanded: !m.workExpanded } : m,
      ),
    }));
  }, []);

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
    patch({ phase: "building", messages: [], buildLog: "", buildFailed: false, rebuilding: true, error: null });
    addMessage({ role: "system", text: `Forcing a rebuild of the query program for ${ontology}…` });
    try {
      const res = await api.rebuild(ontology);
      streamBuildOutput(res.job_id, ontology);
    } catch (e) {
      patch({ phase: "error", error: (e as Error).message });
      addMessage({ role: "system", text: `Error starting rebuild: ${(e as Error).message}` });
    }
  }, [state.ontology, patch, addMessage, streamBuildOutput]);

  return { state, selectOntology, send, reset, rebuild, toggleWork };
}
