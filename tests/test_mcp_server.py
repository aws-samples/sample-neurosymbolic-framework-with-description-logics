"""Tests for the MCP server (ontology_qa_cli.mcp_server).

The OntologyAnswerer is stubbed so these tests exercise the MCP JSON-RPC
protocol + tool wiring WITHOUT loading any ontology or evolved program (fast,
no owlready2 parse). The server now exposes one tool per configured ontology
(answer_question_with_<NAME>), so the fixtures build a server over an explicit
ontology list and back every ontology's answerer with the same stub.
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


def _make_client(monkeypatch, ontologies, reply=None):
    """Build a TestClient over `ontologies`, every answerer backed by one stub."""
    monkeypatch.setattr(mcp, "load_config", lambda p: object())

    class _StubEntry:
        owl_file = "unused.owl"

    monkeypatch.setattr(mcp, "resolve_ontology", lambda cfg, name: _StubEntry())
    monkeypatch.setattr(mcp, "find_best_program", lambda onto, runs: "unused/query_program.py")
    stub = _StubAnswerer() if reply is None else _StubAnswerer(reply)
    monkeypatch.setattr(
        mcp.OntologyAnswerer, "from_program",
        classmethod(lambda cls, program_path, onto_file: stub),
    )
    client = TestClient(mcp.create_mcp_app(config_path="unused.yaml", ontologies=ontologies))
    client._stub_answerer = stub  # type: ignore[attr-defined]
    return client


@pytest.fixture
def answer_client(monkeypatch):
    """Single-ontology (GO) client, answer tool backed by a stub answerer."""
    return _make_client(monkeypatch, "GO")


@pytest.fixture
def multi_client(monkeypatch):
    """Two-ontology (GO + CTO) client; both tools share one stub answerer."""
    return _make_client(monkeypatch, "GO,CTO")


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

    def test_tools_list_single(self, answer_client):
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


class TestMultiTool:
    def test_tools_list_exposes_one_tool_per_ontology(self, multi_client):
        r = _rpc(multi_client, "tools/list")
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert names == {"answer_question_with_GO", "answer_question_with_CTO"}

    def test_each_tool_descriptions_are_distinct_and_on_topic(self, multi_client):
        tools = {t["name"]: t for t in _rpc(multi_client, "tools/list").json()["result"]["tools"]}
        go_desc = tools["answer_question_with_GO"]["description"].lower()
        cto_desc = tools["answer_question_with_CTO"]["description"].lower()
        assert "gene" in go_desc or "protein" in go_desc
        assert "clinical trial" in cto_desc
        assert go_desc != cto_desc

    def test_cto_tool_answers(self, multi_client):
        import json
        q = "What does a randomized double-blind phase 3 design establish?"
        r = _rpc(multi_client, "tools/call", {
            "name": "answer_question_with_CTO", "arguments": {"question": q},
        })
        result = r.json()["result"]
        assert result["isError"] is False
        payload = json.loads(result["content"][0]["text"])
        assert payload["answered"] is True
        assert payload["question"] == q
        assert "CTO" in payload["provenance"]

    def test_health_lists_all_ontologies(self, multi_client):
        r = multi_client.get("/mcp/health")
        assert r.json()["ontologies"] == ["GO", "CTO"]


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
        assert "GO" in payload["provenance"]
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
        client = TestClient(mcp.create_mcp_app(config_path="unused.yaml", ontologies="GO"))
        r = _rpc(client, "tools/call", {
            "name": "answer_question_with_GO", "arguments": {"question": "q"},
        })
        result = r.json()["result"]
        assert result["isError"] is True
        assert "no query program has been built" in result["content"][0]["text"].lower()


class TestOntologyParsing:
    def test_defaults_to_go(self):
        assert mcp._parse_ontologies(None) == ["GO"] or "GO" in mcp._parse_ontologies(None)

    def test_comma_separated(self):
        assert mcp._parse_ontologies("GO,CTO") == ["GO", "CTO"]

    def test_strips_blanks_and_dupes(self):
        assert mcp._parse_ontologies(" GO , CTO , GO ,") == ["GO", "CTO"]

    def test_tool_name_for(self):
        assert mcp.tool_name_for("CTO") == "answer_question_with_CTO"
