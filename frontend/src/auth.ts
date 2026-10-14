// Browser-side Cognito login via the hosted UI, using the OAuth
// authorization-code grant with PKCE (public client, no secret).
//
// Flow
// ----
//  1. On load, `ensureAuthenticated()` checks for a stored, unexpired id token.
//  2. If none, and the URL carries a `?code=` (we just came back from the
//     hosted UI), it exchanges the code + PKCE verifier for tokens, stores them,
//     and strips the query string.
//  3. Otherwise it redirects the browser to the hosted UI login page, stashing a
//     fresh PKCE verifier + state first.
//  4. API calls attach the id token as `Authorization: Bearer`; SSE (EventSource,
//     which cannot set headers) gets it as an `?access_token=` query param.
//
// Config comes from Vite build-time env (see `frontend/.env` / CI):
//   VITE_COGNITO_DOMAIN     e.g. https://neurosymbolic-dl-<acct>.auth.us-east-1.amazoncognito.com
//   VITE_COGNITO_CLIENT_ID  the PUBLIC SPA app client id (no secret)
//   VITE_COGNITO_REDIRECT   the SPA origin, e.g. https://d15j8kb5manvuj.cloudfront.net/
//
// When these are unset (local dev against an open backend) auth is DISABLED and
// every helper is a no-op, so `npm run dev` keeps working with no Cognito.

interface Tokens {
  idToken: string;
  accessToken: string;
  refreshToken?: string;
  expiresAt: number; // epoch seconds
}

const DOMAIN = (import.meta.env.VITE_COGNITO_DOMAIN ?? "").replace(/\/+$/, "");
const CLIENT_ID = import.meta.env.VITE_COGNITO_CLIENT_ID ?? "";
const REDIRECT_URI =
  import.meta.env.VITE_COGNITO_REDIRECT ?? window.location.origin + "/";

const STORAGE_KEY = "ontology_qa_tokens";
const PKCE_VERIFIER_KEY = "ontology_qa_pkce_verifier";
const PKCE_STATE_KEY = "ontology_qa_pkce_state";

/** Auth is only active when the SPA was built with Cognito config. */
export const authEnabled = (): boolean => Boolean(DOMAIN && CLIENT_ID);

// ---------------------------------------------------------------- token store
function loadTokens(): Tokens | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Tokens) : null;
  } catch {
    return null;
  }
}

function saveTokens(t: Tokens): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(t));
}

export function clearTokens(): void {
  localStorage.removeItem(STORAGE_KEY);
}

/** A valid, unexpired id token, or null. 30s skew guard. */
function currentIdToken(): string | null {
  const t = loadTokens();
  if (!t) return null;
  if (t.expiresAt - 30 <= Math.floor(Date.now() / 1000)) return null;
  return t.idToken;
}

/**
 * Return a valid id token, refreshing an expired one first and, if that fails,
 * bouncing to the hosted-UI login (never resolving, since the page navigates
 * away). Resolves to the token when auth is active, or `null` when auth is
 * disabled (local dev build without Cognito config).
 *
 * Use this BEFORE opening an EventSource: `withAuthParam` can only read the
 * token synchronously and silently sends NONE when the stored one has expired,
 * which makes the authenticated SSE endpoints 401 and the stream close with no
 * completion frame (surfaced to the user as "Lost connection…"). Awaiting a
 * fresh token first keeps that from happening in the common token-expiry case.
 */
export async function freshIdToken(): Promise<string | null> {
  if (!authEnabled()) return null;
  const current = currentIdToken();
  if (current) return current;
  // Stored token missing/expired: try a silent refresh, else full re-login.
  const refreshed = await refresh();
  if (refreshed) return refreshed.idToken;
  await ensureAuthenticated(); // refreshes or navigates to the hosted UI
  return currentIdToken();
}

