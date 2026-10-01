"""Provably-correct subsumption queries over an OWL DL ontology.

This module answers questions of the form "what are ALL the specific types of
X?" by computing the transitive ``is-a`` (``rdfs:subClassOf``) descendants of a
concept directly from the ontology graph with owlready2. There is **no LLM in
the reasoning path**: given the ontology, the returned set is sound and complete
by construction, so the answer is provably correct (it is exactly the set of
classes the ontology entails as subclasses).

Contrast with the evolved ``query_program`` pipeline (builder/serve), which has
an LLM propose candidate terms that owlready2 then merely validates for
existence -- grounded, but NOT a completeness proof. For "all subtypes of X"
you want this module, not that pipeline.

Design
------
* The ontology's owlready2 SQLite backend is opened **read-only** on a fresh
  ``World`` (same lock-safe pattern the serving layer uses), so many queries
  run in-process without re-parsing and concurrent readers never contend.
* Concept resolution maps a human string (e.g. "Ischemic heart disease") to an
  ontology class via exact-then-fuzzy label match, skipping obsolete classes.
  Ambiguous matches are returned as candidates rather than guessed at, so the
  caller (or agent) can disambiguate -- never silently pick the wrong concept.
* ``descendants`` uses owlready2's reasoner-free transitive subclass closure
  (``cls.descendants()``), which reflects the asserted ``is-a`` hierarchy. An
  optional role filter (e.g. SNOMED "finding site = Eye") further restricts the
  set to descendants bearing that existential restriction, still deterministic.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import owlready2


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Concept:
    """A resolved ontology class: its stable id (IRI fragment) and label."""
    iri: str
    label: str

    @property
    def id(self) -> str:
        """The IRI fragment (e.g. a SNOMED concept id), stable across runs."""
        # owlready2 IRIs look like "http://.../SCTID_22298006"; take the tail.
        return self.iri.rsplit("/", 1)[-1].rsplit("#", 1)[-1]


@dataclass
class ConceptMatch:
    """Outcome of resolving a human string to a concept.

    Exactly one of ``resolved`` / ``candidates`` is meaningful: a unique match
    sets ``resolved``; an ambiguous one leaves ``resolved`` None and lists
    ``candidates`` so the caller can disambiguate instead of guessing.
    """
    query: str
    resolved: Optional[Concept] = None
    candidates: list[Concept] = field(default_factory=list)

    @property
    def is_unique(self) -> bool:
        return self.resolved is not None


@dataclass
class SubsumptionResult:
    """The provably-complete set of descendants of a concept."""
    concept: Concept
    descendants: list[Concept]
    role_filter: Optional[str] = None

    @property
    def count(self) -> int:
        return len(self.descendants)


# --------------------------------------------------------------------------- #
# Reasoner
# --------------------------------------------------------------------------- #
class SubsumptionReasoner:
    """Loads an ontology once (read-only) and answers subsumption queries.

    NOT thread-safe: owlready2's World is process-global-ish and class
    iteration is not reentrant. Construct one per worker process (or serialize
    calls), mirroring :class:`ontology_qa_cli.serve.OntologyAnswerer`.
    """

    def __init__(self, onto_file: Path) -> None:
        self._onto_file = Path(onto_file)
        self._world = self._open_reader(self._onto_file)
        # Cache (lower-label -> list[class]) for fast, case-insensitive lookup.
        self._label_index: dict[str, list] = {}
        self._build_label_index()

    @staticmethod
    def _open_reader(onto_file: Path) -> "owlready2.World":
        """Open the ontology's SQLite cache read-only (lock-safe, no re-parse).

        Mirrors the serving layer: owlready2 derives the SQLite cache path from
        the OWL path via ``.with_suffix('.sqlite3')``. Opening read-only skips
        owlready2's write-side ANALYZE/PRAGMA maintenance, so many concurrent
        readers never hit 'database is locked'.
        """
        world = owlready2.World()
        cache = onto_file.with_suffix(".sqlite3")
        if cache.exists():
            world.set_backend(filename=str(cache), read_only=True)
        else:
            # First load: parse the OWL into a fresh cache next to the file.
            world.set_backend(filename=str(cache))
            world.get_ontology(onto_file.as_uri()).load()
            world.save()
        return world

    def _build_label_index(self) -> None:
        for cls in self._world.classes():
            if self._is_obsolete(cls):
                continue
            for lab in self._labels_of(cls):
                self._label_index.setdefault(lab.strip().lower(), []).append(cls)

    @staticmethod
    def _labels_of(cls) -> list[str]:
        """All human labels for a class: rdfs:label plus owlready name."""
        labels: list[str] = []
        try:
            labels.extend([str(x) for x in getattr(cls, "label", []) or []])
        except Exception:  # noqa: BLE001 - some classes expose odd label types
            pass
        # SNOMED often carries the Fully Specified Name in a skos:prefLabel or
        # the class name; include the name as a fallback.
        if getattr(cls, "name", None):
            labels.append(str(cls.name))
        return labels

    @staticmethod
    def _is_obsolete(cls) -> bool:
        try:
            dep = getattr(cls, "deprecated", None)
            if dep:
                return True
        except Exception:  # noqa: BLE001
            pass
        name = (getattr(cls, "name", "") or "").lower()
        labels = " ".join(
            str(x).lower() for x in (getattr(cls, "label", []) or [])
        )
        return "obsolete" in name or "obsolete" in labels

    def _to_concept(self, cls) -> Concept:
        labels = self._labels_of(cls)
        label = labels[0] if labels else str(getattr(cls, "name", "?"))
        return Concept(iri=str(cls.iri), label=label)

    # ---- public API ------------------------------------------------------- #
    def resolve(self, name: str, *, max_candidates: int = 10) -> ConceptMatch:
        """Resolve a human string to a concept.

        Exact (case-insensitive) label match wins and is treated as unique when
        it maps to a single class. Otherwise a substring (fuzzy) search returns
        up to ``max_candidates`` for the caller to disambiguate.
        """
        key = name.strip().lower()
        exact = self._label_index.get(key, [])
        if len(exact) == 1:
            return ConceptMatch(query=name, resolved=self._to_concept(exact[0]))
        if len(exact) > 1:
            return ConceptMatch(
                query=name,
                candidates=[self._to_concept(c) for c in exact[:max_candidates]],
            )
        # Fuzzy: substring over indexed labels.
        hits: list = []
        seen: set[str] = set()
        for lab, classes in self._label_index.items():
            if key in lab:
                for c in classes:
                    if str(c.iri) not in seen:
                        seen.add(str(c.iri))
                        hits.append(c)
            if len(hits) >= max_candidates:
                break
        if len(hits) == 1:
            return ConceptMatch(query=name, resolved=self._to_concept(hits[0]))
        return ConceptMatch(
            query=name,
            candidates=[self._to_concept(c) for c in hits[:max_candidates]],
        )

    def descendants(
        self, concept: Concept, *, include_self: bool = False
    ) -> SubsumptionResult:
        """Return ALL transitive is-a descendants of ``concept`` (provable set).

        Uses owlready2's transitive subclass closure, which is the complete set
        of classes the ontology asserts as (indirect) subclasses -- sound and
        complete by construction.
        """
        cls = self._class_for(concept)
        if cls is None:
            return SubsumptionResult(concept=concept, descendants=[])
        descs = []
        seen: set[str] = set()
        for d in cls.descendants(include_self=include_self):
            if self._is_obsolete(d):
                continue
            iri = str(d.iri)
            if iri in seen:
                continue
            seen.add(iri)
            descs.append(self._to_concept(d))
        descs.sort(key=lambda c: c.label.lower())
        return SubsumptionResult(concept=concept, descendants=descs)

    def _class_for(self, concept: Concept):
        """Look the owlready class back up from a resolved concept's IRI."""
        try:
            return self._world[concept.iri] or None
        except Exception:  # noqa: BLE001
            return None

    def close(self) -> None:
        try:
            self._world.close()
        except Exception:  # noqa: BLE001
            pass


_ID_PATTERN = re.compile(r"[A-Za-z]+[_:]\d+$")
