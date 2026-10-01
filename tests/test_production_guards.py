"""Production guards: junk entities/relations must not become graph nodes."""
from src.entity_extractor import DEFAULT_DROP_TYPES
from src.entity_resolver import resolve_entities
from src.entity_extractor import ExtractedEntity
from src.graph_builder import ensure_endpoint_entities
from src.relationship_extractor import (
    DEFAULT_DROP_PREDICATES,
    extract_relations_from_sentence,
    _valid_endpoint,
)


def _m(text, etype="ORG"):
    return ExtractedEntity(text=text, normalized_name=text.strip().lower(), entity_type=etype,
                           document="d.pdf", page_number=1, sentence="s")


def test_numeric_types_are_dropped_by_default():
    assert {"CARDINAL", "DATE", "MONEY"} <= DEFAULT_DROP_TYPES


def test_copula_predicates_dropped(nlp_model):
    doc = nlp_model("Rahul Sharma is an engineer at ABC Bank.")
    rels = [r for sent in doc.sents
            for r in extract_relations_from_sentence(sent, "d.pdf", 1)]
    assert all(r.predicate.lower() not in DEFAULT_DROP_PREDICATES for r in rels)


def test_numeric_endpoints_invalid():
    assert not _valid_endpoint("12345")
    assert not _valid_endpoint("x" * 200)
    assert _valid_endpoint("ABC Bank")


def test_long_phrases_do_not_alias_merge():
    long_a = "the application for Home Loan LN30003 submitted by Arjun Verma for processing"
    long_b = "the application for Home Loan LN30004 submitted by Priya Singh for review"
    resolved = resolve_entities([_m(long_a, "NOUN_PHRASE"), _m(long_b, "NOUN_PHRASE")],
                                use_fuzzy=True)
    assert len(resolved) == 2


def test_numeric_placeholder_not_created():
    from src.entity_resolver import ResolvedEntity
    resolved = [ResolvedEntity(id="e1", name="A", normalized_name="a", entity_type="ORG",
                               mentions=["A"], documents=["d.pdf"], pages=[1])]
    from src.relationship_extractor import CandidateRelation
    rels = [CandidateRelation(subject_text="A", predicate="paid", object_text="12345",
                              relation_type="PAID", sentence="A paid 12345.",
                              document="d.pdf", page_number=1,
                              subject_id="e1", object_id=None)]
    out = ensure_endpoint_entities(rels, resolved)
    assert len(out) == 1  # no junk node for "12345"
    assert rels[0].object_id is None
