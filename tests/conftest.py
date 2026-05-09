"""Shared test fixtures for ontology QA CLI tests."""
import pytest
from pathlib import Path


@pytest.fixture
def sample_config_path(tmp_path: Path) -> Path:
    """Create a sample ontology config YAML file for testing."""
    config_content = """\
ontologies:
  - name: "TestOntology"
    owl_file: "data/test.owl"
    questions:
      - question: "What is a test?"
        expected_answer: "A test is a procedure."
      - question: "What are the types of tests?"
        expected_answer: "Unit tests and integration tests."
"""
    config_file = tmp_path / "ontology_config.yaml"
    config_file.write_text(config_content)
    return config_file


@pytest.fixture
def sample_config_dict() -> dict:
    """Return a sample config as a dictionary."""
    return {
        "ontologies": [
            {
                "name": "TestOntology",
                "owl_file": "data/test.owl",
                "questions": [
                    {
                        "question": "What is a test?",
                        "expected_answer": "A test is a procedure.",
                    },
                    {
                        "question": "What are the types of tests?",
                        "expected_answer": "Unit tests and integration tests.",
                    },
                ],
            }
        ]
    }
