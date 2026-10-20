#!/usr/bin/env python3
"""Translate a Turtle (.ttl) ontology into RDF/XML (.owl) for owlready2.

Why this exists
---------------
The NeurosymbolicDL toolchain loads every ontology through **owlready2**
(see ``ontology_qa_cli/subsumption.py`` and ``seed_program.py``), which parses
RDF/XML, OWL/XML and NTriples but **not Turtle**. Some source ontologies ship
only as Turtle and additionally carry SHACL node shapes and SKOS concept
schemes layered on top of the OWL axioms (e.g. the Life Sciences Manufacturing
& Supply Chain ontology, ``lifesci``).

Rather than add a runtime Turtle parser (and a new runtime dependency) to the
served image, we normalise such a file **offline, once**, into the same RDF/XML
``.owl`` shape every other catalog entry uses. The round-trip is loss-free:
rdflib reads every triple (OWL class axioms + ``owl:Restriction`` existentials,
SKOS ``inScheme``/``ConceptScheme`` domain grouping, SHACL ``NodeShape`` data
contracts, and the custom ``materializedIn`` data-asset links) and re-serialises
them as RDF/XML. owlready2 then loads the result natively: the class hierarchy
and existential restrictions drive the subsumption reasoner and the evolved
query program exactly as for FMA/GO/Mondo, while the SKOS/SHACL/data-asset
triples remain reachable through owlready2's graph for relational questions.

The produced ``.owl`` is uploaded to the ontology S3 bucket under
``ontologies/<name>.owl`` and referenced from ``ontology_config.yaml`` by its
``s3://`` URI, identically to every other ontology.

Usage
-----
    # one-off, in a throwaway venv that has rdflib (NOT a project dependency):
    python -m venv /tmp/conv && /tmp/conv/bin/pip install rdflib
    /tmp/conv/bin/python scripts/ttl_to_owl.py <input.ttl> <output.owl>

rdflib is intentionally not in pyproject.toml: this is build-time tooling, not
part of the served runtime.
"""
from __future__ import annotations

import sys
from pathlib import Path


def convert(ttl_path: Path, owl_path: Path) -> int:
    """Parse ``ttl_path`` as Turtle and serialise it as RDF/XML to ``owl_path``.

    Returns the number of triples written so the caller can sanity-check the
    round-trip (a near-zero count means the parse silently produced nothing).
    """
    import rdflib  # local import: build-time only, not a runtime dependency

    g = rdflib.Graph()
    g.parse(str(ttl_path), format="turtle")
    g.serialize(destination=str(owl_path), format="xml")
    return len(g)


def main(argv: list[str]) -> None:
    if len(argv) != 3:
        print(
            "Usage: python scripts/ttl_to_owl.py <input.ttl> <output.owl>",
            file=sys.stderr,
        )
        sys.exit(2)

    ttl_path = Path(argv[1]).expanduser()
    owl_path = Path(argv[2]).expanduser()
    if not ttl_path.exists():
        print(f"Error: input not found: {ttl_path}", file=sys.stderr)
        sys.exit(1)

    n = convert(ttl_path, owl_path)
    print(f"Wrote {owl_path} ({owl_path.stat().st_size} bytes, {n} triples)")


if __name__ == "__main__":
    main(sys.argv)
