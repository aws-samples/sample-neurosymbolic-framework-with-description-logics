"""Configuration loader for ontology QA CLI."""
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class QuestionAnswer:
    question: str
    expected_answer: str


@dataclass
class OntologyEntry:
    name: str
    owl_file: Path
    questions: list[QuestionAnswer]


@dataclass
class AppConfig:
    ontologies: list[OntologyEntry]


def load_config(config_path: Path) -> AppConfig:
    """Load and validate the YAML configuration file.

    Raises:
        SystemExit: If file not found, invalid YAML, or missing required fields.
    """
    # Check if file exists
    if not config_path.exists():
        print(
            f"Error: Configuration file not found at '{config_path}'",
            file=sys.stderr,
        )
        sys.exit(1)

    # Try to parse YAML
    try:
        content = config_path.read_text()
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        print(
            f"Error: Invalid YAML in configuration file '{config_path}': {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Repo root is the directory containing the config file. A bare owl_file
    # name (no path component) is resolved relative to <repo_root>/data.
    repo_root = config_path.expanduser().resolve().parent
    data_dir = repo_root / "data"

    # Validate top-level structure
    if not isinstance(data, dict) or "ontologies" not in data:
        print(
            "Error: Configuration file missing required field 'ontologies'",
            file=sys.stderr,
        )
        sys.exit(1)

    ontologies_data = data["ontologies"]
    if not isinstance(ontologies_data, list):
        print(
            "Error: 'ontologies' must be a list",
            file=sys.stderr,
        )
        sys.exit(1)

    # Validate and build ontology entries
    ontologies: list[OntologyEntry] = []
    for i, entry in enumerate(ontologies_data):
        if not isinstance(entry, dict):
            print(
                f"Error: Ontology entry at index {i} must be a mapping",
                file=sys.stderr,
            )
            sys.exit(1)

        # Validate 'name' field
        if "name" not in entry:
            print(
                f"Error: Ontology entry at index {i} missing required field 'name'",
                file=sys.stderr,
            )
            sys.exit(1)
        name = entry["name"]
        if not isinstance(name, str):
            print(
                f"Error: Ontology entry at index {i} field 'name' must be a string",
                file=sys.stderr,
            )
            sys.exit(1)

        # Validate 'owl_file' field
        if "owl_file" not in entry:
            print(
                f"Error: Ontology entry '{name}' missing required field 'owl_file'",
                file=sys.stderr,
            )
            sys.exit(1)
        owl_file = entry["owl_file"]
        if not isinstance(owl_file, str):
            print(
                f"Error: Ontology entry '{name}' field 'owl_file' must be a string",
                file=sys.stderr,
            )
            sys.exit(1)

        # Validate 'questions' field
        if "questions" not in entry:
            print(
                f"Error: Ontology entry '{name}' missing required field 'questions'",
                file=sys.stderr,
            )
            sys.exit(1)
        questions_data = entry["questions"]
        if not isinstance(questions_data, list):
            print(
                f"Error: Ontology entry '{name}' field 'questions' must be a list",
                file=sys.stderr,
            )
            sys.exit(1)

        # Validate each question-answer pair
        questions: list[QuestionAnswer] = []
        for j, qa in enumerate(questions_data):
            if not isinstance(qa, dict):
                print(
                    f"Error: Question at index {j} in ontology '{name}' must be a mapping",
                    file=sys.stderr,
                )
                sys.exit(1)

            if "question" not in qa:
                print(
                    f"Error: Question at index {j} in ontology '{name}' missing required field 'question'",
                    file=sys.stderr,
                )
                sys.exit(1)
            if not isinstance(qa["question"], str):
                print(
                    f"Error: Question at index {j} in ontology '{name}' field 'question' must be a string",
                    file=sys.stderr,
                )
                sys.exit(1)

            if "expected_answer" not in qa:
                print(
                    f"Error: Question at index {j} in ontology '{name}' missing required field 'expected_answer'",
                    file=sys.stderr,
                )
                sys.exit(1)
            if not isinstance(qa["expected_answer"], str):
                print(
                    f"Error: Question at index {j} in ontology '{name}' field 'expected_answer' must be a string",
                    file=sys.stderr,
                )
                sys.exit(1)

            questions.append(
                QuestionAnswer(
                    question=qa["question"],
                    expected_answer=qa["expected_answer"],
                )
            )

        # Resolve the OWL file path. A bare filename (no directory component,
        # e.g. "fma.owl") is assumed to live in <repo_root>/data. Paths with
        # any directory part (absolute, ~, or relative like "./x" or "data/x")
        # are used as-is after user expansion.
        owl_path = Path(owl_file).expanduser()
        if owl_path.parent == Path("."):
            owl_path = data_dir / owl_path.name

        ontologies.append(
            OntologyEntry(
                name=name,
                owl_file=owl_path,
                questions=questions,
            )
        )

    return AppConfig(ontologies=ontologies)


def resolve_ontology(config: AppConfig, name: str) -> OntologyEntry:
    """Look up an ontology by name (case-insensitive).

    Raises:
        SystemExit: If ontology name not found. Prints available names.
    """
    for entry in config.ontologies:
        if entry.name.lower() == name.lower():
            return entry

    available = [e.name for e in config.ontologies]
    print(
        f"Error: Ontology '{name}' not found. Available ontologies: {', '.join(available)}",
        file=sys.stderr,
    )
    sys.exit(1)
