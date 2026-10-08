"""Application-level Cognito JWT authentication for the FastAPI apps.

Both the web app (``webapp.create_app``) and the MCP server
(``mcp_server.create_mcp_app``) call :func:`install_cognito_auth` to gate every
request on a valid Amazon Cognito-issued JWT. This is the "behind Cognito"
enforcement done at the *application* layer rather than at the ALB, so it needs
**no ACM certificate** -- TLS is already terminated by CloudFront at the edge
and the ALB can stay plain HTTP behind it. It also works for both browser and
programmatic (MCP) clients, which the ALB ``authenticate-cognito`` browser
redirect flow does not.

How a request is authenticated
------------------------------
Each request must carry ``Authorization: Bearer <jwt>``. The token is verified
against the user pool's published JWKS (JSON Web Key Set):

  * signature (RS256) against the pool's rotating public keys,
  * ``exp`` / ``nbf`` (not expired / not before),
  * ``iss`` equals the pool issuer URL,
  * ``token_use`` is ``access`` or ``id`` (both accepted),
  * audience: an *id* token's ``aud`` or an *access* token's ``client_id`` must
    match the configured app client id (when one is configured).

Configuration (environment)
---------------------------
``COGNITO_USER_POOL_ID``   e.g. ``us-east-1_abc123`` -- REQUIRED to enable auth.
``COGNITO_REGION``         AWS region of the pool. Optional: defaults to the
                           region prefix of the pool id (``us-east-1`` above).
``COGNITO_CLIENT_ID``      app client id. Optional but recommended: when set the
                           token's audience/client_id must match it.
``COGNITO_AUTH_DISABLED``  set truthy to force-disable even if a pool id is set
                           (useful for a local run against the real config).

**Auth is DISABLED when no ``COGNITO_USER_POOL_ID`` is configured.** That keeps
local development, the test suite, and the pre-existing deployment working
unchanged; it is enabled only once the CDK compute stack injects the pool
variables into the container (which it now does).

Dependencies
------------
Verification uses :mod:`jwt` (PyJWT with the ``crypto`` extra for RS256), added
to the ``api`` optional-dependency group. If PyJWT is missing *and* auth is
configured, :func:`install_cognito_auth` raises -- a configured-but-unenforceable
state must fail loudly rather than silently allow every request.
"""
from __future__ import annotations

import os
import time
from typing import Iterable, Optional

try:  # FastAPI/Starlette are only present with the `api` extra.
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from starlette.requests import Request
except ImportError as exc:  # pragma: no cover - only without the api extra
    raise ImportError(
        "FastAPI is required for auth. Install with `uv sync --extra api`."
    ) from exc


# Paths that must stay reachable WITHOUT a token, no matter what. The ALB target
# groups health-check these (web: GET /api/ontologies, mcp: GET /mcp/health); a
# gated health check would fail the target and take the service out of service.
# Keep this list tiny and GET-only.
_PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/api/ontologies",  # web ALB target-group health check
        "/mcp/health",      # mcp ALB target-group health check
    }
)


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _region_from_pool_id(pool_id: str) -> Optional[str]:
    # A Cognito user pool id is "<region>_<suffix>", e.g. "us-east-1_abc123".
    if "_" in pool_id:
        return pool_id.split("_", 1)[0]
    return None


class CognitoConfig:
    """Resolved Cognito settings read from the environment."""

    def __init__(
        self,
        pool_id: str,
        region: str,
        client_id: Optional[str],
    ) -> None:
        self.pool_id = pool_id
        self.region = region
        self.client_id = client_id
        self.issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"
        self.jwks_url = f"{self.issuer}/.well-known/jwks.json"

    @classmethod
    def from_env(cls) -> "Optional[CognitoConfig]":
        """Build config from env, or None when auth is not configured/disabled."""
        if _truthy(os.environ.get("COGNITO_AUTH_DISABLED")):
            return None
        pool_id = (os.environ.get("COGNITO_USER_POOL_ID") or "").strip()
        if not pool_id:
            return None
        region = (
            (os.environ.get("COGNITO_REGION") or "").strip()
            or _region_from_pool_id(pool_id)
            or (os.environ.get("AWS_REGION") or "").strip()
        )
        if not region:
            raise RuntimeError(
                "COGNITO_USER_POOL_ID is set but the region could not be "
                "determined. Set COGNITO_REGION explicitly."
            )
        client_id = (os.environ.get("COGNITO_CLIENT_ID") or "").strip() or None
        return cls(pool_id=pool_id, region=region, client_id=client_id)


