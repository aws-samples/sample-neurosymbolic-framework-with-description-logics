"""Transport-agnostic conversational layer over :class:`OntologyAnswerer`.

This module holds the *conversation logic* only — history, follow-up
resolution, and turn handling — with no terminal or HTTP code. Both the CLI
chatbot (``chatbot.py``) and the future web backend build on top of it, so the
conversational behavior lives in exactly one place.

Why a resolution step?
----------------------
The evolved ``query_program.py`` answers ONE self-contained question and has no
memory. Real conversation has follow-ups that only make sense in context::

    user> What are the parts of the heart?
    user> What about the liver?          # <- means "what are the parts of the liver?"
    user> And its blood supply?          # <- refers to the liver

Before calling the (stateless) answerer, :class:`ChatSession` rewrites such a
follow-up into a standalone question using the conversation history. Resolution
is done by a small Bedrock call (same model/region convention as ``judge.py``)
and is entirely optional: the first turn, an explicitly self-contained message,
or a session created with ``resolve_followups=False`` is passed through
verbatim, so the layer works without extra LLM cost in the common case and
degrades gracefully if Bedrock is unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from ontology_qa_cli import llm_config
from ontology_qa_cli.serve import OntologyAnswerer

Role = Literal["user", "assistant"]

_REWRITE_SYSTEM_PROMPT = (
    "You rewrite a user's latest message into a single, self-contained question "
    "about an anatomy/biomedical ontology, using the conversation so far to "
    "resolve pronouns and elliptical follow-ups (e.g. 'what about the liver?' -> "
    "'What are the parts of the liver?').\n\n"
    "Rules:\n"
    "- Output ONLY the rewritten question, nothing else.\n"
    "- If the latest message is already self-contained, output it unchanged.\n"
    "- Preserve the user's intent; do not answer the question or add information.\n"
    "- Keep it a single natural-language question."
)


@dataclass
class Message:
    role: Role
    content: str


@dataclass
class ChatTurn:
    """The result of one user turn."""

    user_message: str
    resolved_question: str  # what was actually sent to the answerer
    answer: Optional[str]  # None if the program produced no answer
    was_rewritten: bool  # True if resolution changed the message


@dataclass
class ChatSession:
    """A stateful conversation over a single :class:`OntologyAnswerer`.

    Not thread-safe: it holds mutable history and the underlying answerer
    serializes its own calls. Use one session per conversation; for the web
    backend, one session per client conversation id.
    """

    answerer: OntologyAnswerer
    resolve_followups: bool = True
    bedrock_client: object = None  # lazily created if needed
    history: list[Message] = field(default_factory=list)

    def reset(self) -> None:
        """Clear the conversation history (start a fresh dialogue)."""
        self.history.clear()

    def send(self, user_message: str) -> ChatTurn:
        """Process one user message and return a structured turn.

        Resolves follow-ups against history (when enabled and there is prior
        context), calls the answerer, and appends both sides to history.
        """
        user_message = user_message.strip()

        resolved = user_message
        was_rewritten = False
        if self.resolve_followups and self.history:
            rewritten = self._resolve(user_message)
            if rewritten and rewritten != user_message:
                resolved = rewritten
                was_rewritten = True

        answer = self.answerer.answer(resolved)

        self.history.append(Message(role="user", content=user_message))
        self.history.append(
            Message(role="assistant", content=answer if answer is not None else "")
        )

        return ChatTurn(
            user_message=user_message,
            resolved_question=resolved,
            answer=answer,
            was_rewritten=was_rewritten,
        )

    def send_streaming(self, user_message: str, on_line) -> ChatTurn:
        """Like :meth:`send`, but stream the program's output live.

        ``on_line`` is called with each stdout/stderr line the evolved program
        emits while it runs, so a UI can show what the program is doing. The
        follow-up resolution step (a Bedrock call, if enabled) happens first and
        is not streamed; only the answerer's program output is.
        """
        user_message = user_message.strip()

        resolved = user_message
        was_rewritten = False
        if self.resolve_followups and self.history:
            rewritten = self._resolve(user_message)
            if rewritten and rewritten != user_message:
                resolved = rewritten
                was_rewritten = True

        answer = self.answerer.answer_streaming(resolved, on_line)

        self.history.append(Message(role="user", content=user_message))
        self.history.append(
            Message(role="assistant", content=answer if answer is not None else "")
        )

        return ChatTurn(
            user_message=user_message,
            resolved_question=resolved,
            answer=answer,
            was_rewritten=was_rewritten,
        )

    def _resolve(self, user_message: str) -> Optional[str]:
        """Rewrite a follow-up into a standalone question via Bedrock.

        Returns the rewritten question, or ``None`` if resolution could not be
        performed (no client, API error) — the caller then falls back to the
        raw message so a Bedrock outage never breaks the chat.
        """
        client = self._get_bedrock()
        if client is None:
            return None

        # Build a compact transcript for context.
        transcript_lines = []
        for m in self.history[-8:]:  # last few turns are enough for coreference
            speaker = "User" if m.role == "user" else "Assistant"
            transcript_lines.append(f"{speaker}: {m.content}")
        transcript = "\n".join(transcript_lines)

        prompt = (
            f"Conversation so far:\n{transcript}\n\n"
            f"Latest user message: {user_message}\n\n"
            "Rewrite the latest user message as a single self-contained question."
        )

        try:
            response = client.converse(
                modelId=llm_config.bedrock_model(),
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                system=[{"text": _REWRITE_SYSTEM_PROMPT}],
            )
            text = response["output"]["message"]["content"][0]["text"].strip()
            return text or None
        except Exception:  # noqa: BLE001 - resolution is best-effort
            return None

    def _get_bedrock(self):
        """Lazily construct a Bedrock client, matching judge.py's convention."""
        if self.bedrock_client is not None:
            return self.bedrock_client
        try:
            import boto3

            self.bedrock_client = boto3.client(
                "bedrock-runtime", region_name=llm_config.bedrock_region()
            )
        except Exception:  # noqa: BLE001 - no creds / no boto3 -> pass-through
            self.bedrock_client = None
        return self.bedrock_client
