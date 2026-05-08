"""Query program for an OWL ontology.

This program uses Opus 4.6 to UNDERSTAND the question (decompose it into
a structured query), then uses owlready2 to ANSWER it by traversing the
ontology graph. The LLM never sees the answer — it only parses the question.
"""
import json
import sys
from pathlib import Path

import boto3
import owlready2


def main(onto_file: Path, question: str) -> None:
    """Answer a question about the ontology.

    Uses Opus 4.6 to understand/decompose the question, then owlready2 to answer it.

    Args:
        onto_file: Path to the OWL XML file.
        question: Natural language question to answer.
    """
    # Load ontology with SQLite caching for faster repeated loads
    cache_file = onto_file.with_suffix(".sqlite3")
    owlready2.default_world.set_backend(filename=str(cache_file))
    onto = owlready2.get_ontology(f"file://{onto_file}").load()

    answer = "I don't know"
    print(f"Answer: {answer}")


if __name__ == "__main__":
    onto_file = Path(sys.argv[1])
    question = sys.argv[2]
    main(onto_file, question)
