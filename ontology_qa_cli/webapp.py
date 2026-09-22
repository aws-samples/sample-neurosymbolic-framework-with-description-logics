"""Web chatbot backend — the browser-facing equivalent of the CLI chatbot.

Serves a single-page UI with an ontology dropdown in the banner. Selecting an
ontology either loads its already-built best program (instant) or kicks off the
builder as a background job; once a program exists, the user chats against it.
The conversation itself reuses :class:`ChatSession` unchanged, so the web chat
is behaviorally equivalent to ``ontology-qa-chat`` (follow-up resolution +
history).

Endpoints
---------
    GET  /                        the single-page chat UI
    GET  /api/ontologies          [{name, has_program}] for the dropdown
    POST /api/select              {ontology} -> ready | building(job_id)
    GET  /api/status/{job_id}     build job progress (poll while building)
    POST /api/chat                {conversation_id, message} -> answer

Design notes
------------
* The builder is long-running and costly, so it never runs inside a request.
  ``/api/select`` reuses an existing run's best program when present; otherwise
  it spawns ``ontology-qa-build <name> --plain`` as a background subprocess and
  returns a job id the UI polls. When the job finishes, the next ``/api/select``
  (triggered by the UI) loads the freshly built program.
* One :class:`ChatSession` per conversation id, kept in memory. The underlying
  :class:`OntologyAnswerer` is cached per ontology so switching back to an
  already-loaded ontology is instant.
* Not for hostile multi-tenant use: in-memory state, single process, localhost.

Run it::

    uv sync --extra api
    ontology-qa-web            # serves http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ontology_qa_cli.chat import ChatSession
from ontology_qa_cli.config import load_config
from ontology_qa_cli.query import find_best_program
from ontology_qa_cli.serve import OntologyAnswerer

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover - only without the api extra
    raise ImportError(
        "FastAPI is not installed. Install the optional web stack with "
        "`uv sync --extra api` (or `pip install -e \".[api]\"`)."
    ) from exc


# --------------------------------------------------------------------------- #
# Build job tracking
# --------------------------------------------------------------------------- #
@dataclass
class BuildJob:
    job_id: str
    ontology: str
    proc: subprocess.Popen
    log_path: Path
    status: str = "running"  # running | finished | failed
    lock: threading.Lock = field(default_factory=threading.Lock)

    def poll(self) -> str:
        """Refresh and return status based on the subprocess exit code."""
        with self.lock:
            if self.status != "running":
                return self.status
            code = self.proc.poll()
            if code is None:
                return "running"
            self.status = "finished" if code == 0 else "failed"
            return self.status

    def log_tail(self, max_lines: int = 40) -> str:
        try:
            text = self.log_path.read_text(errors="replace")
        except OSError:
            return ""
        lines = text.splitlines()
        return "\n".join(lines[-max_lines:])


class ChatRequest(BaseModel):
    conversation_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)


class SelectRequest(BaseModel):
    ontology: str = Field(..., min_length=1)


def create_app(
    config_path: Path = Path("ontology_config.yaml"),
    runs_dir: Path = Path("runs/"),
    resolve_followups: bool = True,
) -> "FastAPI":
    config_path = Path(config_path).expanduser()
    runs_dir = Path(runs_dir).expanduser()

    app = FastAPI(title="Ontology QA — Web Chatbot", version="0.1.0")

    # In-memory state (single process).
    answerers: dict[str, OntologyAnswerer] = {}          # ontology -> answerer
    sessions: dict[str, ChatSession] = {}                # conversation_id -> session
    jobs: dict[str, BuildJob] = {}                       # job_id -> build job
    state_lock = threading.Lock()

    def _ontology_names() -> list[str]:
        return [e.name for e in load_config(config_path).ontologies]

    def _owl_for(ontology: str) -> Path:
        for e in load_config(config_path).ontologies:
            if e.name.lower() == ontology.lower():
                return e.owl_file
        raise HTTPException(status_code=404, detail=f"Unknown ontology '{ontology}'")

    def _has_program(ontology: str) -> bool:
        prefix = f"{ontology}_"
        if not runs_dir.exists():
            return False
        return any(
            d.is_dir() and d.name.startswith(prefix) for d in runs_dir.iterdir()
        )

    def _load_answerer(ontology: str) -> OntologyAnswerer:
        """Load (and cache) the answerer for an ontology from its best run."""
        with state_lock:
            if ontology in answerers:
                return answerers[ontology]
        program_path = find_best_program(ontology, runs_dir)
        owl = _owl_for(ontology)
        answerer = OntologyAnswerer.from_program(program_path, owl)
        with state_lock:
            answerers[ontology] = answerer
        return answerer

    # ----------------------------------------------------------------- API --
    @app.get("/api/ontologies")
    async def api_ontologies() -> dict:
        return {
            "ontologies": [
                {"name": name, "has_program": _has_program(name)}
                for name in _ontology_names()
            ]
        }

    @app.post("/api/select")
    async def api_select(req: SelectRequest) -> dict:
        ontology = req.ontology
        if ontology not in _ontology_names():
            raise HTTPException(status_code=404, detail=f"Unknown ontology '{ontology}'")

        # Already-running build for this ontology? Report it.
        with state_lock:
            for job in jobs.values():
                if job.ontology == ontology and job.poll() == "running":
                    return {"status": "building", "job_id": job.job_id}

        if _has_program(ontology):
            # Load in a worker thread so the event loop is not blocked by the
            # (potentially multi-second) ontology load.
            import asyncio

            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, _load_answerer, ontology)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=500, detail=f"Failed to load program: {exc}"
                ) from exc
            return {"status": "ready", "ontology": ontology}

        # No program yet -> start a build as a background subprocess.
        job_id = uuid.uuid4().hex[:12]
        log_path = runs_dir / f"_web_build_{ontology}_{job_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "w")  # noqa: SIM115 - handed to Popen, closed on exit
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "ontology_qa_cli.builder",
                ontology, "--plain",
                "--ontology-config", str(config_path),
                "--output-dir", str(runs_dir),
            ],
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        job = BuildJob(job_id=job_id, ontology=ontology, proc=proc, log_path=log_path)
        with state_lock:
            jobs[job_id] = job
        return {"status": "building", "job_id": job_id}

    @app.get("/api/status/{job_id}")
    async def api_status(job_id: str) -> dict:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job")
        status = job.poll()
        result = {"status": status, "ontology": job.ontology, "log": job.log_tail()}
        if status == "finished":
            # Preload the answerer so the first chat is instant.
            try:
                _load_answerer(job.ontology)
                result["ready"] = True
            except Exception as exc:  # noqa: BLE001
                result["status"] = "failed"
                result["log"] = (result.get("log", "") + f"\n[load error] {exc}").strip()
        return result

    @app.post("/api/chat")
    async def api_chat(req: ChatRequest) -> dict:
        with state_lock:
            session = sessions.get(req.conversation_id)
        if session is None:
            raise HTTPException(
                status_code=400,
                detail="No active conversation. Select an ontology first.",
            )
        import asyncio

        loop = asyncio.get_running_loop()
        try:
            turn = await loop.run_in_executor(None, session.send, req.message)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"Query failed: {exc}") from exc
        return {
            "answer": turn.answer,
            "resolved_question": turn.resolved_question,
            "was_rewritten": turn.was_rewritten,
        }

    @app.post("/api/conversation")
    async def api_conversation(req: SelectRequest) -> dict:
        """Start a conversation bound to an ontology; returns a conversation_id.

        Requires the ontology's answerer to be loaded already (via /api/select).
        """
        ontology = req.ontology
        with state_lock:
            answerer = answerers.get(ontology)
        if answerer is None:
            raise HTTPException(
                status_code=409,
                detail=f"Ontology '{ontology}' is not ready. Select it first.",
            )
        conversation_id = uuid.uuid4().hex
        with state_lock:
            sessions[conversation_id] = ChatSession(
                answerer=answerer, resolve_followups=resolve_followups
            )
        return {"conversation_id": conversation_id, "ontology": ontology}

    # ----------------------------------------------------------------- UI ---
    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _INDEX_HTML

    return app


# The single-page UI is defined at import time as a module constant so the
# package ships without a separate static-file dependency.
_INDEX_HTML = (Path(__file__).parent / "web" / "index.html").read_text(
    encoding="utf-8"
) if (Path(__file__).parent / "web" / "index.html").exists() else "<!doctype html><p>UI missing</p>"


app = None  # constructed by main(); avoids loading config at import time


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve the web chatbot (ontology dropdown + conversational chat)."
    )
    parser.add_argument(
        "--config", type=Path, default=Path("ontology_config.yaml"),
        help="Path to the YAML configuration file (default: ontology_config.yaml).",
    )
    parser.add_argument(
        "--runs-dir", type=Path, default=Path("runs/"),
        help="Base directory where builder runs are stored (default: runs/).",
    )
    parser.add_argument(
        "--no-followups", action="store_true",
        help="Disable LLM follow-up resolution for web conversations.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000).")
    return parser.parse_args()


def main() -> None:
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
    application = create_app(
        config_path=args.config,
        runs_dir=args.runs_dir,
        resolve_followups=not args.no_followups,
    )
    print(f"Serving web chatbot on http://{args.host}:{args.port}", file=sys.stderr)
    uvicorn.run(application, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
