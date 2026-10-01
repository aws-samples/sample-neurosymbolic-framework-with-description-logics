"""Tests for the MCP server (ontology_qa_cli.mcp_server).

The SubsumptionReasoner is stubbed so these tests exercise the MCP JSON-RPC
protocol + tool wiring WITHOUT loading any ontology (fast, no owlready2 parse).
A dedicated reasoner-core test (test_subsumption.py) covers the real DL logic.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import ontology_qa_cli.mcp_server as mcp
from ontology_qa_cli.subsumption import Concept, ConceptMatch, SubsumptionResult


class _StubReasoner:
    """A tiny in-memory stand-in for SubsumptionReasoner."""

    def __init__(self):
        self._ihd = Concept(iri="http://x#SCTID_414545008", label="Ischemic heart disease")
        self._mi = Concept(iri="http://x#SCTID_22298006", label="Myocardial infarction")
        self._stemi = Concept(iri="http://x#SCTID_401303003", label="STEMI")

    def resolve(self, name, *, max_candidates=10):
        key = name.strip().lower()
        if key == "ischemic heart disease":
            return ConceptMatch(query=name, resolved=self._ihd)
        if key == "angina":  # ambiguous
            return ConceptMatch(query=name, candidates=[
                Concept(iri="http://x#A1", label="Stable angina"),
                Concept(iri="http://x#A2", label="Unstable angina"),
            ])
        return ConceptMatch(query=name)  # no match

    def descendants(self, concept, *, include_self=False):
        descs = [self._mi, self._stemi]
        if include_self:
            descs = [concept] + descs
        return SubsumptionResult(concept=concept, descendants=descs)


@pytest.fixture
def stub_client(monkeypatch):
    # Patch load_config/resolve_ontology/SubsumptionReasoner so _get_reasoner
    # returns our stub without touching disk or S3.
    monkeypatch.setattr(mcp, "load_config", lambda p: object())
    monkeypatch.setattr(mcp, "resolve_ontology", lambda cfg, name: object())
    monkeypatch.setattr(mcp, "SubsumptionReasoner", lambda owl: _StubReasoner())

    class _StubEntry:
        owl_file = "unused.owl"

    monkeypatch.setattr(mcp, "resolve_ontology", lambda cfg, name: _StubEntry())
    return TestClient(mcp.create_mcp_app(config_path="unused.yaml", ontology="SNOMED"))


def _rpc(client, method, params=None, id_=1):
    msg = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        msg["params"] = params
    return client.post("/mcp", json=msg)


class TestProtocol:
    def test_initialize(self, stub_client):
        r = _rpc(stub_client, "initialize", {"protocolVersion": mcp.PROTOCOL_VERSION})
        assert r.status_code == 200
        body = r.json()
        assert body["result"]["serverInfo"]["name"] == mcp.SERVER_NAME
        assert "tools" in body["result"]["capabilities"]

    def test_initialized_notification_is_accepted(self, stub_client):
        # Notification (no id) -> 202, no body.
        r = stub_client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert r.status_code == 202

    def test_tools_list(self, stub_client):
        r = _rpc(stub_client, "tools/list")
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert names == {"ontology_subtypes", "ontology_resolve"}

    def test_unknown_method(self, stub_client):
        r = _rpc(stub_client, "bogus/method")
        assert r.json()["error"]["code"] == mcp._METHOD_NOT_FOUND

    def test_parse_error(self, stub_client):
        r = stub_client.post("/mcp", content=b"not json{")
        assert r.status_code == 400
        assert r.json()["error"]["code"] == mcp._PARSE_ERROR


class TestSubtypesTool:
    def test_subtypes_complete_set(self, stub_client):
        import json
        r = _rpc(stub_client, "tools/call", {
            "name": "ontology_subtypes",
            "arguments": {"concept": "Ischemic heart disease"},
        })
        result = r.json()["result"]
        assert result["isError"] is False
        payload = json.loads(result["content"][0]["text"])
        assert payload["resolved"] is True
        assert payload["concept"]["label"] == "Ischemic heart disease"
        labels = {s["label"] for s in payload["subtypes"]}
        assert labels == {"Myocardial infarction", "STEMI"}
        assert payload["subtype_count"] == 2
        assert "no llm" in payload["provenance"].lower()

    def test_subtypes_ambiguous_returns_candidates(self, stub_client):
        import json
        r = _rpc(stub_client, "tools/call", {
            "name": "ontology_subtypes", "arguments": {"concept": "angina"},
        })
        payload = json.loads(r.json()["result"]["content"][0]["text"])
        assert payload["resolved"] is False
        labels = {c["label"] for c in payload["candidates"]}
        assert labels == {"Stable angina", "Unstable angina"}

    def test_subtypes_no_match(self, stub_client):
        import json
        r = _rpc(stub_client, "tools/call", {
            "name": "ontology_subtypes", "arguments": {"concept": "zzz nothing"},
        })
        payload = json.loads(r.json()["result"]["content"][0]["text"])
        assert payload["resolved"] is False and payload["candidates"] == []

    def test_subtypes_missing_concept_is_tool_error(self, stub_client):
        r = _rpc(stub_client, "tools/call", {"name": "ontology_subtypes", "arguments": {}})
        result = r.json()["result"]
        assert result["isError"] is True

    def test_unknown_tool(self, stub_client):
        r = _rpc(stub_client, "tools/call", {"name": "nope", "arguments": {}})
        assert r.json()["error"]["code"] == mcp._INVALID_PARAMS


class TestResolveTool:
    def test_resolve_unique(self, stub_client):
        import json
        r = _rpc(stub_client, "tools/call", {
            "name": "ontology_resolve", "arguments": {"concept": "Ischemic heart disease"},
        })
        payload = json.loads(r.json()["result"]["content"][0]["text"])
        assert payload["resolved"] is True
        assert payload["concept"]["id"] == "SCTID_414545008"