// ------------------------------------------------------------------ PKCE util
function base64url(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomString(len = 64): string {
  const bytes = new Uint8Array(len);
  crypto.getRandomValues(bytes);
  return base64url(bytes);
}

async function sha256(input: string): Promise<Uint8Array> {
  const data = new TextEncoder().encode(input);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return new Uint8Array(digest);
}

// ----------------------------------------------------------------- login flow
async function redirectToLogin(): Promise<void> {
  const verifier = randomString();
  const state = randomString(16);
  sessionStorage.setItem(PKCE_VERIFIER_KEY, verifier);
  sessionStorage.setItem(PKCE_STATE_KEY, state);
  const challenge = base64url(await sha256(verifier));
  const params = new URLSearchParams({
    response_type: "code",
    client_id: CLIENT_ID,
    redirect_uri: REDIRECT_URI,
    scope: "openid email",
    state,
    code_challenge: challenge,
    code_challenge_method: "S256",
  });
  window.location.assign(`${DOMAIN}/oauth2/authorize?${params.toString()}`);
}

async function exchangeCode(code: string): Promise<Tokens> {
  const verifier = sessionStorage.getItem(PKCE_VERIFIER_KEY) ?? "";
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: CLIENT_ID,
    code,
    redirect_uri: REDIRECT_URI,
    code_verifier: verifier,
  });
  const res = await fetch(`${DOMAIN}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!res.ok) throw new Error(`Token exchange failed: ${res.status}`);
  const json = (await res.json()) as {
    id_token: string;
    access_token: string;
    refresh_token?: string;
    expires_in: number;
  };
  sessionStorage.removeItem(PKCE_VERIFIER_KEY);
  sessionStorage.removeItem(PKCE_STATE_KEY);
  return {
    idToken: json.id_token,
    accessToken: json.access_token,
    refreshToken: json.refresh_token,
    expiresAt: Math.floor(Date.now() / 1000) + json.expires_in,
  };
}

async function refresh(): Promise<Tokens | null> {
  const t = loadTokens();
  if (!t?.refreshToken) return null;
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    client_id: CLIENT_ID,
    refresh_token: t.refreshToken,
  });
  const res = await fetch(`${DOMAIN}/oauth2/token`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!res.ok) return null;
  const json = (await res.json()) as {
    id_token: string;
    access_token: string;
    expires_in: number;
  };
  const next: Tokens = {
    idToken: json.id_token,
    accessToken: json.access_token,
    refreshToken: t.refreshToken, // Cognito does not rotate refresh tokens here
    expiresAt: Math.floor(Date.now() / 1000) + json.expires_in,
  };
  saveTokens(next);
  return next;
}

/**
 * Ensure the user is logged in. Resolves when a valid token is held; otherwise
 * it either completes the redirect-back code exchange or sends the browser to
 * the hosted UI (and never resolves, since the page navigates away).
 *
 * No-op when auth is disabled (local dev build without Cognito config).
 */
export async function ensureAuthenticated(): Promise<void> {
  if (!authEnabled()) return;

  if (currentIdToken()) return;

  // Returning from the hosted UI with ?code=...
  const url = new URL(window.location.href);
  const code = url.searchParams.get("code");
  const returnedState = url.searchParams.get("state");
  if (code) {
    const expectedState = sessionStorage.getItem(PKCE_STATE_KEY);
    if (expectedState && returnedState !== expectedState) {
      throw new Error("OAuth state mismatch — possible CSRF, aborting login.");
    }
    const tokens = await exchangeCode(code);
    saveTokens(tokens);
    // Strip the code/state from the URL without a reload.
    url.searchParams.delete("code");
    url.searchParams.delete("state");
    window.history.replaceState({}, "", url.pathname + url.search + url.hash);
    return;
  }

  // Try a silent refresh before bouncing to the login page.
  if (await refresh()) return;

  await redirectToLogin();
  // Unreachable: the browser is navigating to the hosted UI.
  await new Promise(() => {});
}

/** Log out: clear local tokens and hit the hosted-UI logout endpoint. */
export function logout(): void {
  clearTokens();
  if (!authEnabled()) {
    window.location.reload();
    return;
  }
  const params = new URLSearchParams({
    client_id: CLIENT_ID,
    logout_uri: REDIRECT_URI,
  });
  window.location.assign(`${DOMAIN}/logout?${params.toString()}`);
}

// ---------------------------------------------------------- request decorators
/** Authorization header for fetch(), empty when auth is disabled/absent. */
export function authHeaders(): Record<string, string> {
  const token = currentIdToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/**
 * Append the token to an EventSource URL as `access_token` (EventSource cannot
 * set headers). Returns the URL unchanged when auth is disabled.
 */
export function withAuthParam(url: string): string {
  const token = currentIdToken();
  if (!token) return url;
  const u = new URL(url, window.location.origin);
  u.searchParams.set("access_token", token);
  // Return relative (same-origin) form so dev proxy + prod CloudFront both work.
  return u.pathname + u.search;
}

/**
 * On a 401 from the API, the token is stale/revoked: clear it and re-login.
 * Called by the api layer. No-op when auth is disabled.
 */
export function handleUnauthorized(): void {
  if (!authEnabled()) return;
  clearTokens();
  void ensureAuthenticated();
}
