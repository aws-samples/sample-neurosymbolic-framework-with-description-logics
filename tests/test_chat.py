"""Tests for the conversational layer (chat.ChatSession).

These use a fake answerer and a stubbed Bedrock client, so no ontology, AWS, or
network is involved.
"""
from dataclasses import dataclass

import pytest

from ontology_qa_cli.chat import ChatSession, Message


@dataclass
class FakeAnswerer:
    """Records the exact question strings passed to answer()."""

    calls: list = None

    def __post_init__(self):
        self.calls = []

    def answer(self, question: str):
        self.calls.append(question)
        return f"answer-to: {question}"


class FakeBedrock:
    """Stub Bedrock client whose converse() returns a canned rewrite."""

    def __init__(self, rewrite: str = None, raise_exc: bool = False):
        self.rewrite = rewrite
        self.raise_exc = raise_exc
        self.converse_calls = 0

    def converse(self, **kwargs):
        self.converse_calls += 1
        if self.raise_exc:
            raise RuntimeError("bedrock down")
        return {"output": {"message": {"content": [{"text": self.rewrite}]}}}


class TestChatSession:
    def test_first_turn_is_passed_through_verbatim(self):
        ans = FakeAnswerer()
        session = ChatSession(answerer=ans, resolve_followups=True, bedrock_client=FakeBedrock())
        turn = session.send("What are the parts of the heart?")
        # No history yet -> no rewrite, Bedrock not consulted.
        assert turn.resolved_question == "What are the parts of the heart?"
        assert turn.was_rewritten is False
        assert ans.calls == ["What are the parts of the heart?"]
        assert turn.answer == "answer-to: What are the parts of the heart?"

    def test_followup_is_rewritten_using_history(self):
        ans = FakeAnswerer()
        bedrock = FakeBedrock(rewrite="What are the parts of the liver?")
        session = ChatSession(answerer=ans, resolve_followups=True, bedrock_client=bedrock)

        session.send("What are the parts of the heart?")
        turn = session.send("what about the liver?")

        assert turn.was_rewritten is True
        assert turn.resolved_question == "What are the parts of the liver?"
        # The answerer received the REWRITTEN question, not the raw follow-up.
        assert ans.calls[-1] == "What are the parts of the liver?"
        assert bedrock.converse_calls == 1

    def test_no_rewrite_when_disabled(self):
        ans = FakeAnswerer()
        bedrock = FakeBedrock(rewrite="SHOULD NOT BE USED")
        session = ChatSession(answerer=ans, resolve_followups=False, bedrock_client=bedrock)

        session.send("What are the parts of the heart?")
        turn = session.send("what about the liver?")

        assert turn.was_rewritten is False
        assert turn.resolved_question == "what about the liver?"
        assert ans.calls[-1] == "what about the liver?"
        assert bedrock.converse_calls == 0

    def test_rewrite_equal_to_input_is_not_flagged(self):
        ans = FakeAnswerer()
        # Bedrock returns the message unchanged (already self-contained).
        bedrock = FakeBedrock(rewrite="Is blood a liquid?")
        session = ChatSession(answerer=ans, resolve_followups=True, bedrock_client=bedrock)

        session.send("What are the parts of the heart?")
        turn = session.send("Is blood a liquid?")

        assert turn.was_rewritten is False
        assert turn.resolved_question == "Is blood a liquid?"

    def test_bedrock_failure_falls_back_to_raw_message(self):
        ans = FakeAnswerer()
        bedrock = FakeBedrock(raise_exc=True)
        session = ChatSession(answerer=ans, resolve_followups=True, bedrock_client=bedrock)

        session.send("What are the parts of the heart?")
        turn = session.send("what about the liver?")

        # Resolution failed -> raw follow-up is used, chat keeps working.
        assert turn.was_rewritten is False
        assert turn.resolved_question == "what about the liver?"
        assert ans.calls[-1] == "what about the liver?"

    def test_history_accumulates_both_sides(self):
        ans = FakeAnswerer()
        session = ChatSession(answerer=ans, resolve_followups=False)
        session.send("q1")
        session.send("q2")
        assert [m.role for m in session.history] == ["user", "assistant", "user", "assistant"]
        assert session.history[0] == Message(role="user", content="q1")
        assert session.history[1].content == "answer-to: q1"

    def test_reset_clears_history(self):
        ans = FakeAnswerer()
        session = ChatSession(answerer=ans, resolve_followups=False)
        session.send("q1")
        assert session.history
        session.reset()
        assert session.history == []

    def test_none_answer_stored_as_empty_string(self):
        class NoneAnswerer:
            def answer(self, q):
                return None

        session = ChatSession(answerer=NoneAnswerer(), resolve_followups=False)
        turn = session.send("q")
        assert turn.answer is None
        # Assistant side of history is a string, never None.
        assert session.history[-1].content == ""
