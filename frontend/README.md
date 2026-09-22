# Ontology QA — React frontend

A React (Vite + TypeScript) chatbot UI for the ontology QA backend. It is the
componentized equivalent of the vanilla single-page UI shipped inside
`ontology_qa_cli/web/index.html`, and talks to the same FastAPI endpoints.

## Prerequisites

- Node.js 20+ and npm.
- The backend running: `uv sync --extra api && uv run ontology-qa-web`
  (serves the API on http://127.0.0.1:8000).

## Develop

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173
```

Vite proxies `/api/*` to the backend on port 8000, so the browser makes
same-origin requests (no CORS config needed). Point at a different backend with:

```bash
VITE_API_TARGET=http://host:port npm run dev
```

## Build

```bash
npm run build      # type-checks, then emits static assets to dist/
npm run preview    # serve the production build locally
```

`dist/` is a static bundle you can serve from any static host, or wire the
FastAPI backend to serve it.

## Structure

- `src/api.ts` — typed client, one function per backend endpoint.
- `src/useChat.ts` — the conversation state machine (idle → building → ready),
  ontology selection, build polling, and message sending.
- `src/App.tsx` — composes `Banner` (ontology dropdown + status), `Transcript`
  (messages + build progress), and `Composer` (input).

## Behavior

Selecting an ontology that already has a built program starts chatting
immediately. Selecting one with no program kicks off the builder as a
background job on the server; the UI shows live build progress and switches to
chat when the build finishes. Follow-up questions are resolved against
conversation history server-side, and when a message is rewritten the UI shows
"interpreted as: …", exactly like the CLI chatbot.
