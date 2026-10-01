"""Tests for the provable subsumption reasoner (ontology_qa_cli.subsumption).

Uses a tiny synthetic OWL hierarchy so the test is fast and needs no large
licensed ontology. The hierarchy mirrors the shape of the real question
("all specific types of Ischemic Heart Disease"):

    Disease
      └─ HeartDisease
           └─ IschemicHeartDisease
                ├─ StableAngina
                ├─ UnstableAngina
                └─ MyocardialInfarction
                     ├─ STEMI
                     └─ NSTEMI
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ontology_qa_cli.subsumption import SubsumptionReasoner


TINY_OWL = textwrap.dedent(
    """\
    <?xml version="1.0"?>
    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
             xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
             xmlns:owl="http://www.w3.org/2002/07/owl#"
             xml:base="http://example.org/snomed">
      <owl:Ontology rdf:about="http://example.org/snomed"/>
      <owl:Class rdf:about="http://example.org/snomed#Disease">
        <rdfs:label>Disease</rdfs:label>
      </owl:Class>
      <owl:Class rdf:about="http://example.org/snomed#HeartDisease">
        <rdfs:label>Heart disease</rdfs:label>
        <rdfs:subClassOf rdf:resource="http://example.org/snomed#Disease"/>
      </owl:Class>
      <owl:Class rdf:about="http://example.org/snomed#IschemicHeartDisease">
        <rdfs:label>Ischemic heart disease</rdfs:label>
        <rdfs:subClassOf rdf:resource="http://example.org/snomed#HeartDisease"/>
      </owl:Class>
      <owl:Class rdf:about="http://example.org/snomed#StableAngina">
        <rdfs:label>Stable angina</rdfs:label>
        <rdfs:subClassOf rdf:resource="http://example.org/snomed#IschemicHeartDisease"/>
      </owl:Class>
      <owl:Class rdf:about="http://example.org/snomed#UnstableAngina">
        <rdfs:label>Unstable angina</rdfs:label>
        <rdfs:subClassOf rdf:resource="http://example.org/snomed#IschemicHeartDisease"/>
      </owl:Class>
      <owl:Class rdf:about="http://example.org/snomed#MyocardialInfarction">
        <rdfs:label>Myocardial infarction</rdfs:label>
        <rdfs:subClassOf rdf:resource="http://example.org/snomed#IschemicHeartDisease"/>
      </owl:Class>
      <owl:Class rdf:about="http://example.org/snomed#STEMI">
        <rdfs:label>ST elevation myocardial infarction</rdfs:label>
        <rdfs:subClassOf rdf:resource="http://example.org/snomed#MyocardialInfarction"/>
      </owl:Class>
      <owl:Class rdf:about="http://example.org/snomed#NSTEMI">
        <rdfs:label>Non-ST elevation myocardial infarction</rdfs:label>
        <rdfs:subClassOf rdf:resource="http://example.org/snomed#MyocardialInfarction"/>
      </owl:Class>
    </rdf:RDF>
    """
)


@pytest.fixture
def reasoner(tmp_path):
    owl = tmp_path / "tiny-snomed.owl"
    owl.write_text(TINY_OWL)
    r = SubsumptionReasoner(owl)
    yield r
    r.close()


class TestResolve:
    def test_exact_label_unique(self, reasoner):
        m = reasoner.resolve("Ischemic heart disease")
        assert m.is_unique
        assert m.resolved.label == "Ischemic heart disease"

    def test_case_insensitive(self, reasoner):
        assert reasoner.resolve("ISCHEMIC HEART DISEASE").is_unique

    def test_fuzzy_substring_candidates(self, reasoner):
        # "angina" matches two classes -> ambiguous, returned as candidates.
        m = reasoner.resolve("angina")
        assert not m.is_unique
        labels = {c.label for c in m.candidates}
        assert "Stable angina" in labels and "Unstable angina" in labels

    def test_unknown_returns_no_match(self, reasoner):
        m = reasoner.resolve("Nonexistent concept xyz")
        assert not m.is_unique and m.candidates == []


class TestDescendants:
    def test_complete_transitive_set(self, reasoner):
        """ALL specific types of Ischemic Heart Disease -- the provable set.

        Must include the direct children AND the transitive grandchildren
        (STEMI, NSTEMI under Myocardial infarction) -- completeness is the
        whole point.
        """
        m = reasoner.resolve("Ischemic heart disease")
        res = reasoner.descendants(m.resolved)
        labels = {c.label for c in res.descendants}
        assert labels == {
            "Stable angina",
            "Unstable angina",
            "Myocardial infarction",
            "ST elevation myocardial infarction",
            "Non-ST elevation myocardial infarction",
        }
        assert res.count == 5

    def test_include_self(self, reasoner):
        m = reasoner.resolve("Myocardial infarction")
        res = reasoner.descendants(m.resolved, include_self=True)
        labels = {c.label for c in res.descendants}
        assert "Myocardial infarction" in labels  # self included
        assert "ST elevation myocardial infarction" in labels

    def test_leaf_has_no_descendants(self, reasoner):
        m = reasoner.resolve("ST elevation myocardial infarction")
        assert reasoner.descendants(m.resolved).count == 0

    def test_concept_id_is_iri_fragment(self, reasoner):
        c = reasoner.resolve("Ischemic heart disease").resolved
        assert c.id == "IschemicHeartDisease"