class _JwksCache:
    """Fetches and caches a user pool's JWKS, keyed by `kid`.

    Cognito rotates signing keys, so a key id missing from the cache triggers a
    single refresh before the token is rejected. The cache has a soft TTL to
    pick up rotation even for tokens whose kid was already seen.
    """

    def __init__(self, jwks_url: str, ttl_secs: float = 3600.0) -> None:
        self._jwks_url = jwks_url
        self._ttl = ttl_secs
        self._keys: dict[str, object] = {}
        self._fetched_at = 0.0

    def _refresh(self) -> None:
        import json
        import urllib.request

        from jwt.algorithms import RSAAlgorithm

        with urllib.request.urlopen(self._jwks_url, timeout=5) as resp:  # noqa: S310 - fixed https Cognito URL
            data = json.loads(resp.read().decode("utf-8"))
        keys: dict[str, object] = {}
        for jwk in data.get("keys", []):
            kid = jwk.get("kid")
            if kid:
                keys[kid] = RSAAlgorithm.from_jwk(json.dumps(jwk))
        self._keys = keys
        self._fetched_at = time.time()

    def get(self, kid: str):
        """Return the public key for ``kid``, refreshing once on a miss/expiry."""
        fresh = (time.time() - self._fetched_at) < self._ttl
        if kid in self._keys and fresh:
            return self._keys[kid]
        # Miss or stale -> refresh once, then look again.
        self._refresh()
        return self._keys.get(kid)


def _verify_token(token: str, cfg: CognitoConfig, jwks: _JwksCache) -> None:
    """Validate a Cognito JWT or raise ``_AuthError`` with a client-safe reason."""
    import jwt  # PyJWT

    try:
        header = jwt.get_unverified_header(token)
    except Exception as exc:  # noqa: BLE001
        raise _AuthError(f"malformed token header: {exc}")
    kid = header.get("kid")
    if not kid:
        raise _AuthError("token header has no 'kid'")
    key = jwts_get(jwks, kid)
    if key is None:
        raise _AuthError("signing key not found for token 'kid'")

    # Cognito access tokens carry no 'aud' (the client id is in 'client_id'),
    # while id tokens carry 'aud'. Verify the audience ourselves after decode so
    # both token types are accepted with one code path.
    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=["RS256"],
            issuer=cfg.issuer,
            options={"verify_aud": False, "require": ["exp", "iss"]},
        )
    except Exception as exc:  # noqa: BLE001 - PyJWT raises many subclasses
        raise _AuthError(f"token rejected: {exc}")

    token_use = claims.get("token_use")
    if token_use not in ("access", "id"):
        raise _AuthError(f"unexpected token_use '{token_use}'")

    if cfg.client_id:
        # id token -> aud; access token -> client_id.
        audience = claims.get("aud") or claims.get("client_id")
        if audience != cfg.client_id:
            raise _AuthError("token audience does not match the app client")


def jwts_get(jwks: _JwksCache, kid: str):
    """Indirection kept trivial so tests can monkeypatch key resolution."""
    return jwks.get(kid)


class _AuthError(Exception):
    """A request-authentication failure; its message is returned to the client."""


def _extract_bearer(request: "Request") -> Optional[str]:
    header = request.headers.get("authorization") or request.headers.get("Authorization")
    if not header:
        return None
    parts = header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        return None
    return parts[1].strip()


def install_cognito_auth(
    app: "FastAPI",
    *,
    public_paths: "Optional[Iterable[str]]" = None,
) -> bool:
    """Install Cognito JWT enforcement on ``app`` when configured.

    Returns ``True`` if auth was installed (pool configured), ``False`` if auth
    is disabled (no pool id / explicitly disabled) -- in which case the app
    serves unauthenticated exactly as before.

    ``public_paths`` overrides the default set of always-open paths (the ALB
    health-check endpoints). Pass an empty iterable to gate everything.
    """
    cfg = CognitoConfig.from_env()
    if cfg is None:
        return False

    # Fail loudly if we are configured to enforce auth but cannot verify tokens.
    try:
        import jwt  # noqa: F401
        from jwt.algorithms import RSAAlgorithm  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise RuntimeError(
            "COGNITO_USER_POOL_ID is set (auth enabled) but PyJWT is not "
            "installed. Add it with the `api` extra: `uv sync --extra api`."
        ) from exc

    open_paths = frozenset(public_paths) if public_paths is not None else _PUBLIC_PATHS
    jwks = _JwksCache(cfg.jwks_url)

    def _unauthorized(detail: str) -> "JSONResponse":
        return JSONResponse(
            {"detail": detail},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.middleware("http")
    async def _cognito_auth_mw(request: "Request", call_next):
        # Always allow CORS preflight and the health-check endpoints through.
        if request.method == "OPTIONS" or request.url.path in open_paths:
            return await call_next(request)

        token = _extract_bearer(request)
        if token is None:
            return _unauthorized("Missing bearer token. Authenticate via Cognito.")
        try:
            # Signature verification does blocking I/O (JWKS fetch) only on a
            # cache miss; run it in a worker thread so the event loop is free.
            import anyio

            await anyio.to_thread.run_sync(_verify_token, token, cfg, jwks)
        except _AuthError as exc:
            return _unauthorized(str(exc))
        except Exception as exc:  # noqa: BLE001 - never leak internals as 500 here
            return _unauthorized(f"token verification failed: {exc}")
        return await call_next(request)

    return True
