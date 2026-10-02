"""Remote MCP server exposing provable ontology subsumption as agent tools.

Speaks the Model Context Protocol over the **Streamable HTTP** transport: a
single ``POST /mcp`` endpoint accepting JSON-RPC 2.0 requests (initialize,
tools/list, tools/call). This shape sits cleanly behind an ALB -- one path, no
long-lived socket required for the request/response tools here -- and reuses the
project's existing FastAPI/uvicorn stack (the ``api`` extra).

The exposed tools do PURE DL reasoning via
:class:`ontology_qa_cli.subsumption.SubsumptionReasoner`; there is no LLM in the
answer path, so results are provably sound+complete given the ontology. The
calling agent phrases the question; this server computes the exact set.

The tools are ontology-neutral: the same server serves whichever ontology
``MCP_ONTOLOGY`` selects (Gene Ontology by default, but equally FMA or any other
configured entry) without code changes.

Tools
-----
* ``ontology_subtypes`` -- given a term name (e.g. "immune system process"),
  return ALL specific sub-types (transitive is-a descendants). Ambiguous names
  return candidate terms for the agent to disambiguate rather than a guess.
* ``ontology_resolve`` -- resolve a term name to its canonical id + label (or
  candidates), useful for disambiguation before a subtypes call.
* ``answer_question_with_GO`` -- answer a free-form natural-language question
  using the SAME evolved query program the web app runs. The program is built
  offline by the builder (``ontology-qa-build``) and persisted to the shared
  runs/ mount; this server loads the best GO program once (lazily, on first
  call) via :class:`ontology_qa_cli.serve.OntologyAnswerer` and runs it per
  question -- mirroring the web app's "build at startup, run per question" flow.
  Unlike the two subsumption tools, the answer path here DOES involve the
  evolved program (which may call an LLM); it is the conversational QA path,
  not the provable-reasoning path.

Config: ``MCP_ONTOLOGY`` selects which configured ontology to serve (default
"GO"); the ontology is loaded once at startup from the same
``ontology_config.yaml`` + shared S3/EFS path the rest of the app uses.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from ontology_qa_cli.config import load_config, resolve_ontology
from ontology_qa_cli.subsumption import SubsumptionReasoner
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


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "ontology_subtypes",
            "description": (
                "Return ALL specific sub-types of an ontology term (the "
                "complete, transitive is-a descendant set). The result is "
                "computed directly from the ontology's description-logic "
                "hierarchy and is provably complete and sound -- there is no "
                "language-model guessing in the answer. Example: concept="
                "'immune system process' returns every process the ontology "
                "classifies as a kind of immune system process. If the term "
                "name is ambiguous, candidate terms are returned instead of "
                "a guess; call again with a specific label or the term id."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "concept": {
                        "type": "string",
                        "description": (
                            "The ontology term name or id to find sub-types of, "
                            "e.g. 'immune system process' or 'GO:0002376'."
                        ),
                    },
                    "include_self": {
                        "type": "boolean",
                        "description": "Include the term itself in the result (default false).",
                        "default": False,
                    },
                },
                "required": ["concept"],
            },
        },
        {
            "name": "ontology_resolve",
            "description": (
                "Resolve an ontology term name to its canonical id + label, or "
                "return candidate terms when the name is ambiguous. Use to "
                "disambiguate before ontology_subtypes."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "concept": {"type": "string", "description": "Term name to resolve."},
                },
                "required": ["concept"],
            },
        },
        {
            "name": "answer_question_with_GO",
            "description": (
                "Answer a free-form natural-language question about the Gene "
                "Ontology (GO) using the project's evolved query program -- the "
                "same logic the web chatbot runs. Give a full question in plain "
                "English (e.g. 'A GO enrichment analysis shows significant "
                "enrichment for microtubule cytoskeleton organization in "
                "upregulated genes -- what does this mean practically?') and a "
                "prose answer is returned. The query program is built offline "
                "and loaded once; this call runs it against the pre-loaded "
                "ontology. Note: unlike ontology_subtypes/ontology_resolve, this "
                "path runs the evolved program (which may use an LLM), so it is "
                "the conversational QA path, not the provable-reasoning path. "
                "Use ontology_subtypes when you need a provably complete subtype "
                "set rather than an explanatory answer."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The natural-language question to answer about the functions, locations, and biological roles of genes and proteins across different organisms",
                    },
                },
                "required": ["question"],
            },
        },
    ]


def _format_subtypes(reasoner: SubsumptionReasoner, args: dict[str, Any]) -> dict[str, Any]:
    name = (args or {}).get("concept", "")
    if not isinstance(name, str) or not name.strip():
        raise _ToolError("'concept' must be a non-empty string")
    include_self = bool((args or {}).get("include_self", False))
    match = reasoner.resolve(name)
    if not match.is_unique:
        cands = [{"id": c.id, "label": c.label} for c in match.candidates]
        if not cands:
            return {
                "resolved": False,
                "reason": f"No ontology term matches '{name}'.",
                "candidates": [],
            }
        return {
            "resolved": False,
            "reason": (
                f"'{name}' is ambiguous; {len(cands)} candidate term(s). "
                "Call ontology_subtypes again with an exact label or id."
            ),
            "candidates": cands,
        }
    res = reasoner.descendants(match.resolved, include_self=include_self)
    return {
        "resolved": True,
        "concept": {"id": match.resolved.id, "label": match.resolved.label},
        "subtype_count": res.count,
        "subtypes": [{"id": c.id, "label": c.label} for c in res.descendants],
        "provenance": (
            "Complete transitive is-a descendant set from the ontology DL "
            "hierarchy (sound + complete; no LLM in the reasoning path)."
        ),
    }


def _format_resolve(reasoner: SubsumptionReasoner, args: dict[str, Any]) -> dict[str, Any]:
    name = (args or {}).get("concept", "")
    if not isinstance(name, str) or not name.strip():
        raise _ToolError("'concept' must be a non-empty string")
    match = reasoner.resolve(name)
    if match.is_unique:
        return {
            "resolved": True,
            "concept": {"id": match.resolved.id, "label": match.resolved.label},
        }
    return {
        "resolved": False,
        "candidates": [{"id": c.id, "label": c.label} for c in match.candidates],
    }


def _format_answer(answerer: "OntologyAnswerer", args: dict[str, Any]) -> dict[str, Any]:
    """Answer a free-form question by running the evolved query program.

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
            "Answered by the evolved GO query program (the same logic the web "
            "chatbot runs). This path may use an LLM; use ontology_subtypes for "
            "provable subtype sets."
        ),
    }


