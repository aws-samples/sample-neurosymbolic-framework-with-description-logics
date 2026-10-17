"""Tests for application-level Cognito JWT auth (ontology_qa_cli.auth).

Mints RS256 tokens with a locally-generated RSA key and monkeypatches the JWKS
key resolver so no network is touched. Covers:
  * auth DISABLED by default (no COGNITO_USER_POOL_ID) -> app serves open,
  * a valid access/id token is accepted,
  * missing / expired / wrong-audience / wrong-issuer tokens are 401,
  * the ALB health-check paths stay public even with auth enabled,
  * both the web app and the MCP app enforce it.
Skipped if FastAPI or PyJWT (crypto) are absent.
"""
from __future__ import annotations

import json
import textwrap
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi.testclient")
pytest.importorskip("jwt")
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
import jwt as pyjwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import ontology_qa_cli.auth as auth  # noqa: E402
import ontology_qa_cli.webapp as webapp  # noqa: E402
import ontology_qa_cli.mcp_server as mcp_server  # noqa: E402


POOL_ID = "us-east-1_testpool"
REGION = "us-east-1"
CLIENT_ID = "test-client-id"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL_ID}"
KID = "test-kid-1"


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _mint(
    rsa_key,
    *,
    token_use: str = "access",
    aud: str | None = None,
    client_id: str | None = CLIENT_ID,
    issuer: str = ISSUER,
    exp_delta: int = 300,
    kid: str = KID,
) -> str:
    now = int(time.time())
    claims: dict = {
        "sub": "user-123",
        "iss": issuer,
        "token_use": token_use,
        "iat": now,
        "exp": now + exp_delta,
    }
    if token_use == "id" and aud is not None:
        claims["aud"] = aud
    if token_use == "access" and client_id is not None:
        claims["client_id"] = client_id
    priv_pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pyjwt.encode(claims, priv_pem, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def cognito_env(monkeypatch, rsa_key):
    """Enable auth and make JWKS resolution return our local public key."""
    monkeypatch.setenv("COGNITO_USER_POOL_ID", POOL_ID)
    monkeypatch.setenv("COGNITO_REGION", REGION)
    monkeypatch.setenv("COGNITO_CLIENT_ID", CLIENT_ID)
    monkeypatch.delenv("COGNITO_AUTH_DISABLED", raising=False)

    pub = rsa_key.public_key()

    def _fake_get(_jwks, kid):
        return pub if kid == KID else None

    monkeypatch.setattr(auth, "jwts_get", _fake_get)
    return rsa_key


# --------------------------------------------------------------------------- #
# MCP app — small and self-contained, used for most auth assertions.
#
# The MCP endpoint now speaks the SDK's Streamable HTTP transport, so a request
# that is meant to PASS auth must be a valid MCP request (initialize, with the
# Accept: application/json, text/event-stream header) and the TestClient must be
# used as a context manager so the app lifespan starts the SDK session manager.
# Auth is enforced by middleware BEFORE the SDK mount, so the 401-vs-not-401
# decision these tests care about is made by the middleware regardless.
# --------------------------------------------------------------------------- #
_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    },
}


def _mcp_app(tmp_path: Path):
    cfg = tmp_path / "ontology_config.yaml"
    cfg.write_text(
        textwrap.dedent(
            """\
            ontologies:
              - name: "GO"
                owl_file: "/nonexistent.owl"
                questions:
                  - question: "q?"
                    expected_answer: "a"
            """
        )
    )
    return mcp_server.create_mcp_app(config_path=cfg, ontologies="GO")


def _post_initialize(client: TestClient, *, headers: dict | None = None):
    h = dict(_MCP_HEADERS)
    if headers:
        h.update(headers)
    return client.post("/mcp", json=_INITIALIZE, headers=h)


def test_auth_disabled_by_default(tmp_path, monkeypatch):
    """With no COGNITO_USER_POOL_ID, the MCP endpoint answers unauthenticated."""
    monkeypatch.delenv("COGNITO_USER_POOL_ID", raising=False)
    monkeypatch.delenv("COGNITO_AUTH_DISABLED", raising=False)
    with TestClient(_mcp_app(tmp_path)) as client:
        r = _post_initialize(client)
        assert r.status_code == 200


