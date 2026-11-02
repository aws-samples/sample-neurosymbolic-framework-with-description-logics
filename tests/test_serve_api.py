"""Tests for the serving layer (serve.OntologyAnswerer) and the FastAPI API.

These use a tiny throwaway OWL file and a fake evolved query program, so they
run without FMA, AWS, or any network. The API tests are skipped if the optional
web stack (FastAPI) is not installed.
"""
import textwrap
from pathlib import Path

import pytest

from ontology_qa_cli.serve import OntologyAnswerer

TINY_OWL = textwrap.dedent(
    """\
    <?xml version="1.0"?>
    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
             xmlns:owl="http://www.w3.org/2002/07/owl#"
             xmlns="http://example.org/tiny#"
             xml:base="http://example.org/tiny">
      <owl:Ontology rdf:about="http://example.org/tiny"/>
      <owl:Class rdf:about="http://example.org/tiny#Heart"/>
      <owl:Class rdf:about="http://example.org/tiny#Liver"/>
    </rdf:RDF>
    """
)

# A fake evolved program honoring the main(onto_file, question) + "Answer:" contract.
FAKE_PROGRAM = textwrap.dedent(
    '''\
    import sys
    from pathlib import Path
    import owlready2

    def main(onto_file, question):
        owlready2.default_world.set_backend(
            filename=str(Path(onto_file).with_suffix(".sqlite3"))
        )
        onto = owlready2.get_ontology(f"file://{onto_file}").load()
        classes = sorted(c.name for c in onto.classes())
        print(f"Answer: q={question!r} classes={classes}")

    if __name__ == "__main__":
        main(Path(sys.argv[1]), sys.argv[2])
    '''
)

# A program that bound owlready2's globals via ``from ... import`` (so the
# module-attribute repointing alone cannot reach it).
FROM_IMPORT_PROGRAM = textwrap.dedent(
    '''\
    from pathlib import Path
    from owlready2 import default_world, get_ontology

    def main(onto_file, question):
        default_world.set_backend(
            filename=str(Path(onto_file).with_suffix(".sqlite3"))
        )
        onto = get_ontology(f"file://{onto_file}").load()
        print(f"Answer: {sorted(c.name for c in onto.classes())}")
    '''
)

# A program that raises, to exercise error propagation / HTTP 500.
FAILING_PROGRAM = textwrap.dedent(
    '''\
    from pathlib import Path

    def main(onto_file, question):
        raise ValueError("boom")
    '''
)

# A program that never prints an Answer: line, to exercise the None path.
SILENT_PROGRAM = textwrap.dedent(
    '''\
    from pathlib import Path

    def main(onto_file, question):
        print("just some output, no answer here")
    '''
)


@pytest.fixture
def tiny_owl(tmp_path: Path) -> Path:
    owl = tmp_path / "tiny.owl"
    owl.write_text(TINY_OWL)
    return owl


def _write_program(tmp_path: Path, source: str) -> Path:
    prog = tmp_path / "query_program.py"
    prog.write_text(source)
    return prog


