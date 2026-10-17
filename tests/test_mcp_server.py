"""Tests for the MCP server (ontology_qa_cli.mcp_server).

The server now speaks MCP over the official SDK's **Streamable HTTP** transport
(``mcp.server.fastmcp.FastMCP``), mounted at ``POST /mcp`` inside the FastAPI
app in stateless JSON mode. These tests drive that real transport end to end
with a tiny JSON-RPC-over-HTTP helper: every request carries the
``Accept: application/json, text/event-stream`` header the SDK requires, and
the ``TestClient`` is used as a context manager so the app lifespan (which
starts the SDK session manager) runs.

The OntologyAnswerer is stubbed so these tests exercise the MCP protocol + tool
wiring WITHOUT loading any ontology or evolved program (fast, no owlready2
parse). The server exposes one tool per configured ontology
(answer_question_with_<NAME>), so the fixtures build a server over an explicit
ontology list and back every ontology's answerer with the same stub.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import ontology_qa_cli.mcp_server as mcp


# MCP Streamable HTTP requires the client to accept both JSON and the SSE media
# type, even in json_response mode (the SDK negotiates on it).
_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


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


def _install_stub(monkeypatch, reply=None):
    """Patch the module's loaders so every ontology's answerer is one stub."""
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
    return stub


def _unwrap(response):
    """Return the JSON-RPC body from an SDK response (json or SSE-framed)."""
    ctype = response.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        # Parse the single `data: {...}` line out of the SSE frame.
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        raise AssertionError(f"no data line in SSE body: {response.text!r}")
    return response.json()


class _McpSession:
    """Minimal MCP-over-HTTP driver bound to a TestClient.

    Performs the ``initialize`` handshake on construction, then exposes
    ``call(method, params)`` for subsequent JSON-RPC requests. In stateless
    JSON mode each POST is self-contained, so no session id is threaded.
    """

    def __init__(self, client: TestClient):
        self._client = client
        self._id = 0
        r = self._raw("initialize", {
            "protocolVersion": mcp.PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        })
        assert r.status_code == 200, r.text
        self.initialize_result = _unwrap(r)

    def _next_id(self):
        self._id += 1
        return self._id

    def _raw(self, method, params=None):
        msg = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
        if params is not None:
            msg["params"] = params
        return self._client.post("/mcp", json=msg, headers=_MCP_HEADERS)

    def call(self, method, params=None):
        return self._raw(method, params)


@pytest.fixture
def answer_client(monkeypatch):
    """Single-ontology (GO) client, answer tool backed by a stub answerer."""
    stub = _install_stub(monkeypatch)
    with TestClient(mcp.create_mcp_app(config_path="unused.yaml", ontologies="GO")) as c:
        c._stub_answerer = stub  # type: ignore[attr-defined]
        yield c


@pytest.fixture
def multi_client(monkeypatch):
    """Two-ontology (GO + CTO) client; both tools share one stub answerer."""
    stub = _install_stub(monkeypatch)
    with TestClient(mcp.create_mcp_app(config_path="unused.yaml", ontologies="GO,CTO")) as c:
        c._stub_answerer = stub  # type: ignore[attr-defined]
        yield c


def _tool_result_payload(call_response):
    """Extract the JSON payload from a tools/call text-content result."""
    result = _unwrap(call_response)["result"]
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


class TestProtocol:
    def test_initialize(self, answer_client):
        sess = _McpSession(answer_client)
        info = sess.initialize_result["result"]["serverInfo"]
        assert info["name"] == mcp.SERVER_NAME
        assert "tools" in sess.initialize_result["result"]["capabilities"]

    def test_tools_list_single(self, answer_client):
        sess = _McpSession(answer_client)
        r = sess.call("tools/list")
        names = {t["name"] for t in _unwrap(r)["result"]["tools"]}
        assert names == {"answer_question_with_GO"}

    def test_unknown_tool_is_error(self, answer_client):
        sess = _McpSession(answer_client)
        r = sess.call("tools/call", {"name": "nope", "arguments": {}})
        body = _unwrap(r)
        # The SDK reports an unknown tool either as a JSON-RPC error or an
        # isError tool result depending on version; accept either shape.
        assert ("error" in body) or (body.get("result", {}).get("isError") is True)


class TestMultiTool:
    def test_tools_list_exposes_one_tool_per_ontology(self, multi_client):
        sess = _McpSession(multi_client)
        names = {t["name"] for t in _unwrap(sess.call("tools/list"))["result"]["tools"]}
        assert names == {"answer_question_with_GO", "answer_question_with_CTO"}

    def test_each_tool_descriptions_are_distinct_and_on_topic(self, multi_client):
        sess = _McpSession(multi_client)
        tools = {t["name"]: t for t in _unwrap(sess.call("tools/list"))["result"]["tools"]}
        go_desc = tools["answer_question_with_GO"]["description"].lower()
        cto_desc = tools["answer_question_with_CTO"]["description"].lower()
        assert "gene" in go_desc or "protein" in go_desc
        assert "clinical trial" in cto_desc
        assert go_desc != cto_desc

    def test_cto_tool_answers(self, multi_client):
        sess = _McpSession(multi_client)
        q = "What does a randomized double-blind phase 3 design establish?"
        r = sess.call("tools/call", {
            "name": "answer_question_with_CTO", "arguments": {"question": q},
        })
        payload = _tool_result_payload(r)
        assert payload["answered"] is True
        assert payload["question"] == q
        assert "CTO" in payload["provenance"]

    def test_health_lists_all_ontologies(self, multi_client):
        r = multi_client.get("/mcp/health")
        assert r.json()["ontologies"] == ["GO", "CTO"]


class TestAnswerQuestionTool:
    def test_answer_question_returns_prose(self, answer_client):
        sess = _McpSession(answer_client)
        q = (
            "A GO enrichment analysis reveals significant enrichment for "
            "microtubule cytoskeleton organization in 200 upregulated genes "
            "from drug-resistant cancer cells. What does this mean practically?"
        )
        r = sess.call("tools/call", {
            "name": "answer_question_with_GO", "arguments": {"question": q},
        })
        payload = _tool_result_payload(r)
        assert payload["answered"] is True
        assert payload["question"] == q
        assert "microtubule" in payload["answer"].lower()
        assert "GO" in payload["provenance"]
        # The evolved program (stubbed) was actually invoked with the question.
        assert answer_client._stub_answerer.calls == [q]

    def test_answer_question_missing_is_tool_error(self, answer_client):
        sess = _McpSession(answer_client)
        r = sess.call("tools/call", {
            "name": "answer_question_with_GO", "arguments": {},
        })
        body = _unwrap(r)
        # Missing required 'question' -> SDK input-schema validation error or a
        # tool error result; either is a non-success.
        assert ("error" in body) or (body.get("result", {}).get("isError") is True)

    def test_answer_question_no_answer_line(self, answer_client):
        # Program ran but produced no Answer: line -> answered:false, not fabricated.
        answer_client._stub_answerer._reply = None
        sess = _McpSession(answer_client)
        r = sess.call("tools/call", {
            "name": "answer_question_with_GO", "arguments": {"question": "huh?"},
        })
        payload = _tool_result_payload(r)
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
        with TestClient(mcp.create_mcp_app(config_path="unused.yaml", ontologies="GO")) as client:
            sess = _McpSession(client)
            r = sess.call("tools/call", {
                "name": "answer_question_with_GO", "arguments": {"question": "q"},
            })
            body = _unwrap(r)
            text = json.dumps(body).lower()
            assert ("error" in body) or (body.get("result", {}).get("isError") is True)
            assert "no query program has been built" in text


class TestOntologyParsing:
    def test_defaults_to_go(self):
        assert mcp._parse_ontologies(None) == ["GO"] or "GO" in mcp._parse_ontologies(None)

    def test_comma_separated(self):
        assert mcp._parse_ontologies("GO,CTO") == ["GO", "CTO"]

    def test_strips_blanks_and_dupes(self):
        assert mcp._parse_ontologies(" GO , CTO , GO ,") == ["GO", "CTO"]

    def test_tool_name_for(self):
        assert mcp.tool_name_for("CTO") == "answer_question_with_CTO"
