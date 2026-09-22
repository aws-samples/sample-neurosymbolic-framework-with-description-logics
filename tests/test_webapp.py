"""Tests for the web chatbot backend (webapp.create_app).

Uses a temp config + seeded run + fake program, so no FMA, AWS, or real build
is involved. The build-job path monkeypatches the builder subprocess with a
trivial command that seeds a run and exits 0. Skipped if FastAPI is absent.
"""
import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi.testclient")
from fastapi.testclient import TestClient  # noqa: E402

import ontology_qa_cli.webapp as webapp  # noqa: E402

TINY_OWL = (
    '<?xml version="1.0"?>'
    '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
    'xmlns:owl="http://www.w3.org/2002/07/owl#" '
    'xmlns="http://example.org/t#" xml:base="http://example.org/t">'
    '<owl:Ontology rdf:about="http://example.org/t"/>'
    '<owl:Class rdf:about="http://example.org/t#Heart"/></rdf:RDF>'
)

FAKE_PROGRAM = textwrap.dedent(
    '''\
    import sys
    from pathlib import Path
    import owlready2
    def main(onto_file, question):
        owlready2.default_world.set_backend(filename=str(Path(onto_file).with_suffix(".sqlite3")))
        onto = owlready2.get_ontology(f"file://{onto_file}").load()
        print(f"Answer: re {question!r}: {sorted(c.name for c in onto.classes())}")
    if __name__ == "__main__":
        main(Path(sys.argv[1]), sys.argv[2])
    '''
)


def _seed_run(runs: Path, ontology: str) -> None:
    it = runs / f"{ontology}_20260101_000000" / "iteration_1"
    it.mkdir(parents=True, exist_ok=True)
    (it / "query_program.py").write_text(FAKE_PROGRAM)
    (runs / f"{ontology}_20260101_000000" / "summary.json").write_text(
        json.dumps({"best_iteration": 1})
    )


@pytest.fixture
def env(tmp_path: Path):
    owl = tmp_path / "tiny.owl"
    owl.write_text(TINY_OWL)
    cfg = tmp_path / "ontology_config.yaml"
    cfg.write_text(
        textwrap.dedent(
            f"""\
            ontologies:
              - name: "TINY"
                owl_file: "{owl}"
                questions:
                  - question: "q?"
                    expected_answer: "a"
              - name: "NOPROG"
                owl_file: "{owl}"
                questions:
                  - question: "q?"
                    expected_answer: "a"
            """
        )
    )
    runs = tmp_path / "runs"
    runs.mkdir()
    _seed_run(runs, "TINY")  # TINY has a program; NOPROG does not
    return {"cfg": cfg, "runs": runs, "owl": owl, "tmp": tmp_path}


def _client(env, resolve_followups=False):
    app = webapp.create_app(
        config_path=env["cfg"], runs_dir=env["runs"], resolve_followups=resolve_followups
    )
    return TestClient(app)


class TestWebApp:
    def test_index_served(self, env):
        with _client(env) as c:
            assert c.get("/").status_code == 200

    def test_ontologies_reflect_build_state(self, env):
        with _client(env) as c:
            onts = c.get("/api/ontologies").json()["ontologies"]
            by_name = {o["name"]: o["has_program"] for o in onts}
            assert by_name == {"TINY": True, "NOPROG": False}

    def test_select_ready_when_program_exists(self, env):
        with _client(env) as c:
            res = c.post("/api/select", json={"ontology": "TINY"}).json()
            assert res["status"] == "ready"

    def test_conversation_and_chat(self, env):
        with _client(env) as c:
            c.post("/api/select", json={"ontology": "TINY"})
            conv = c.post("/api/conversation", json={"ontology": "TINY"}).json()
            r = c.post(
                "/api/chat",
                json={"conversation_id": conv["conversation_id"], "message": "what classes?"},
            ).json()
            assert "['Heart']" in r["answer"]
            assert r["was_rewritten"] is False

    def test_unknown_ontology_select_404(self, env):
        with _client(env) as c:
            assert c.post("/api/select", json={"ontology": "ZZZ"}).status_code == 404

    def test_chat_without_conversation_400(self, env):
        with _client(env) as c:
            assert (
                c.post("/api/chat", json={"conversation_id": "nope", "message": "x"}).status_code
                == 400
            )

    def test_conversation_before_select_409(self, env):
        with _client(env) as c:
            assert c.post("/api/conversation", json={"ontology": "NOPROG"}).status_code == 409

    def test_build_job_flow(self, env, monkeypatch):
        runs = env["runs"]
        real_popen = subprocess.Popen

        def fake_popen(cmd, **kw):
            # Simulate the builder: seed a run for NOPROG, then exit 0.
            _seed_run(runs, "NOPROG")
            return real_popen([sys.executable, "-c", "print('build done')"], **kw)

        monkeypatch.setattr(webapp.subprocess, "Popen", fake_popen)

        with _client(env) as c:
            sel = c.post("/api/select", json={"ontology": "NOPROG"}).json()
            assert sel["status"] == "building"
            job = sel["job_id"]

            status = "running"
            for _ in range(50):
                s = c.get(f"/api/status/{job}").json()
                status = s["status"]
                if status != "running":
                    break
                time.sleep(0.1)
            assert status == "finished"

            conv = c.post("/api/conversation", json={"ontology": "NOPROG"}).json()
            r = c.post(
                "/api/chat",
                json={"conversation_id": conv["conversation_id"], "message": "hi"},
            ).json()
            assert "['Heart']" in r["answer"]

    def test_status_unknown_job_404(self, env):
        with _client(env) as c:
            assert c.get("/api/status/deadbeef").status_code == 404