def test_explicit_disable_overrides_pool(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNITO_USER_POOL_ID", POOL_ID)
    monkeypatch.setenv("COGNITO_AUTH_DISABLED", "true")
    with TestClient(_mcp_app(tmp_path)) as client:
        r = _post_initialize(client)
        assert r.status_code == 200


def test_missing_token_is_401(tmp_path, cognito_env):
    with TestClient(_mcp_app(tmp_path)) as client:
        r = _post_initialize(client)
        assert r.status_code == 401
        # RFC 9728: the challenge advertises the Protected Resource Metadata URL
        # so an RFC-9728-aware client can discover the authorization server.
        www = r.headers.get("www-authenticate", "")
        assert www.startswith("Bearer")
        assert "resource_metadata=" in www
        assert "/.well-known/oauth-protected-resource" in www


def test_resource_metadata_is_public_and_points_at_cognito(tmp_path, cognito_env):
    """RFC 9728 doc is reachable without a token and names the Cognito issuer."""
    with TestClient(_mcp_app(tmp_path)) as client:
        r = client.get("/.well-known/oauth-protected-resource")
        assert r.status_code == 200
        doc = r.json()
        assert doc["authorization_servers"] == [ISSUER]
        assert "neurosymbolic-mcp/mcp.invoke" in doc["scopes_supported"]


def test_challenge_uses_https_behind_forwarded_proto(tmp_path, cognito_env):
    """The 401 resource_metadata URL is https when X-Forwarded-Proto says so.

    Behind CloudFront/ALB the container is reached over plain HTTP, so
    request.base_url is http://; the challenge must still advertise an https
    metadata URL (strict RFC-9728 clients reject a non-TLS metadata URL).
    """
    with TestClient(_mcp_app(tmp_path)) as client:
        r = _post_initialize(client, headers={"X-Forwarded-Proto": "https"})
        assert r.status_code == 401
        www = r.headers.get("www-authenticate", "")
        assert 'resource_metadata="https://' in www
        assert 'resource_metadata="http://' not in www


def test_valid_access_token_accepted(tmp_path, cognito_env):
    with TestClient(_mcp_app(tmp_path)) as client:
        token = _mint(cognito_env, token_use="access", client_id=CLIENT_ID)
        r = _post_initialize(client, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200


def test_valid_id_token_accepted(tmp_path, cognito_env):
    with TestClient(_mcp_app(tmp_path)) as client:
        token = _mint(cognito_env, token_use="id", aud=CLIENT_ID)
        r = _post_initialize(client, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200


def test_expired_token_is_401(tmp_path, cognito_env):
    with TestClient(_mcp_app(tmp_path)) as client:
        token = _mint(cognito_env, exp_delta=-10)  # already expired
        r = _post_initialize(client, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401


def test_wrong_audience_is_401(tmp_path, cognito_env):
    with TestClient(_mcp_app(tmp_path)) as client:
        token = _mint(cognito_env, token_use="access", client_id="some-other-client")
        r = _post_initialize(client, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401


def test_wrong_issuer_is_401(tmp_path, cognito_env):
    with TestClient(_mcp_app(tmp_path)) as client:
        token = _mint(cognito_env, issuer="https://evil.example.com/pool")
        r = _post_initialize(client, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401


def test_unknown_kid_is_401(tmp_path, cognito_env):
    with TestClient(_mcp_app(tmp_path)) as client:
        token = _mint(cognito_env, kid="unknown-kid")
        r = _post_initialize(client, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401


def test_malformed_bearer_is_401(tmp_path, cognito_env):
    with TestClient(_mcp_app(tmp_path)) as client:
        r = _post_initialize(client, headers={"Authorization": "Bearer not-a-jwt"})
        assert r.status_code == 401


def test_mcp_health_is_public_even_with_auth(tmp_path, cognito_env):
    """The ALB health-check path must stay reachable without a token."""
    with TestClient(_mcp_app(tmp_path)) as client:
        r = client.get("/mcp/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


# --------------------------------------------------------------------------- #
# Web app — confirm the same enforcement wraps the SPA/API surface, and that
# /api/ontologies (its ALB health check) stays public.
# --------------------------------------------------------------------------- #
TINY_OWL = (
    '<?xml version="1.0"?>'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
    'xmlns:owl="http://www.w3.org/2002/07/owl#" '
    'xmlns="http://example.org/t#" xml:base="http://example.org/t">'
    '<owl:Ontology rdf:about="http://example.org/t"/>'
    '<owl:Class rdf:about="http://example.org/t#Heart"/></rdf:RDF>'
)


def _web_client(tmp_path: Path) -> TestClient:
    owl = tmp_path / "tiny.owl"
    owl.write_text(TINY_OWL)
    cfg = tmp_path / "ontology_config.yaml"
    cfg.write_text(
        textwrap.dedent(
            f"""\
            ontologies:
              - name: "TINY"
                owl_file: "{owl}"
                questions:
                  - question: "q?"
                    expected_answer: "a"
            """
        )
    )
    runs = tmp_path / "runs"
    runs.mkdir()
    app = webapp.create_app(config_path=cfg, runs_dir=runs, resolve_followups=False)
    return TestClient(app)


def test_web_ontologies_health_public(tmp_path, cognito_env):
    client = _web_client(tmp_path)
    r = client.get("/api/ontologies")  # ALB health check path
    assert r.status_code == 200


def test_web_select_requires_token(tmp_path, cognito_env):
    client = _web_client(tmp_path)
    r = client.post("/api/select", json={"ontology": "TINY"})
    assert r.status_code == 401


def test_web_select_accepts_valid_token(tmp_path, cognito_env):
    client = _web_client(tmp_path)
    token = _mint(cognito_env, token_use="access")
    # TINY has no built program, so a valid-token select returns 200 "building"
    # (or ready) rather than 401 -- the point is auth passed, not the outcome.
    r = client.post(
        "/api/select",
        json={"ontology": "TINY"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code != 401


# --------------------------------------------------------------------------- #
# Config resolution unit checks (no app).
# --------------------------------------------------------------------------- #
def test_region_derived_from_pool_id(monkeypatch):
    monkeypatch.setenv("COGNITO_USER_POOL_ID", "eu-west-2_xyz")
    monkeypatch.delenv("COGNITO_REGION", raising=False)
    monkeypatch.delenv("COGNITO_AUTH_DISABLED", raising=False)
    cfg = auth.CognitoConfig.from_env()
    assert cfg is not None
    assert cfg.region == "eu-west-2"
    assert cfg.issuer == "https://cognito-idp.eu-west-2.amazonaws.com/eu-west-2_xyz"


def test_from_env_none_when_unconfigured(monkeypatch):
    monkeypatch.delenv("COGNITO_USER_POOL_ID", raising=False)
    monkeypatch.delenv("COGNITO_AUTH_DISABLED", raising=False)
    assert auth.CognitoConfig.from_env() is None


# --------------------------------------------------------------------------- #
# Multiple allowed client ids (SPA + confidential) — COGNITO_CLIENT_ID is a list.
# --------------------------------------------------------------------------- #
def test_multiple_client_ids_parsed(monkeypatch):
    monkeypatch.setenv("COGNITO_USER_POOL_ID", POOL_ID)
    monkeypatch.setenv("COGNITO_REGION", REGION)
    monkeypatch.setenv("COGNITO_CLIENT_ID", "aaa,bbb ccc")  # comma AND whitespace
    monkeypatch.delenv("COGNITO_AUTH_DISABLED", raising=False)
    cfg = auth.CognitoConfig.from_env()
    assert cfg is not None
    assert cfg.client_ids == frozenset({"aaa", "bbb", "ccc"})


def test_token_from_second_client_id_accepted(tmp_path, monkeypatch, rsa_key):
    """A token whose audience is the SECOND configured client id validates."""
    monkeypatch.setenv("COGNITO_USER_POOL_ID", POOL_ID)
    monkeypatch.setenv("COGNITO_REGION", REGION)
    monkeypatch.setenv("COGNITO_CLIENT_ID", f"{CLIENT_ID},spa-client-xyz")
    monkeypatch.delenv("COGNITO_AUTH_DISABLED", raising=False)
    pub = rsa_key.public_key()
    monkeypatch.setattr(auth, "jwts_get", lambda _j, kid: pub if kid == KID else None)
    with TestClient(_mcp_app(tmp_path)) as client:
        token = _mint(rsa_key, token_use="id", aud="spa-client-xyz")
        r = _post_initialize(client, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code != 401


# --------------------------------------------------------------------------- #
# SSE token via query param — EventSource cannot set an Authorization header.
# --------------------------------------------------------------------------- #
def test_token_via_query_param_on_get(tmp_path, cognito_env):
    """A GET carrying `?access_token=` validates (the EventSource/SSE path)."""
    client = _web_client(tmp_path)
    token = _mint(cognito_env, token_use="id", aud=CLIENT_ID)
    # A gated GET with no header but a valid query-param token must NOT 401.
    r = client.get(f"/api/program/TINY?access_token={token}")
    assert r.status_code != 401


def test_query_param_token_ignored_on_post(tmp_path, cognito_env):
    """A POST may not authenticate via the query param (header-only for POST)."""
    client = _web_client(tmp_path)
    token = _mint(cognito_env, token_use="id", aud=CLIENT_ID)
    r = client.post(f"/api/select?access_token={token}", json={"ontology": "TINY"})
    assert r.status_code == 401
