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
    # Load ontology with SQLite caching for faster repeated loads.
    #
    # The cache (onto.sqlite3) is a shared on-disk SQLite backend. Under the
    # builder's parallel evaluation, many copies of this program run at once,
    # so we must NOT all try to (re)populate the same backend concurrently:
    # SQLite has a single writer and the losers fail with "database is locked".
    # The builder warms this cache serially before eval, so here we open the
    # existing backend and only parse+load the OWL when the cache is empty.
    cache_file = onto_file.with_suffix(".sqlite3")
    cache_exists = cache_file.exists()
    owlready2.default_world.set_backend(filename=str(cache_file))
    if not cache_exists or not list(owlready2.default_world.ontologies):
        onto = owlready2.get_ontology(f"file://{onto_file}").load()
    else:
        # Cache already populated by the builder's serial warm-up; reuse it.
        onto = next(iter(owlready2.default_world.ontologies.values()))

    answer = "I don't know"
    print(f"Answer: {answer}")


if __name__ == "__main__":
    onto_file = Path(sys.argv[1])
    question = sys.argv[2]
    main(onto_file, question)
