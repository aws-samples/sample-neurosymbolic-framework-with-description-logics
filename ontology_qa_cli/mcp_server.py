"""Remote MCP server exposing one free-form question-answering tool per ontology.

Speaks the Model Context Protocol over the **Streamable HTTP** transport using
the official MCP Python SDK (``mcp.server.fastmcp.FastMCP``). The SDK's
streamable-HTTP ASGI app is **mounted at ``POST /mcp``** inside this module's
FastAPI app, so the external contract is unchanged from the previous
hand-rolled implementation: a single ``/mcp`` endpoint speaking JSON-RPC 2.0
(initialize, tools/list, tools/call).

Why the SDK, and in *stateless JSON* mode
-----------------------------------------
``FastMCP(stateless_http=True, json_response=True)`` makes every ``POST /mcp``
a self-contained request answered with a single ``application/json`` body: no
``Mcp-Session-Id``, no server-initiated SSE stream, no per-session state pinned
to one worker. That is the mode that sits cleanly behind the CloudFront -> ALB
-> *multi-task* Fargate topology this service runs on -- any task can serve any
request, so no sticky-session routing is required. (The SDK's default stateful
mode would pin a session to the worker that created it, which breaks across
tasks without sticky sessions.) It also matches what Amazon Quick Suite's MCP
client speaks: it prefers Streamable HTTP and does not need the SSE stream.

Tools
-----
One tool per configured ontology, named ``answer_question_with_<NAME>`` (e.g.
``answer_question_with_GO``, ``answer_question_with_CTO``). Each answers a
free-form natural-language question using the SAME evolved query program the web
app runs for that ontology. Programs are built offline by the builder
(``ontology-qa-build``) and persisted to the shared runs/ mount; this server
loads the best program for each ontology once (lazily, on the first call to that
ontology's tool) via :class:`ontology_qa_cli.serve.OntologyAnswerer` and runs it
per question -- mirroring the web app's "build offline, run per question" flow.
The answer path runs the evolved program (which may call an LLM); it is the
conversational QA path, not a provable-reasoning path.

Each ontology's answerer loads into its OWN private owlready2 World (see
``OntologyAnswerer``), so serving several ontologies from one process keeps them
isolated -- no shared-world quadstore collision.

Auth and discovery (for Amazon Quick Suite 2LO)
-----------------------------------------------
``POST /mcp`` is gated by the application-level Cognito JWT middleware
(``ontology_qa_cli.auth.install_cognito_auth``); ``GET /mcp/health`` stays
public for the ALB target-group probe. When a pool is configured, the server
also publishes **OAuth 2.0 Protected Resource Metadata (RFC 9728)** at
``GET /.well-known/oauth-protected-resource`` and points the 401
``WWW-Authenticate`` header at it, so an RFC-9728-aware client (Amazon Quick
Suite) can auto-discover the Cognito authorization server. A client that does
not discover (Quick's "Service authentication" / 2LO form) is configured
manually with the Cognito token URL + the M2M client id/secret; see
``docs/quick-mcp-integration.md``.

Config
------
``MCP_ONTOLOGIES`` (comma-separated, e.g. "GO,CTO") selects which configured
ontologies to serve; default "GO". The legacy singular ``MCP_ONTOLOGY`` is still
honored as an alias when ``MCP_ONTOLOGIES`` is unset. Ontologies are loaded
lazily (per tool, on first call) from the same ``ontology_config.yaml`` +
shared S3/EFS path the rest of the app uses.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from ontology_qa_cli.config import load_config, resolve_ontology
from ontology_qa_cli.query import find_best_program
from ontology_qa_cli.serve import OntologyAnswerer
from ontology_qa_cli.auth import CognitoConfig, install_cognito_auth

try:
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
except ImportError as exc:  # pragma: no cover - only without the api extra
    raise ImportError(
        "FastAPI is required for the MCP server. Install with "
        "`uv sync --extra api`."
    ) from exc

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings
    from mcp.types import TextContent
except ImportError as exc:  # pragma: no cover - only without the api extra
    raise ImportError(
        "The MCP SDK is required for the MCP server. Install with "
        "`uv sync --extra api` (it is in the `api` optional-dependency group)."
    ) from exc


PROTOCOL_VERSION = "2025-06-18"  # MCP revision the SDK negotiates
SERVER_NAME = "ontology-subsumption-mcp"
SERVER_VERSION = "0.1.0"

# Per-ontology descriptions shown in tools/list. An ontology without an explicit
# entry here falls back to the generic template in ``_tool_description``.
_TOOL_DESCRIPTIONS: dict[str, str] = {
    "GO": (
        "Answer a free-form natural-language question about the functions, "
        "locations, and biological roles of genes and proteins across different "
        "organisms. Give a full question in plain English (e.g. 'A GO enrichment "
        "analysis shows significant enrichment for microtubule cytoskeleton "
        "organization in upregulated genes -- what does this mean practically?') "
        "and a prose answer is returned."
    ),
    "CTO": (
        "Answer a free-form natural-language question about clinical trials -- "
        "their design, phases, eligibility criteria, masking/blinding, "
        "allocation, outcome measures, sponsors, and registries. Give a full "
        "question in plain English (e.g. 'A trial is a randomized, double-blind, "
        "placebo-controlled phase 3 study -- what does that design mean "
        "practically and what can it establish?') and a prose answer is "
        "returned."
    ),
}

_QUESTION_PARAM_DESCRIPTIONS: dict[str, str] = {
    "GO": (
        "The natural-language question to answer about the functions, "
        "locations, and biological roles of genes and proteins across different "
        "organisms"
    ),
    "CTO": (
        "The natural-language question to answer about clinical trials -- their "
        "design, phases, eligibility, masking, allocation, outcomes, sponsors, "
        "and registries"
    ),
}


def tool_name_for(ontology: str) -> str:
    """The MCP tool name exposed for a configured ontology."""
    return f"answer_question_with_{ontology}"


def _tool_description(ontology: str) -> str:
    return _TOOL_DESCRIPTIONS.get(
        ontology,
        f"Answer a free-form natural-language question about the {ontology} "
        f"ontology. Give a full question in plain English and a prose answer is "
        f"returned.",
    )


def _question_param_description(ontology: str) -> str:
    return _QUESTION_PARAM_DESCRIPTIONS.get(
        ontology, f"The natural-language question to answer about {ontology}"
    )


def _format_answer(
    answerer: "OntologyAnswerer", ontology: str, question: str
) -> dict[str, Any]:
    """Answer a free-form question by running an ontology's evolved query program.

    Mirrors the web app: the OntologyAnswerer has loaded the ontology + the best
    evolved program once; here we run it for this one question and return the
    extracted ``Answer:`` text. A program that produces no ``Answer:`` line
    yields ``answered: false`` rather than a fabricated reply.
    """
    if not isinstance(question, str) or not question.strip():
        raise _ToolError("'question' must be a non-empty string")
    text = answerer.answer(question)
    if text is None:
        return {
            "answered": False,
            "reason": (
                "The query program ran but produced no answer in the expected "
                "format. The question may be outside what the program handles."
            ),
        }
    return {
        "answered": True,
        "question": question,
        "answer": text,
        "provenance": (
            f"Answered by the evolved {ontology} query program (the same logic "
            f"the web chatbot runs). This path may use an LLM."
        ),
    }


class _ToolError(Exception):
    """A tool-level error returned as an MCP tool result with isError=true."""


def _parse_ontologies(ontologies: Optional[str]) -> list[str]:
    """Normalize the configured ontology list.

    Accepts a comma-separated string (``MCP_ONTOLOGIES``), honoring the legacy
    singular ``MCP_ONTOLOGY`` alias. Preserves order, drops blanks/dupes.
    """
    raw = ontologies
    if raw is None:
        raw = os.environ.get("MCP_ONTOLOGIES") or os.environ.get("MCP_ONTOLOGY") or "GO"
    seen: set[str] = set()
    result: list[str] = []
    for part in raw.split(","):
        name = part.strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return result or ["GO"]


def _build_fastmcp(
    onto_list: list[str],
    config_path: Path,
    runs_dir: Path,
) -> "FastMCP":
    """Construct the FastMCP server with one answer tool per ontology.

    Stateless JSON mode: each POST /mcp is self-contained and answered with a
    single JSON body (no session id, no SSE), which keeps the service free of
    sticky-session requirements behind the ALB. The streamable-HTTP route is
    served at ``/mcp`` directly (and the ASGI app is mounted at the FastAPI
    app's root), so the external path is exactly ``POST /mcp``.

    DNS-rebinding (Host/Origin allowlist) protection is DISABLED: it targets
    browser-based localhost attacks, and this endpoint is a token-gated
    server-to-server surface behind CloudFront + ALB, where the Host header is
    the public CloudFront/ALB domain (not an allowlistable fixed value) and the
    application-level Cognito JWT is the real auth gate. Leaving it on would 421
    every real request whose Host is not pre-registered.
    """
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    mcp = FastMCP(
        name=SERVER_NAME,
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        transport_security=security,
    )

    # One lazily-populated answerer slot per configured ontology.
    answerers: dict[str, Optional[OntologyAnswerer]] = {o: None for o in onto_list}

    def _get_answerer(ontology_name: str) -> "OntologyAnswerer":
        """Load (once) the evolved query program + ontology for one ontology.

        Mirrors the web app: resolve the ontology's OWL (materializing it from
        S3 if needed) and the best already-built query program from runs/, then
        construct an OntologyAnswerer that loads both once. The costly builder
        is NOT run here. Each ontology loads into its own private World.
        """
        if answerers[ontology_name] is None:
            cfg = load_config(config_path)
            entry = resolve_ontology(cfg, ontology_name)  # materializes S3 -> local cache
            program_path = find_best_program(ontology_name, runs_dir)
            answerers[ontology_name] = OntologyAnswerer.from_program(
                program_path, entry.owl_file
            )
        return answerers[ontology_name]  # type: ignore[return-value]

    import json as _json

    def _make_tool(ontology_name: str):
        """Build the async tool callable for one ontology.

        Bound to ``ontology_name`` via a factory so each registered tool closes
        over its own ontology. Returns an MCP tool *result* shape: a list of
        content blocks. The ``_format_answer`` payload is serialized to the same
        indented JSON text block the previous implementation returned, so the
        tool-result contract is byte-for-byte compatible.
        """

        async def _answer(question: str) -> list["TextContent"]:
            try:
                payload = _format_answer(
                    _get_answerer(ontology_name), ontology_name, question
                )
                return [TextContent(type="text", text=_json.dumps(payload, indent=2))]
            except _ToolError as te:
                # A bad/empty question: surface as an error tool result.
                raise ValueError(str(te)) from te
            except SystemExit as se:
                # find_best_program calls sys.exit(1) when no program is built.
                # Surface as a tool error rather than crashing the worker.
                raise ValueError(
                    f"No query program has been built for '{ontology_name}' yet "
                    f"(looked in {runs_dir}). Build one offline with the builder "
                    f"before this tool can answer. ({se})"
                ) from se

        _answer.__name__ = tool_name_for(ontology_name)
        return _answer

    for onto in onto_list:
        mcp.add_tool(
            _make_tool(onto),
            name=tool_name_for(onto),
            description=_tool_description(onto),
        )

    return mcp


def _resource_metadata(cfg: "CognitoConfig", mcp_url: Optional[str]) -> dict[str, Any]:
    """RFC 9728 Protected Resource Metadata document for this MCP endpoint.

    Points an RFC-9728-aware client (Amazon Quick Suite) at the Cognito user
    pool as the authorization server. The pool's OpenID/OAuth metadata lives at
    ``<issuer>/.well-known/openid-configuration`` (``issuer`` is the pool URL),
    from which the client reads the token/authorization/registration endpoints.
    """
    doc: dict[str, Any] = {
        # The protected resource identifier. Best effort: the public /mcp URL
        # when known (set MCP_PUBLIC_URL), else the issuer as a stable fallback.
        "resource": mcp_url or cfg.issuer,
        "authorization_servers": [cfg.issuer],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["neurosymbolic-mcp/mcp.invoke"],
    }
    return doc


def create_mcp_app(
    config_path: Optional[Path] = None,
    ontology: Optional[str] = None,
    ontologies: Optional[str] = None,
) -> "FastAPI":
    """Build the MCP server app, exposing one answer tool per configured ontology.

    ``ontology`` (singular) and ``ontologies`` (comma-separated) are both
    accepted; ``ontologies`` wins if both are given. When neither is passed the
    ``MCP_ONTOLOGIES`` / ``MCP_ONTOLOGY`` environment variables are consulted.

    The returned FastAPI app:
      * mounts the SDK streamable-HTTP transport at ``POST /mcp`` (stateless JSON),
      * keeps ``GET /mcp/health`` public (ALB target-group probe),
      * publishes RFC 9728 Protected Resource Metadata at
        ``GET /.well-known/oauth-protected-resource`` when Cognito is configured,
      * gates everything else behind the Cognito JWT middleware.
    """
    config_path = Path(
        config_path or os.environ.get("ONTOLOGY_CONFIG", "ontology_config.yaml")
    ).expanduser()
    onto_list = _parse_ontologies(ontologies if ontologies is not None else ontology)
    runs_dir = Path(os.environ.get("ONTOLOGY_RUNS_DIR", "runs/")).expanduser()

    mcp = _build_fastmcp(onto_list, config_path, runs_dir)
    mcp_asgi = mcp.streamable_http_app()  # Starlette app serving POST / (its root)

    # The SDK's streamable-HTTP session manager must be started/stopped with the
    # app lifespan. Chain it into the FastAPI app's lifespan so uvicorn drives it.
    @asynccontextmanager
    async def lifespan(_app: "FastAPI"):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title=SERVER_NAME, version=SERVER_VERSION, lifespan=lifespan)

    cfg = CognitoConfig.from_env()

    # RFC 9728 Protected Resource Metadata + a WWW-Authenticate hint, so an
    # RFC-9728-aware MCP client (Amazon Quick Suite) auto-discovers the Cognito
    # authorization server. Only meaningful when auth is configured. Declared
    # BEFORE install_cognito_auth so it joins the public-paths set.
    well_known_path = "/.well-known/oauth-protected-resource"
    public_paths = {"/mcp/health", well_known_path}
    # The known public origin (e.g. https://d123.cloudfront.net/mcp). Used both
    # as the RFC 9728 `resource` identifier and to pin the scheme+host of the
    # metadata URL advertised in the 401 challenge, independent of forwarded
    # proxy headers.
    mcp_public_url = os.environ.get("MCP_PUBLIC_URL")

    if cfg is not None:

        @app.get(well_known_path)
        async def oauth_protected_resource() -> JSONResponse:
            return JSONResponse(_resource_metadata(cfg, mcp_public_url))

    # Application-level Cognito JWT auth gating POST /mcp. No-op unless
    # COGNITO_USER_POOL_ID is configured; the CDK compute stack injects it. The
    # health check and the discovery document stay public. When auth is enabled
    # its 401s carry a WWW-Authenticate header pointing at the discovery doc.
    install_cognito_auth(
        app,
        public_paths=public_paths,
        resource_metadata_path=well_known_path if cfg is not None else None,
        public_base_url=mcp_public_url,
    )

    @app.get("/mcp/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "ontologies": onto_list}

    # Mount the SDK transport LAST, at the app ROOT. Its own route is "/mcp"
    # (set via streamable_http_path above), so the external path is exactly
    # POST /mcp -- no trailing-slash redirect (which a Mount at "/mcp" would
    # introduce). FastAPI's own routes (/mcp/health, the discovery doc) are
    # registered first and take precedence over the catch-all mount.
    app.mount("/", mcp_asgi)

    return app


def main() -> None:  # pragma: no cover - thin uvicorn launcher
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Ontology question-answering MCP server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument(
        "--ontologies",
        default=os.environ.get("MCP_ONTOLOGIES") or os.environ.get("MCP_ONTOLOGY"),
        help="Comma-separated ontologies to serve (e.g. 'GO,CTO'). Default: GO.",
    )
    args = parser.parse_args()
    uvicorn.run(
        create_mcp_app(ontologies=args.ontologies),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    main()
