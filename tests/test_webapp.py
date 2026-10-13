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
    # The evaluation set the builder scored this iteration against — surfaced by
    # /api/program as `test_set`. Include a non-dict and a question-less entry
    # to exercise the endpoint's filtering.
    (it / "evaluation.json").write_text(
        json.dumps(
            {
                "results": [
                    {"question": "What is a dog?", "expected_answer": "An animal."},
                    {"question": "What is a cat?", "expected_answer": ""},
                    {"expected_answer": "no question — skipped"},
                    "not-a-dict — skipped",
                ]
            }
        )
    )
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
                display_name: "Tiny Test Ontology"
                owl_file: "{owl}"
                questions:
                  - question: "What is the heart?"
                    expected_answer: "a"
              - name: "NOPROG"
                owl_file: "{owl}"
                questions:
                  - question: "q?"
                    expected_answer: "a"
              - name: "EMPTY"
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
    # EMPTY has a run folder but NO query_program.py inside it (e.g. an
    # interrupted/failed run) — must NOT count as having a program.
    (runs / "EMPTY_20260101_000000" / "iteration_1").mkdir(parents=True)
    return {"cfg": cfg, "runs": runs, "owl": owl, "tmp": tmp_path}


def _client(env, resolve_followups=False):
    app = webapp.create_app(
        config_path=env["cfg"], runs_dir=env["runs"], resolve_followups=resolve_followups
    )
    return TestClient(app)


