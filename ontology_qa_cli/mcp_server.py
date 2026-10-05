"""Remote MCP server exposing one free-form question-answering tool per ontology.

Speaks the Model Context Protocol over the **Streamable HTTP** transport: a
single ``POST /mcp`` endpoint accepting JSON-RPC 2.0 requests (initialize,
tools/list, tools/call). This shape sits cleanly behind an ALB -- one path, no
long-lived socket required for the request/response tools here -- and reuses the
project's existing FastAPI/uvicorn stack (the ``api`` extra).

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
from pathlib import Path
from typing import Any, Optional

from ontology_qa_cli.config import load_config, resolve_ontology
from ontology_qa_cli.query import find_best_program
from ontology_qa_cli.serve import OntologyAnswerer

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
except ImportError as exc:  # pragma: no cover - only without the api extra
    raise ImportError(
        "FastAPI is required for the MCP server. Install with "
        "`uv sync --extra api`."
    ) from exc


PROTOCOL_VERSION = "2025-06-18"  # MCP revision this server implements
SERVER_NAME = "ontology-subsumption-mcp"
SERVER_VERSION = "0.1.0"

# JSON-RPC 2.0 error codes
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603

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


def _tool_definitions(ontologies: list[str]) -> list[dict[str, Any]]:
    return [
        {
            "name": tool_name_for(onto),
            "description": _tool_description(onto),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": _question_param_description(onto),
                    },
                },
                "required": ["question"],
            },
        }
        for onto in ontologies
    ]


def _format_answer(
    answerer: "OntologyAnswerer", ontology: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Answer a free-form question by running an ontology's evolved query program.

    Mirrors the web app: the OntologyAnswerer has loaded the ontology + the best
    evolved program once; here we run it for this one question and return the
    extracted ``Answer:`` text. A program that produces no ``Answer:`` line
    yields ``answered: false`` rather than a fabricated reply.
    """
    question = (args or {}).get("question", "")
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


def create_mcp_app(
    config_path: Optional[Path] = None,
    ontology: Optional[str] = None,
    ontologies: Optional[str] = None,
) -> "FastAPI":
    """Build the MCP server app, exposing one answer tool per configured ontology.

    ``ontology`` (singular) and ``ontologies`` (comma-separated) are both
    accepted; ``ontologies`` wins if both are given. When neither is passed the
    ``MCP_ONTOLOGIES`` / ``MCP_ONTOLOGY`` environment variables are consulted.
    """
    config_path = Path(
        config_path or os.environ.get("ONTOLOGY_CONFIG", "ontology_config.yaml")
    ).expanduser()
    onto_list = _parse_ontologies(ontologies if ontologies is not None else ontology)

    app = FastAPI(title=SERVER_NAME, version=SERVER_VERSION)
    runs_dir = Path(
        os.environ.get("ONTOLOGY_RUNS_DIR", "runs/")
    ).expanduser()
    # One lazily-populated answerer slot per configured ontology.
    answerers: dict[str, Optional[OntologyAnswerer]] = {o: None for o in onto_list}
    # Tool name -> ontology name, for O(1) dispatch.
    tool_to_ontology: dict[str, str] = {tool_name_for(o): o for o in onto_list}
    state: dict[str, Any] = {
        "answerers": answerers,
        "ontologies": onto_list,
        "runs_dir": runs_dir,
    }

    def _get_answerer(ontology_name: str) -> "OntologyAnswerer":
        """Load (once) the evolved query program + ontology for one ontology.

        Mirrors the web app: resolve the ontology's OWL (materializing it from
        S3 if needed) and the best already-built query program from runs/, then
        construct an OntologyAnswerer that loads both once. The costly builder
        is NOT run here -- it writes query_program.py offline to the shared
        runs/ mount; this just loads its output. Raises if no program exists.
        Each ontology loads into its own private World, so several can be served
        from one process without colliding.
        """
        if answerers[ontology_name] is None:
            cfg = load_config(config_path)
            entry = resolve_ontology(cfg, ontology_name)  # materializes S3 -> local cache
            program_path = find_best_program(ontology_name, state["runs_dir"])
            answerers[ontology_name] = OntologyAnswerer.from_program(
                program_path, entry.owl_file
            )
        return answerers[ontology_name]  # type: ignore[return-value]

    import json as _json

    def _result(id_: Any, result: Any) -> dict:
        return {"jsonrpc": "2.0", "id": id_, "result": result}

    def _error(id_: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}

    def _dispatch(msg: dict) -> Optional[dict]:
        """Handle one JSON-RPC message; return a response (None for notifications)."""
        if msg.get("jsonrpc") != "2.0" or "method" not in msg:
            return _error(msg.get("id"), _INVALID_REQUEST, "Invalid JSON-RPC 2.0 request")
        method = msg["method"]
        id_ = msg.get("id")
        is_notification = "id" not in msg

        if method == "initialize":
            return _result(id_, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        if method in ("notifications/initialized", "initialized"):
            return None  # notification, no response
        if method == "ping":
            return _result(id_, {})
        if method == "tools/list":
            return _result(id_, {"tools": _tool_definitions(state["ontologies"])})
        if method == "tools/call":
            params = msg.get("params") or {}
            tool = params.get("name")
            args = params.get("arguments") or {}
            ontology_name = tool_to_ontology.get(tool)
            if ontology_name is not None:
                try:
                    payload = _format_answer(_get_answerer(ontology_name), ontology_name, args)
                    return _result(id_, {
                        "content": [{"type": "text", "text": _json.dumps(payload, indent=2)}],
                        "isError": False,
                    })
                except _ToolError as te:
                    return _result(id_, {
                        "content": [{"type": "text", "text": str(te)}],
                        "isError": True,
                    })
                except SystemExit as se:
                    # find_best_program calls sys.exit(1) when no built program
                    # exists for the ontology. Surface it as a tool error rather
                    # than letting it propagate and kill the worker.
                    return _result(id_, {
                        "content": [{"type": "text", "text": (
                            f"No query program has been built for '{ontology_name}' yet "
                            f"(looked in {state['runs_dir']}). Build one offline with "
                            f"the builder before this tool can answer. ({se})"
                        )}],
                        "isError": True,
                    })
                except Exception as exc:  # noqa: BLE001 - surface as tool error, not 500
                    return _result(id_, {
                        "content": [{"type": "text", "text": f"Internal tool error: {exc}"}],
                        "isError": True,
                    })
            return _error(id_, _INVALID_PARAMS, f"Unknown tool '{tool}'")
        if is_notification:
            return None
        return _error(id_, _METHOD_NOT_FOUND, f"Method not found: {method}")

    @app.post("/mcp")
    async def mcp_endpoint(request: Request):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(_error(None, _PARSE_ERROR, "Parse error"), status_code=400)
        # JSON-RPC batch (list) or single message.
        if isinstance(body, list):
            responses = [r for r in (_dispatch(m) for m in body) if r is not None]
            return JSONResponse(responses)
        resp = _dispatch(body)
        if resp is None:
            # Notification: 202 Accepted, no body (per MCP Streamable HTTP).
            return JSONResponse(None, status_code=202)
        return JSONResponse(resp)

    @app.get("/mcp/health")
    async def health():
        return {"status": "ok", "ontologies": state["ontologies"]}

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
