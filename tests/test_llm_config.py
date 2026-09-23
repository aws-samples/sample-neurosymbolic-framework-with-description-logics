"""Tests for env-driven Bedrock region/model configuration."""
from ontology_qa_cli import llm_config


def test_defaults_match_previous_hardcoded_values(monkeypatch):
    monkeypatch.delenv("ONTOLOGY_QA_BEDROCK_REGION", raising=False)
    monkeypatch.delenv("ONTOLOGY_QA_BEDROCK_MODEL", raising=False)
    assert llm_config.bedrock_region() == "us-east-1"
    assert llm_config.bedrock_model() == "us.anthropic.claude-opus-4-6-v1"


def test_region_env_override(monkeypatch):
    monkeypatch.setenv("ONTOLOGY_QA_BEDROCK_REGION", "eu-west-1")
    assert llm_config.bedrock_region() == "eu-west-1"


def test_model_env_override(monkeypatch):
    monkeypatch.setenv("ONTOLOGY_QA_BEDROCK_MODEL", "us.anthropic.claude-sonnet-4-5-v1")
    assert llm_config.bedrock_model() == "us.anthropic.claude-sonnet-4-5-v1"


def test_blank_env_falls_back_to_default(monkeypatch):
    # An empty or whitespace-only var is treated as unset, not as a blank id.
    monkeypatch.setenv("ONTOLOGY_QA_BEDROCK_REGION", "   ")
    monkeypatch.setenv("ONTOLOGY_QA_BEDROCK_MODEL", "")
    assert llm_config.bedrock_region() == "us-east-1"
    assert llm_config.bedrock_model() == "us.anthropic.claude-opus-4-6-v1"


def test_chat_resolve_uses_configured_model(monkeypatch):
    # chat.py reads the model at call time, so an override reaches the API call.
    from ontology_qa_cli import chat

    monkeypatch.setenv("ONTOLOGY_QA_BEDROCK_MODEL", "us.anthropic.claude-sonnet-4-5-v1")
    captured = {}

    class FakeClient:
        def converse(self, **kwargs):
            captured.update(kwargs)
            return {"output": {"message": {"content": [{"text": "rewritten?"}]}}}

    session = chat.ChatSession(answerer=None, bedrock_client=FakeClient())
    session.history.append(chat.Message(role="user", content="prior"))
    session.history.append(chat.Message(role="assistant", content="prior answer"))
    result = session._resolve("what about it?")
    assert result == "rewritten?"
    assert captured["modelId"] == "us.anthropic.claude-sonnet-4-5-v1"