class TestWebApp:
    def test_index_served(self, env):
        # `/` serves the built React app when present, otherwise a short
        # "frontend not built" instruction page. Either way it is HTML 200.
        with _client(env) as c:
            resp = c.get("/")
            assert resp.status_code == 200
            assert "text/html" in resp.headers["content-type"]

    def test_ontologies_reflect_build_state(self, env):
        with _client(env) as c:
            onts = c.get("/api/ontologies").json()["ontologies"]
            by_name = {o["name"]: o["has_program"] for o in onts}
            assert by_name == {"TINY": True, "NOPROG": False, "EMPTY": False}

    def test_ontologies_expose_canned_questions(self, env):
        # The "I'm feeling lazy!" dropdown is fed by the per-ontology question
        # texts; expected answers (grading data) must NOT be exposed.
        with _client(env) as c:
            onts = c.get("/api/ontologies").json()["ontologies"]
            tiny = next(o for o in onts if o["name"] == "TINY")
            assert tiny["questions"] == ["What is the heart?"]
            assert "expected_answer" not in tiny
            # Every ontology carries a questions list (possibly empty).
            assert all(isinstance(o.get("questions"), list) for o in onts)

    def test_ontologies_expose_display_name(self, env):
        # The dropdown shows the human-readable full name when configured;
        # entries without a display_name report None (UI falls back to the code).
        with _client(env) as c:
            onts = c.get("/api/ontologies").json()["ontologies"]
            by_name = {o["name"]: o.get("display_name") for o in onts}
            assert by_name["TINY"] == "Tiny Test Ontology"
            assert by_name["NOPROG"] is None
            assert by_name["EMPTY"] is None

    def test_select_ready_when_program_exists(self, env):
        with _client(env) as c:
            res = c.post("/api/select", json={"ontology": "TINY"}).json()
            assert res["status"] == "ready"

    def test_select_stream_emits_phases_then_ready(self, env):
        with _client(env) as c:
            with c.stream("GET", "/api/select/stream", params={"ontology": "TINY"}) as resp:
                assert resp.status_code == 200
                body = "".join(resp.iter_text())
            # Each load phase is streamed as a data frame...
            assert "data: Finding best program…" in body
            assert "data: Loading OWL file…" in body
            # ...then a terminal done frame reports the ontology is ready.
            assert "event: done" in body
            done_line = next(
                ln for ln in body.splitlines()
                if ln.startswith("data: {") and "status" in ln
            )
            payload = json.loads(done_line[len("data: "):])
            assert payload["status"] == "ready"
            assert payload["ontology"] == "TINY"

    def test_select_stream_404_for_unknown_ontology(self, env):
        with _client(env) as c:
            assert c.get("/api/select/stream", params={"ontology": "ZZZ"}).status_code == 404

    def test_program_returns_source(self, env):
        with _client(env) as c:
            r = c.get("/api/program/TINY")
            assert r.status_code == 200
            body = r.json()
            assert body["ontology"] == "TINY"
            assert body["path"].endswith("iteration_1/query_program.py")
            assert "def main(" in body["source"]

    def test_program_returns_test_set(self, env):
        with _client(env) as c:
            body = c.get("/api/program/TINY").json()
            # Only well-formed, question-bearing entries survive; a missing
            # expected answer is kept as "".
            assert body["test_set"] == [
                {"question": "What is a dog?", "expected_answer": "An animal."},
                {"question": "What is a cat?", "expected_answer": ""},
            ]

    def test_program_404_when_no_program(self, env):
        with _client(env) as c:
            assert c.get("/api/program/EMPTY").status_code == 404
            assert c.get("/api/program/NOPROG").status_code == 404

    def test_program_404_unknown_ontology(self, env):
        with _client(env) as c:
            assert c.get("/api/program/NOPE").status_code == 404

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

    def test_conversation_survives_task_restart(self, env, monkeypatch):
        """A conversation created on one task (process) must keep working on a
        REPLACEMENT task after a restart. The conversation store is a shared
        file store on the EFS runs mount; a second app instance over the same
        runs_dir models the replacement task (fresh in-memory sessions dict,
        same durable store). We force the file store directly rather than via
        BUILD_BACKEND=ecs so the test doesn't also need the ECS build backend's
        cluster/subnet env."""
        from ontology_qa_cli.conversation_store import FileConversationStore

        def _file_store(runs_dir):
            return FileConversationStore(Path(runs_dir) / ".conversations")

        monkeypatch.setattr(webapp, "make_conversation_store", _file_store)

        # Task A: create a conversation and take a turn.
        with _client(env) as c1:
            c1.post("/api/select", json={"ontology": "TINY"})
            conv = c1.post("/api/conversation", json={"ontology": "TINY"}).json()
            cid = conv["conversation_id"]
            r1 = c1.post(
                "/api/chat",
                json={"conversation_id": cid, "message": "what classes?"},
            )
            assert r1.status_code == 200

        # The persisted record exists on the shared mount.
        assert (env["runs"] / ".conversations" / f"{cid}.json").exists()

        # Task B: a brand-new app instance (empty in-memory sessions) over the
        # SAME runs_dir -- the replacement task. The same conversation_id must
        # NOT 400; it rehydrates from the store and continues.
        with _client(env) as c2:
            # No /api/conversation call here: the conversation predates this task.
            r2 = c2.post(
                "/api/chat",
                json={"conversation_id": cid, "message": "what classes again?"},
            )
            assert r2.status_code == 200, r2.text
            assert "['Heart']" in r2.json()["answer"]

        # History carried over: 2 turns x (user+assistant) = 4 messages persisted.
        rec = json.loads(
            (env["runs"] / ".conversations" / f"{cid}.json").read_text()
        )
        assert len(rec["history"]) == 4
        assert rec["ontology"] == "TINY"

    def test_unknown_conversation_still_400_with_file_store(self, env, monkeypatch):
        """Rehydration must not mask a genuinely unknown conversation id: with
        no record in the store, the chat endpoint still returns 400."""
        from ontology_qa_cli.conversation_store import FileConversationStore

        monkeypatch.setattr(
            webapp, "make_conversation_store",
            lambda runs_dir: FileConversationStore(Path(runs_dir) / ".conversations"),
        )
        with _client(env) as c:
            resp = c.post(
                "/api/chat",
                json={"conversation_id": "deadbeef", "message": "x"},
            )
            assert resp.status_code == 400

    def test_chat_stream_streams_output_then_answer(self, env):
        with _client(env) as c:
            c.post("/api/select", json={"ontology": "TINY"})
            conv = c.post("/api/conversation", json={"ontology": "TINY"}).json()
            with c.stream(
                "GET",
                "/api/chat/stream",
                params={
                    "conversation_id": conv["conversation_id"],
                    "message": "what classes?",
                },
            ) as resp:
                assert resp.status_code == 200
                body = "".join(resp.iter_text())
            # The program's Answer: line is streamed live as a data frame...
            assert "data: Answer: re 'what classes?': ['Heart']" in body
            # ...and a terminal done frame carries the parsed answer.
            assert "event: done" in body
            done_line = next(
                ln for ln in body.splitlines()
                if ln.startswith("data: {") and "answer" in ln
            )
            payload = json.loads(done_line[len("data: "):])
            assert "['Heart']" in payload["answer"]
            assert payload["was_rewritten"] is False

    def test_chat_stream_without_conversation_400(self, env):
        with _client(env) as c:
            resp = c.get(
                "/api/chat/stream",
                params={"conversation_id": "nope", "message": "x"},
            )
            assert resp.status_code == 400

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
        # An ontology with no built program cannot start a conversation: the
        # lazy answerer load fails (find_best_program -> SystemExit) and must
        # surface as a clean 409, not a 500.
        with _client(env) as c:
            assert c.post("/api/conversation", json={"ontology": "NOPROG"}).status_code == 409

    def test_conversation_without_prior_select_lazy_loads(self, env):
        # Regression: with >1 web task behind the ALB, /api/select lands on one
        # task (loading the answerer there) while the follow-up /api/conversation
        # can be routed to a sibling task with an empty answerer cache. That used
        # to 409 and wedge the UI ("Switched to X", composer greyed out forever).
        # The handler now lazy-loads the answerer from the durable program, so a
        # conversation starts for a BUILT ontology even with no prior select on
        # this task. TINY has a program but this fresh client never selected it.
        with _client(env) as c:
            r = c.post("/api/conversation", json={"ontology": "TINY"})
            assert r.status_code == 200
            body = r.json()
            assert body["ontology"] == "TINY"
            assert body["conversation_id"]

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

    def test_rebuild_forces_build_when_program_exists(self, env, monkeypatch):
        runs = env["runs"]
        real_popen = subprocess.Popen

        def fake_popen(cmd, **kw):
            _seed_run(runs, "TINY")  # simulate rebuild producing a program
            return real_popen([sys.executable, "-c", "print('rebuilt')"], **kw)

        monkeypatch.setattr(webapp.subprocess, "Popen", fake_popen)
        with _client(env) as c:
            res = c.post("/api/rebuild", json={"ontology": "TINY"}).json()
            assert res["status"] == "building"
            assert "job_id" in res

    def test_rebuild_409_when_no_program(self, env):
        with _client(env) as c:
            assert c.post("/api/rebuild", json={"ontology": "NOPROG"}).status_code == 409
            assert c.post("/api/rebuild", json={"ontology": "EMPTY"}).status_code == 409

    def test_rebuild_404_unknown_ontology(self, env):
        with _client(env) as c:
            assert c.post("/api/rebuild", json={"ontology": "NOPE"}).status_code == 404

    def test_stream_unknown_job_404(self, env):
        with _client(env) as c:
            assert c.get("/api/stream/deadbeef").status_code == 404

    def test_stream_failing_build_reports_error(self, env, monkeypatch):
        """A failing build streams its output and ends with a 'failed' done event.

        Reproduces the missing-OWL-file case: the builder prints an error and
        exits non-zero; the SSE stream must carry that error and the terminal
        status, not swallow it.
        """
        real_popen = subprocess.Popen

        def fake_popen(cmd, **kw):
            # Simulate the builder failing on a missing OWL file, exit code 1.
            return real_popen(
                [
                    sys.executable, "-c",
                    "import sys; print(\"Error: OWL file not found at '/x/go-plus.owl'\"); "
                    "sys.exit(1)",
                ],
                **kw,
            )

        monkeypatch.setattr(webapp.subprocess, "Popen", fake_popen)

        with _client(env) as c:
            sel = c.post("/api/select", json={"ontology": "NOPROG"}).json()
            assert sel["status"] == "building"
            job = sel["job_id"]

            # Consume the SSE stream to completion.
            with c.stream("GET", f"/api/stream/{job}") as resp:
                assert resp.status_code == 200
                body = "".join(resp.iter_text())

        assert "OWL file not found" in body  # the real error streamed through
        assert "event: done" in body
        assert '"status": "failed"' in body