class TestOntologyAnswerer:
    def test_answers_many_questions_after_single_load(self, tmp_path, tiny_owl):
        prog = _write_program(tmp_path, FAKE_PROGRAM)
        answerer = OntologyAnswerer.from_program(prog, tiny_owl)

        a1 = answerer.answer("first?")
        a2 = answerer.answer("second?")
        assert a1 is not None and "classes=['Heart', 'Liver']" in a1
        assert "q='first?'" in a1
        assert "q='second?'" in a2

    def test_from_import_program_repeated_calls(self, tmp_path, tiny_owl):
        prog = _write_program(tmp_path, FROM_IMPORT_PROGRAM)
        answerer = OntologyAnswerer.from_program(prog, tiny_owl)
        for q in ("a?", "b?"):
            assert answerer.answer(q) == "['Heart', 'Liver']"

    def test_missing_program_raises(self, tmp_path, tiny_owl):
        with pytest.raises(FileNotFoundError):
            OntologyAnswerer.from_program(tmp_path / "nope.py", tiny_owl)

    def test_missing_owl_raises(self, tmp_path):
        prog = _write_program(tmp_path, FAKE_PROGRAM)
        with pytest.raises(FileNotFoundError):
            OntologyAnswerer.from_program(prog, tmp_path / "nope.owl")

    def test_program_without_main_raises(self, tmp_path, tiny_owl):
        prog = _write_program(tmp_path, "x = 1\n")
        with pytest.raises(AttributeError):
            OntologyAnswerer.from_program(prog, tiny_owl)

    def test_no_answer_line_returns_none(self, tmp_path, tiny_owl):
        prog = _write_program(tmp_path, SILENT_PROGRAM)
        answerer = OntologyAnswerer.from_program(prog, tiny_owl)
        assert answerer.answer("anything") is None

    def test_streaming_forwards_output_lines(self, tmp_path, tiny_owl):
        prog = _write_program(tmp_path, FAKE_PROGRAM)
        answerer = OntologyAnswerer.from_program(prog, tiny_owl)

        lines: list[str] = []
        ans = answerer.answer_streaming("what?", lines.append)

        # The program's single print() line is forwarded live...
        assert any("classes=['Heart', 'Liver']" in ln for ln in lines)
        assert any("q='what?'" in ln for ln in lines)
        # ...and the final answer is still extracted and returned.
        assert ans is not None and "classes=['Heart', 'Liver']" in ans

    def test_two_answerers_same_process_isolated_worlds(self, tmp_path):
        """Regression: constructing a second answerer for a different ontology in
        the same process must not fail with owlready2's "Cannot save existent
        quadstore ... File already exists".

        The web app is a single long-lived process; switching ontologies builds a
        new answerer while a previous one has already populated its world. Each
        answerer must load into its own owlready2.World, and the second ontology's
        cache file (created by the first construction) must attach to a fresh,
        empty world rather than the now-dirty default world.
        """
        prog = _write_program(tmp_path, FAKE_PROGRAM)

        owl_a = tmp_path / "onto_a.owl"
        owl_a.write_text(TINY_OWL)
        owl_b = tmp_path / "onto_b.owl"
        owl_b.write_text(TINY_OWL)

        answerer_a = OntologyAnswerer.from_program(prog, owl_a)
        # First construction leaves onto_a.sqlite3 on disk. Now build a second
        # answerer for a different ontology in the SAME process.
        answerer_b = OntologyAnswerer.from_program(prog, owl_b)

        # Both remain independently usable, and each has its own World.
        assert answerer_a._world is not answerer_b._world
        assert answerer_a.answer("a?") is not None
        assert answerer_b.answer("b?") is not None

        # Re-constructing over an EXISTING cache file (the GO case) must also work.
        answerer_b2 = OntologyAnswerer.from_program(prog, owl_b)
        assert answerer_b2.answer("b again?") is not None

    def test_program_exception_propagates(self, tmp_path, tiny_owl):
        prog = _write_program(tmp_path, FAILING_PROGRAM)
        answerer = OntologyAnswerer.from_program(prog, tiny_owl)
        with pytest.raises(ValueError, match="boom"):
            answerer.answer("anything")

    def test_readonly_cache_falls_back_to_in_memory(self, tmp_path, tiny_owl):
        """Regression (CL): an EXISTING on-disk sqlite cache that cannot be
        written to must not crash construction.

        owlready2's read-only backend attach still issues maintenance writes;
        when the cache file is not writable this raises SQLite's "attempt to
        write a readonly database", which previously surfaced to the user as
        "Failed to load program: attempt to write a readonly database" for the
        ontologies whose cache already existed. The answerer must detect that
        and fall back to a fresh in-memory load so answering still works.
        """
        import os
        import stat

        prog = _write_program(tmp_path, FAKE_PROGRAM)

        # First construction materializes <onto>.sqlite3 next to the OWL.
        OntologyAnswerer.from_program(prog, tiny_owl)
        cache = tiny_owl.with_suffix(".sqlite3")
        assert cache.exists(), "expected the warm cache to be created on first load"

        # Make the cache file read-only so owlready2's open-time write fails,
        # reproducing the shared-EFS cache condition that broke CL.
        cache.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        try:
            answerer = OntologyAnswerer.from_program(prog, tiny_owl)
            # Construction succeeded via the in-memory fallback (no on-disk
            # backend) and answering still works end to end.
            ans = answerer.answer("still works?")
            assert ans is not None and "classes=['Heart', 'Liver']" in ans
        finally:
            # Restore write perms so tmp_path cleanup can remove it.
            cache.chmod(stat.S_IRUSR | stat.S_IWUSR)
            os.chmod(cache, stat.S_IRUSR | stat.S_IWUSR)


# The API tests require the optional FastAPI stack + httpx TestClient.
fastapi_testclient = pytest.importorskip("fastapi.testclient")


class TestApi:
    def _client(self, tmp_path, tiny_owl, source):
        from ontology_qa_cli.api import create_app

        prog = _write_program(tmp_path, source)
        answerer = OntologyAnswerer.from_program(prog, tiny_owl)
        return fastapi_testclient.TestClient(create_app(answerer))

    def test_health_ok(self, tmp_path, tiny_owl):
        with self._client(tmp_path, tiny_owl, FAKE_PROGRAM) as client:
            resp = client.get("/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"

    def test_ask_returns_answer(self, tmp_path, tiny_owl):
        with self._client(tmp_path, tiny_owl, FAKE_PROGRAM) as client:
            resp = client.post("/ask", json={"question": "what classes?"})
            assert resp.status_code == 200
            body = resp.json()
            assert body["question"] == "what classes?"
            assert "classes=['Heart', 'Liver']" in body["answer"]

    def test_ask_empty_question_rejected(self, tmp_path, tiny_owl):
        with self._client(tmp_path, tiny_owl, FAKE_PROGRAM) as client:
            assert client.post("/ask", json={"question": ""}).status_code == 422

    def test_ask_missing_field_rejected(self, tmp_path, tiny_owl):
        with self._client(tmp_path, tiny_owl, FAKE_PROGRAM) as client:
            assert client.post("/ask", json={}).status_code == 422

    def test_ask_program_error_is_500(self, tmp_path, tiny_owl):
        with self._client(tmp_path, tiny_owl, FAILING_PROGRAM) as client:
            resp = client.post("/ask", json={"question": "boom?"})
            assert resp.status_code == 500
            assert "boom" in resp.json()["detail"]

    def test_ask_silent_program_returns_null_answer(self, tmp_path, tiny_owl):
        with self._client(tmp_path, tiny_owl, SILENT_PROGRAM) as client:
            resp = client.post("/ask", json={"question": "?"})
            assert resp.status_code == 200
            assert resp.json()["answer"] is None
