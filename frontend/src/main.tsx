import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { ensureAuthenticated } from "./auth";
import "./styles.css";

// Gate the SPA on a valid Cognito session before rendering. When auth is
// enabled and the user is not logged in, this navigates away to the hosted UI
// (or completes the ?code= exchange on the way back) and the app mounts only
// once a token is held. When auth is disabled (local dev build with no Cognito
// config) this resolves immediately and the app renders as before.
void ensureAuthenticated()
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