class TestRunsDirResolution:
    """create_app must find built programs on the shared EFS mount.

    The Dockerfile.web launch is `uvicorn ...:create_app --factory` with NO
    runs_dir argument, so the ONLY way the web task is told where the builder
    Fargate task wrote programs (the EFS mount, ONTOLOGY_RUNS_DIR=/mnt/runs) is
    via that env var. If create_app ignored it and used the container-local
    ./runs, every ontology would show has_program=false even after a successful
    build. These tests lock that wiring in.
    """

    def test_env_runs_dir_used_when_arg_omitted(self, env, monkeypatch):
        monkeypatch.setenv("ONTOLOGY_RUNS_DIR", str(env["runs"]))
        app = webapp.create_app(config_path=env["cfg"], resolve_followups=False)
        with TestClient(app) as c:
            onts = {o["name"]: o["has_program"] for o in c.get("/api/ontologies").json()["ontologies"]}
            # TINY was seeded with a program under env["runs"]; it must be found
            # via the env var alone (no runs_dir arg passed).
            assert onts["TINY"] is True
            assert onts["NOPROG"] is False

    def test_explicit_arg_overrides_env(self, env, monkeypatch):
        # An explicit runs_dir arg wins over the env var.
        monkeypatch.setenv("ONTOLOGY_RUNS_DIR", str(env["tmp"] / "nonexistent"))
        app = webapp.create_app(
            config_path=env["cfg"], runs_dir=env["runs"], resolve_followups=False
        )
        with TestClient(app) as c:
            onts = {o["name"]: o["has_program"] for o in c.get("/api/ontologies").json()["ontologies"]}
            assert onts["TINY"] is True
