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

    def test_program_exception_propagates(self, tmp_path, tiny_owl):
        prog = _write_program(tmp_path, FAILING_PROGRAM)
        answerer = OntologyAnswerer.from_program(prog, tiny_owl)
        with pytest.raises(ValueError, match="boom"):
            answerer.answer("anything")


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
