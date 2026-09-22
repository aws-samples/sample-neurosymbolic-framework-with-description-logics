"""FastAPI HTTP wrapper around :class:`OntologyAnswerer`.

Exposes a single ``/ask`` endpoint backed by ONE answerer that loads the
ontology once at startup and answers questions in-process. This is the
chatbot-facing surface: point your bot's backend at ``POST /ask``.

Run it::

    # install the optional web stack first
    uv sync --extra api
    # then serve the best FMA program over HTTP
    ontology-qa-api FMA
    # or via uvicorn directly (configured through env vars, see below)
    ONTOLOGY_QA_SOURCE=FMA uvicorn ontology_qa_cli.api:app

Configuration when launched via ``uvicorn`` directly (rather than the
``ontology-qa-api`` CLI) is read from environment variables, because uvicorn
constructs the app itself and cannot pass constructor arguments:

    ONTOLOGY_QA_SOURCE     ontology name (e.g. "FMA") or path to query_program.py  [required]
    ONTOLOGY_QA_CONFIG     path to ontology_config.yaml   (default: ontology_config.yaml)
    ONTOLOGY_QA_RUNS_DIR   builder runs directory          (default: runs/)
    ONTOLOGY_QA_OWL_FILE   OWL file (only when SOURCE is a program path, multi-ontology config)

The answerer is not thread-safe (owlready2's world is process-global), so the
sync ``.answer()`` call is offloaded to a single-worker thread and calls are
serialized. For horizontal scale, run multiple single-worker processes behind a
load balancer rather than threads within one process.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from ontology_qa_cli.serve import OntologyAnswerer, _build_answerer

try:
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "FastAPI is not installed. Install the optional web stack with "
        "`uv sync --extra api` (or `pip install -e \".[api]\"`)."
    ) from exc


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="Natural language question.")


class AskResponse(BaseModel):
    question: str
    answer: Optional[str] = Field(
        None, description="Answer text, or null if the program produced no answer."
    )


def _answerer_from_env() -> OntologyAnswerer:
    """Build the answerer from ONTOLOGY_QA_* environment variables.

    Used when uvicorn imports ``app`` directly (no CLI args available).
    """
    source = os.environ.get("ONTOLOGY_QA_SOURCE")
    if not source:
        raise RuntimeError(
            "ONTOLOGY_QA_SOURCE is not set. Set it to an ontology name (e.g. 'FMA') "
            "or a path to a query_program.py, or launch via the `ontology-qa-api` CLI."
        )
    ns = argparse.Namespace(
        source=source,
        config=Path(os.environ.get("ONTOLOGY_QA_CONFIG", "ontology_config.yaml")),
        runs_dir=Path(os.environ.get("ONTOLOGY_QA_RUNS_DIR", "runs/")),
        owl_file=(
            Path(os.environ["ONTOLOGY_QA_OWL_FILE"])
            if os.environ.get("ONTOLOGY_QA_OWL_FILE")
            else None
        ),
    )
    return _build_answerer(ns)


def create_app(answerer: Optional[OntologyAnswerer] = None) -> "FastAPI":
    """Build the FastAPI app.

    Args:
        answerer: A pre-built answerer to serve. If omitted, one is constructed
            from ONTOLOGY_QA_* environment variables at startup (lifespan).
    """
    # Single worker: the answerer serializes anyway, and offloading keeps the
    # async event loop from blocking on the sync ontology query.
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="answerer")
    state: dict[str, Optional[OntologyAnswerer]] = {"answerer": answerer}

    @asynccontextmanager
    async def lifespan(_app: "FastAPI"):
        if state["answerer"] is None:
            state["answerer"] = _answerer_from_env()
        try:
            yield
        finally:
            pool.shutdown(wait=True)

    fastapi_app = FastAPI(
        title="Ontology QA",
        description="Answer natural-language questions against an OWL ontology "
        "using an evolved query program.",
        version="0.1.0",
        lifespan=lifespan,
    )

    @fastapi_app.get("/health")
    async def health() -> dict[str, object]:
        a = state["answerer"]
        return {
            "status": "ok" if a is not None else "loading",
            "program": str(a.program_path) if a else None,
            "ontology": str(a.onto_file) if a else None,
        }

    @fastapi_app.post("/ask", response_model=AskResponse)
    async def ask(req: AskRequest) -> AskResponse:
        a = state["answerer"]
        if a is None:  # pragma: no cover - lifespan populates this before serving
            raise HTTPException(status_code=503, detail="Answerer not ready")
        loop = asyncio.get_running_loop()
        try:
            answer = await loop.run_in_executor(pool, a.answer, req.question)
        except Exception as exc:  # noqa: BLE001 - surface program errors as 500
            raise HTTPException(
                status_code=500, detail=f"Query program failed: {exc}"
            ) from exc
        return AskResponse(question=req.question, answer=answer)

    return fastapi_app


# Module-level app for `uvicorn ontology_qa_cli.api:app` (env-var configured).
# The ontology load is deferred to lifespan startup, so importing this module
# stays cheap (help text, tests, reloaders).
app = create_app()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve an evolved query program over HTTP (POST /ask)."
    )
    parser.add_argument(
        "source",
        help="Ontology name (e.g. 'FMA') or path to a query_program.py file.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("ontology_config.yaml"),
        help="Path to the YAML configuration file (default: ontology_config.yaml).",
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=Path("runs/"),
        help="Base directory where builder runs are stored (default: runs/).",
    )
    parser.add_argument(
        "--owl-file",
        type=Path,
        default=None,
        help="OWL file to use when source is a program path (overrides config lookup).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to listen on (default: 8000).",
    )
    return parser.parse_args()


def main() -> None:
    """CLI entry point: load the answerer, then run uvicorn."""
    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn is not installed. Install the optional web stack with "
            '`uv sync --extra api` (or `pip install -e ".[api]"`).',
            file=sys.stderr,
        )
        sys.exit(1)

    args = _parse_args()
    print("Loading ontology (once)...", file=sys.stderr)
    answerer = _build_answerer(args)
    print(f"Ready. Serving POST /ask on http://{args.host}:{args.port}", file=sys.stderr)
    uvicorn.run(create_app(answerer), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
