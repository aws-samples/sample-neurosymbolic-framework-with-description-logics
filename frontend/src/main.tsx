import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { ensureAuthenticated, freshIdToken } from "./auth";
import "./styles.css";

// Gate the SPA on a valid Cognito session before rendering. When auth is
// enabled and the user is not logged in, this navigates away to the hosted UI
// (or completes the ?code= exchange on the way back) and the app mounts only
// once a token is held. When auth is disabled (local dev build with no Cognito
// config) this resolves immediately and the app renders as before.
//
// After ensureAuthenticated() we additionally await freshIdToken(): that
// PROACTIVELY refreshes a token in its final 90s BEFORE the app mounts, so the
// one-shot ontology auto-restore (which opens an authenticated SSE stream the
// instant the ontology list loads) runs with a comfortably-valid token rather
// than one 30s from expiry that would 401 the stream mid-open on a hard reload
// ("Lost connection while selecting the ontology").
void ensureAuthenticated()
  .then(() => freshIdToken())
  .then(() => {
    ReactDOM.createRoot(document.getElementById("root")!).render(
      <React.StrictMode>
        <App />
      </React.StrictMode>,
    );
  })
  .catch((e) => {
    const root = document.getElementById("root");
    if (root) {
      root.textContent = `Sign-in failed: ${(e as Error).message}`;
    }
  });