class _ToolError(Exception):
    """A tool-level error returned as an MCP tool result with isError=true."""


_TOOL_HANDLERS = {
    "ontology_subtypes": _format_subtypes,
    "ontology_resolve": _format_resolve,
}


def create_mcp_app(
    config_path: Optional[Path] = None,
    ontology: Optional[str] = None,
) -> "FastAPI":
    """Build the MCP server app, loading the configured ontology once."""
    config_path = Path(
        config_path or os.environ.get("ONTOLOGY_CONFIG", "ontology_config.yaml")
    ).expanduser()
    ontology = ontology or os.environ.get("MCP_ONTOLOGY", "GO")

    app = FastAPI(title=SERVER_NAME, version=SERVER_VERSION)
    runs_dir = Path(
        os.environ.get("ONTOLOGY_RUNS_DIR", "runs/")
    ).expanduser()
    state: dict[str, Any] = {
        "reasoner": None,
        "answerer": None,
        "ontology": ontology,
        "runs_dir": runs_dir,
    }

    def _get_reasoner() -> SubsumptionReasoner:
        if state["reasoner"] is None:
            cfg = load_config(config_path)
            entry = resolve_ontology(cfg, ontology)  # materializes S3 -> local cache
            state["reasoner"] = SubsumptionReasoner(entry.owl_file)
        return state["reasoner"]

    def _get_answerer() -> "OntologyAnswerer":
        """Load (once) the evolved query program + ontology for free-form QA.

        Mirrors the web app: resolve the ontology's OWL (materializing it from
        S3 if needed) and the best already-built query program from runs/, then
        construct an OntologyAnswerer that loads both once. The costly builder
        is NOT run here -- it writes query_program.py offline to the shared
        runs/ mount; this just loads its output. Raises if no program exists.
        """
        if state["answerer"] is None:
            cfg = load_config(config_path)
            entry = resolve_ontology(cfg, ontology)  # materializes S3 -> local cache
            program_path = find_best_program(ontology, state["runs_dir"])
            state["answerer"] = OntologyAnswerer.from_program(program_path, entry.owl_file)
        return state["answerer"]

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
            return _result(id_, {"tools": _tool_definitions()})
        if method == "tools/call":
            params = msg.get("params") or {}
            tool = params.get("name")
            args = params.get("arguments") or {}
            if tool == "answer_question_with_GO":
                try:
                    payload = _format_answer(_get_answerer(), args)
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
                            f"No query program has been built for '{ontology}' yet "
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
            handler = _TOOL_HANDLERS.get(tool)
            if handler is None:
                return _error(id_, _INVALID_PARAMS, f"Unknown tool '{tool}'")
            try:
                payload = handler(_get_reasoner(), args)
                return _result(id_, {
                    "content": [{"type": "text", "text": _json.dumps(payload, indent=2)}],
                    "isError": False,
                })
            except _ToolError as te:
                return _result(id_, {
                    "content": [{"type": "text", "text": str(te)}],
                    "isError": True,
                })
            except Exception as exc:  # noqa: BLE001 - surface as tool error, not 500
                return _result(id_, {
                    "content": [{"type": "text", "text": f"Internal tool error: {exc}"}],
                    "isError": True,
                })
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
        return {"status": "ok", "ontology": state["ontology"]}

    return app


def main() -> None:  # pragma: no cover - thin uvicorn launcher
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Ontology subsumption MCP server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--ontology", default=os.environ.get("MCP_ONTOLOGY", "GO"))
    args = parser.parse_args()
    uvicorn.run(
        create_mcp_app(ontology=args.ontology),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    main()
