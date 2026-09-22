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
    GET  /api/program/{ontology}  {path, source} of the current best program
    POST /api/select              {ontology} -> ready | building(job_id)
    POST /api/rebuild             {ontology} -> building(job_id) (force rebuild)
    GET  /api/status/{job_id}     build job progress (poll while building)
    POST /api/chat                {conversation_id, message} -> answer
    GET  /api/chat/stream         {conversation_id, message} -> SSE: program
                                  output lines, then a done frame with answer

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
import json
import os
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
    from fastapi.responses import HTMLResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles
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
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
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
    frontend_dist: Optional[Path] = None,
) -> "FastAPI":
    config_path = Path(config_path).expanduser()
    runs_dir = Path(runs_dir).expanduser()
    frontend_dist = Path(frontend_dist).expanduser() if frontend_dist else _default_frontend_dist()

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
        """True only if a run folder for this ontology exists AND it contains a
        built query program (``iteration_*/query_program.py``).

        The folder alone is not enough: an interrupted or failed run can leave
        an empty ``<name>_<timestamp>/`` directory with no usable program, which
        would otherwise falsely show as ready in the dropdown.
        """
        prefix = f"{ontology}_"
        if not runs_dir.exists():
            return False
        for run_dir in runs_dir.iterdir():
            if not (run_dir.is_dir() and run_dir.name.startswith(prefix)):
                continue
            if any(run_dir.glob("iteration_*/query_program.py")):
                return True
        return False

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

    def _best_program_path(ontology: str) -> Optional[Path]:
        """Resolve the current best program path, or None if there isn't one.

        Mirrors query.find_best_program's resolution (latest matching run ->
        best_iteration from summary.json, else highest iteration_N) but returns
        None instead of exiting, so it is safe to call from a request handler.
        """
        prefix = f"{ontology}_"
        if not runs_dir.exists():
            return None
        matching = sorted(
            (d for d in runs_dir.iterdir() if d.is_dir() and d.name.startswith(prefix)),
            key=lambda d: d.name,
        )
        for run_dir in reversed(matching):  # newest first
            best_iter: Optional[int] = None
            summary = run_dir / "summary.json"
            if summary.exists():
                try:
                    best_iter = int(json.loads(summary.read_text()).get("best_iteration"))
                except (ValueError, TypeError, json.JSONDecodeError):
                    best_iter = None
            if best_iter is not None:
                candidate = run_dir / f"iteration_{best_iter}" / "query_program.py"
                if candidate.exists():
                    return candidate
            # Fall back to the highest-numbered iteration that has a program.
            iters = sorted(
                (
                    d for d in run_dir.iterdir()
                    if d.is_dir() and d.name.startswith("iteration_")
                    and (d / "query_program.py").exists()
                ),
                key=lambda d: int(d.name.split("_")[1]),
            )
            if iters:
                return iters[-1] / "query_program.py"
        return None

    @app.get("/api/program/{ontology}")
    async def api_program(ontology: str) -> dict:
        if ontology not in _ontology_names():
            raise HTTPException(status_code=404, detail=f"Unknown ontology '{ontology}'")
        path = _best_program_path(ontology)
        if path is None:
            raise HTTPException(
                status_code=404,
                detail=f"No query program has been built for '{ontology}' yet.",
            )
        return {
            "ontology": ontology,
            "path": str(path),
            "source": path.read_text(),
        }

    def _start_build(ontology: str) -> str:
        """Spawn the builder as a background subprocess; return its job id.

        Used both by /api/select (when no program exists) and /api/rebuild
        (force a new build even when one does).
        """
        job_id = uuid.uuid4().hex[:12]
        log_path = runs_dir / f"_web_build_{ontology}_{job_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 - handed to Popen, closed on exit
        # Force the child to encode stdout/stderr as UTF-8 regardless of the
        # host locale, so non-ASCII output (em dashes, bullets, Rich glyphs)
        # is written cleanly instead of producing replacement characters.
        child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        proc = subprocess.Popen(
            [
                sys.executable, "-u", "-m", "ontology_qa_cli.builder",
                ontology, "--plain",
                "--ontology-config", str(config_path),
                "--output-dir", str(runs_dir),
            ],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=child_env,
        )
        job = BuildJob(job_id=job_id, ontology=ontology, proc=proc, log_path=log_path)
        with state_lock:
            jobs[job_id] = job
        return job_id

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
        return {"status": "building", "job_id": _start_build(ontology)}

    @app.post("/api/rebuild")
    async def api_rebuild(req: SelectRequest) -> dict:
        """Force a fresh build of the query program, even if one already exists.

        Requires an existing program (the UI only enables the button then).
        Evicts the cached answerer so the newly built program is picked up.
        """
        ontology = req.ontology
        if ontology not in _ontology_names():
            raise HTTPException(status_code=404, detail=f"Unknown ontology '{ontology}'")
        if not _has_program(ontology):
            raise HTTPException(
                status_code=409,
                detail=f"No query program exists for '{ontology}' to rebuild. "
                "Select it to build one first.",
            )

        # If a build is already running for this ontology, just report it.
        with state_lock:
            for job in jobs.values():
                if job.ontology == ontology and job.poll() == "running":
                    return {"status": "building", "job_id": job.job_id}
            # Drop the cached answerer; it will reload from the new program.
            answerers.pop(ontology, None)

        return {"status": "building", "job_id": _start_build(ontology)}

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

    @app.get("/api/stream/{job_id}")
    async def api_stream(job_id: str):
        """Stream the build's stdout/stderr live via Server-Sent Events.

        Emits `data:` lines as the builder writes them, then a final
        `event: done` carrying the terminal status (finished|failed). On finish
        the answerer is preloaded so chat is instant. This is what lets the UI
        show exactly what the builder is doing — including the failure message.
        """
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown job")

        import asyncio
        import json as _json

        async def event_stream():
            pos = 0
            # Tail the log file until the process exits and the file is drained.
            while True:
                try:
                    with open(job.log_path, "r", encoding="utf-8", errors="replace") as fh:
                        fh.seek(pos)
                        chunk = fh.read()
                        pos = fh.tell()
                except OSError:
                    chunk = ""
                if chunk:
                    # SSE frames: one `data:` line per output line.
                    for line in chunk.splitlines():
                        yield f"data: {line}\n\n"
                status = job.poll()
                if status != "running":
                    # Drain any final bytes written after the last read.
                    try:
                        with open(job.log_path, "r", encoding="utf-8", errors="replace") as fh:
                            fh.seek(pos)
                            tail = fh.read()
                        for line in tail.splitlines():
                            yield f"data: {line}\n\n"
                    except OSError:
                        pass
                    ready = False
                    if status == "finished":
                        try:
                            _load_answerer(job.ontology)
                            ready = True
                        except Exception as exc:  # noqa: BLE001
                            status = "failed"
                            yield f"data: [load error] {exc}\n\n"
                    payload = _json.dumps(
                        {"status": status, "ready": ready, "ontology": job.ontology}
                    )
                    yield f"event: done\ndata: {payload}\n\n"
                    return
                await asyncio.sleep(0.5)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

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

    @app.get("/api/chat/stream")
    async def api_chat_stream(conversation_id: str, message: str):
        """Answer a question while streaming the program's stdout/stderr live.

        Server-Sent Events: each `data:` frame is one output line the evolved
        program emitted as it ran; a final `event: done` frame carries the
        answer (and the rewritten question, if a follow-up was resolved). This
        is what lets the UI show "what the program is doing" under a collapsible
        disclosure while the answer is still being computed.
        """
        with state_lock:
            session = sessions.get(conversation_id)
        if session is None:
            raise HTTPException(
                status_code=400,
                detail="No active conversation. Select an ontology first.",
            )

        import asyncio
        import json as _json

        loop = asyncio.get_running_loop()
        q: "asyncio.Queue[object]" = asyncio.Queue()
        _SENTINEL = object()

        def _put(item: object) -> None:
            # Called from the worker thread: schedule the put on the event loop.
            loop.call_soon_threadsafe(q.put_nowait, item)

        def _on_line(line: str) -> None:
            _put(("line", line))

        def _work() -> None:
            try:
                turn = session.send_streaming(message, _on_line)
                _put(
                    (
                        "done",
                        {
                            "answer": turn.answer,
                            "resolved_question": turn.resolved_question,
                            "was_rewritten": turn.was_rewritten,
                        },
                    )
                )
            except Exception as exc:  # noqa: BLE001 - surface as a stream error
                _put(("error", str(exc)))
            finally:
                _put(_SENTINEL)

        async def event_stream():
            # Run the (blocking) answerer in a worker thread; drain its queue.
            # asyncio.Queue.get() yields to the loop, so call_soon_threadsafe
            # puts from the worker thread are actually delivered (a plain
            # queue.Queue would block the loop and deadlock the handoff).
            loop.run_in_executor(None, _work)
            while True:
                item = await q.get()
                if item is _SENTINEL:
                    return
                kind, payload = item
                if kind == "line":
                    # SSE data frames cannot contain raw newlines; each call is
                    # already one line, so emit it directly.
                    yield f"data: {payload}\n\n"
                elif kind == "done":
                    yield f"event: done\ndata: {_json.dumps(payload)}\n\n"
                elif kind == "error":
                    yield f"event: error\ndata: {_json.dumps({'detail': payload})}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

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
    # Serve the built React app (frontend/dist). If it has not been built yet,
    # degrade to a short instruction page rather than a broken UI.
    dist_dir = Path(frontend_dist).expanduser()
    index_file = dist_dir / "index.html"
    assets_dir = dist_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    _NOT_BUILT_HTML = (
        "<!doctype html><html><body style=\"font-family:sans-serif;max-width:640px;"
        "margin:60px auto;line-height:1.6\">"
        "<h2>Frontend not built</h2>"
        "<p>The React app has not been built yet. From the project root run:</p>"
        "<pre style=\"background:#f4f4f4;padding:12px;border-radius:8px\">"
        "cd frontend\nnpm install\nnpm run build</pre>"
        "<p>Then reload this page. For live development instead, run "
        "<code>npm run dev</code> and open the Vite dev server.</p>"
        "</body></html>"
    )

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        if index_file.is_file():
            return index_file.read_text(encoding="utf-8")
        return _NOT_BUILT_HTML

    return app


app = None  # constructed by main(); avoids loading config at import time


def _default_frontend_dist() -> Path:
    """Locate the built React app (frontend/dist) relative to the repo root."""
    # ontology_qa_cli/webapp.py -> repo root is the package's parent.
    return Path(__file__).resolve().parent.parent / "frontend" / "dist"


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
    parser.add_argument(
        "--frontend-dist", type=Path, default=None,
        help="Path to the built React app (default: <repo>/frontend/dist). "
        "Build it with: cd frontend && npm install && npm run build.",
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
        frontend_dist=args.frontend_dist,
    )
    print(f"Serving web chatbot on http://{args.host}:{args.port}", file=sys.stderr)
    uvicorn.run(application, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
