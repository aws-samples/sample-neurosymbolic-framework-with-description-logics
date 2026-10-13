"""Durable storage for chat conversations.

The web service runs more than one task behind an ALB, and any task can be
replaced at any time (deploy, health-check cycle, or an unexpected task loss).
Conversation state used to live only in an in-process ``sessions`` dict, so:

  * a task restart silently dropped every open conversation, and
  * even without a restart, a follow-up request load-balanced to a *sibling*
    task hit a task that had never seen that conversation id,

and in both cases ``/api/chat`` returned ``400 "No active conversation"`` and
the UI fell back to re-selecting the ontology (the "it died and reloaded"
symptom).

This module persists the only *irreplaceable* part of a conversation -- its
ontology binding and message history -- to the shared EFS runs mount, exactly
like :mod:`build_backend`'s ``FileJobStore``. The heavy ``OntologyAnswerer`` is
NOT persisted: it is rebuilt from the ontology's program on a cache miss, so a
record is tiny and portable across tasks.

Selection mirrors ``make_job_store``: the ``ecs`` backend (multi-task) uses the
file store under ``<runs_dir>/.conversations``; any other (single-process)
deployment keeps the in-memory store, preserving the original behavior exactly.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import List, Optional, Protocol

from ontology_qa_cli.chat import ChatSession, Message


# A persisted conversation: the ontology it is bound to, its message history,
# and a last-touched timestamp (for optional age-based pruning). The live
# ChatSession (with its OntologyAnswerer) is reconstructed from this by the
# webapp via _load_answerer(ontology).
def session_to_record(conversation_id: str, ontology: str,
                      session: ChatSession) -> dict:
    """Serialize the shareable fields of a live session."""
    return {
        "conversation_id": conversation_id,
        "ontology": ontology,
        "history": [{"role": m.role, "content": m.content} for m in session.history],
        "updated_at": time.time(),
    }


def history_from_record(record: dict) -> List[Message]:
    """Rebuild the ChatSession.history list from a persisted record."""
    out: List[Message] = []
    for m in record.get("history", []):
        role = m.get("role")
        content = m.get("content", "")
        if role in ("user", "assistant"):
            out.append(Message(role=role, content=content))
    return out


class ConversationStore(Protocol):
    """Registry mapping conversation_id -> persisted conversation record."""

    def put(self, conversation_id: str, ontology: str, session: ChatSession) -> None:
        """Record (or update) a conversation's ontology + history."""
        ...

    def get(self, conversation_id: str) -> Optional[dict]:
        """Return the persisted record for ``conversation_id``, or None."""
        ...


class MemoryConversationStore:
    """In-process registry. Correct for any single-process deployment; keeps
    the original in-memory-only behavior (nothing is written to disk)."""

    def __init__(self) -> None:
        self._records: dict[str, dict] = {}
        self._lock = threading.Lock()

    def put(self, conversation_id: str, ontology: str, session: ChatSession) -> None:
        with self._lock:
            self._records[conversation_id] = session_to_record(
                conversation_id, ontology, session)

    def get(self, conversation_id: str) -> Optional[dict]:
        with self._lock:
            return self._records.get(conversation_id)


class FileConversationStore:
    """Conversation registry persisted as one JSON file per conversation under
    a shared directory on the EFS access point every web task mounts, so a
    conversation started on one task is visible to all (and survives a task
    restart). Writes are atomic (temp file + ``os.replace``) so a concurrent
    reader never sees a half-written record.
    """

    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, conversation_id: str) -> Path:
        # conversation_id is a 32-char uuid hex (we control it); still guard
        # against a path-traversal value by taking only the basename.
        safe = os.path.basename(conversation_id)
        return self._dir / f"{safe}.json"

    def put(self, conversation_id: str, ontology: str, session: ChatSession) -> None:
        path = self._path(conversation_id)
        tmp = path.with_suffix(".json.tmp")
        try:
            tmp.write_text(
                json.dumps(session_to_record(conversation_id, ontology, session)),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def get(self, conversation_id: str) -> Optional[dict]:
        path = self._path(conversation_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def make_conversation_store(runs_dir: Path) -> ConversationStore:
    """Return the conversation store matching the active ``BUILD_BACKEND``.

    ecs        -> FileConversationStore under ``<runs_dir>/.conversations``
                  (shared EFS mount), so every web task sees the same
                  conversations and they survive a task restart.
    subprocess -> MemoryConversationStore (single process; original behavior).
    """
    kind = os.environ.get("BUILD_BACKEND", "subprocess").strip().lower()
    if kind == "ecs":
        return FileConversationStore(Path(runs_dir) / ".conversations")
    return MemoryConversationStore()
