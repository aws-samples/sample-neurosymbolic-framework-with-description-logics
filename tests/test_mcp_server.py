"""Tests for the MCP server (ontology_qa_cli.mcp_server).

The OntologyAnswerer is stubbed so these tests exercise the MCP JSON-RPC
protocol + tool wiring WITHOUT loading any ontology or evolved program (fast,
no owlready2 parse).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import ontology_qa_cli.mcp_server as mcp


class _StubAnswerer:
    """Stand-in for OntologyAnswerer: canned reply, no ontology load."""

    def __init__(self, reply="Enrichment for microtubule cytoskeleton organization "
                             "suggests the resistant cells are remodelling their "
                             "microtubules."):
        self._reply = reply
        self.calls = []

    def answer(self, question):
        self.calls.append(question)
        return self._reply


@pytest.fixture
def answer_client(monkeypatch):
    """Client whose answer_question_with_GO is backed by a stub answerer."""
    monkeypatch.setattr(mcp, "load_config", lambda p: object())

    class _StubEntry:
        owl_file = "unused.owl"

    monkeypatch.setattr(mcp, "resolve_ontology", lambda cfg, name: _StubEntry())
    monkeypatch.setattr(mcp, "find_best_program", lambda onto, runs: "unused/query_program.py")
    stub = _StubAnswerer()
    monkeypatch.setattr(
        mcp.OntologyAnswerer, "from_program",
        classmethod(lambda cls, program_path, onto_file: stub),
    )
    client = TestClient(mcp.create_mcp_app(config_path="unused.yaml", ontology="GO"))
    client._stub_answerer = stub  # type: ignore[attr-defined]
    return client


def _rpc(client, method, params=None, id_=1):
    msg = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        msg["params"] = params
    return client.post("/mcp", json=msg)


class TestProtocol:
    def test_initialize(self, answer_client):
        r = _rpc(answer_client, "initialize", {"protocolVersion": mcp.PROTOCOL_VERSION})
        assert r.status_code == 200
        body = r.json()
        assert body["result"]["serverInfo"]["name"] == mcp.SERVER_NAME
        assert "tools" in body["result"]["capabilities"]

    def test_initialized_notification_is_accepted(self, answer_client):
        # Notification (no id) -> 202, no body.
        r = answer_client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert r.status_code == 202

    def test_tools_list(self, answer_client):
        r = _rpc(answer_client, "tools/list")
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert names == {"answer_question_with_GO"}

    def test_unknown_method(self, answer_client):
        r = _rpc(answer_client, "bogus/method")
        assert r.json()["error"]["code"] == mcp._METHOD_NOT_FOUND

    def test_parse_error(self, answer_client):
        r = answer_client.post("/mcp", content=b"not json{")
        assert r.status_code == 400
        assert r.json()["error"]["code"] == mcp._PARSE_ERROR

    def test_unknown_tool(self, answer_client):
        r = _rpc(answer_client, "tools/call", {"name": "nope", "arguments": {}})
        assert r.json()["error"]["code"] == mcp._INVALID_PARAMS


class TestAnswerQuestionTool:
    def test_answer_question_returns_prose(self, answer_client):
        import json
        q = (
            "A GO enrichment analysis reveals significant enrichment for "
            "microtubule cytoskeleton organization in 200 upregulated genes "
            "from drug-resistant cancer cells. What does this mean practically?"
        )
        r = _rpc(answer_client, "tools/call", {
            "name": "answer_question_with_GO", "arguments": {"question": q},
        })
        result = r.json()["result"]
        assert result["isError"] is False
        payload = json.loads(result["content"][0]["text"])
        assert payload["answered"] is True
        assert payload["question"] == q
        assert "microtubule" in payload["answer"].lower()
        # The evolved program (stubbed) was actually invoked with the question.
        assert answer_client._stub_answerer.calls == [q]

    def test_answer_question_missing_is_tool_error(self, answer_client):
        r = _rpc(answer_client, "tools/call", {
            "name": "answer_question_with_GO", "arguments": {},
        })
        assert r.json()["result"]["isError"] is True

    def test_answer_question_no_answer_line(self, answer_client, monkeypatch):
        import json
        # Program ran but produced no Answer: line -> answered:false, not fabricated.
        answer_client._stub_answerer._reply = None
        r = _rpc(answer_client, "tools/call", {
            "name": "answer_question_with_GO", "arguments": {"question": "huh?"},
        })
        payload = json.loads(r.json()["result"]["content"][0]["text"])
        assert payload["answered"] is False

    def test_answer_question_no_program_built_is_tool_error(self, monkeypatch):
        # find_best_program sys.exit(1)s when no program exists -> tool error,
        # NOT a crashed worker.
        monkeypatch.setattr(mcp, "load_config", lambda p: object())

        class _StubEntry:
            owl_file = "unused.owl"

        monkeypatch.setattr(mcp, "resolve_ontology", lambda cfg, name: _StubEntry())

        def _no_program(onto, runs):
            raise SystemExit(1)

        monkeypatch.setattr(mcp, "find_best_program", _no_program)
        client = TestClient(mcp.create_mcp_app(config_path="unused.yaml", ontology="GO"))
        r = _rpc(client, "tools/call", {
            "name": "answer_question_with_GO", "arguments": {"question": "q"},
        })
        result = r.json()["result"]
        assert result["isError"] is True
        assert "no query program has been built" in result["content"][0]["text"].lower()
