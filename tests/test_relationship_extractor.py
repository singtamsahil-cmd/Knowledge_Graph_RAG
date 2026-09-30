from src.relationship_extractor import extract_relations_from_doc, normalize_relation


def test_svo_extraction(nlp_model):
    doc = nlp_model("ABC University appointed Dr. Sharma as the director.")
    rels = extract_relations_from_doc(doc, "sample.pdf", 2)
    assert len(rels) >= 1
    for r in rels:
        assert r.subject_text and r.predicate and r.object_text
        assert r.relation_type == normalize_relation(r.predicate.split()[0]) or r.relation_type
        assert r.sentence
        assert r.document == "sample.pdf" and r.page_number == 2
        assert r.extraction_method == "spacy_dependency"
        assert r.review_status == "candidate"


def test_negation_preserved(nlp_model):
    doc = nlp_model("Acme Corporation did not acquire Beta Industries.")
    rels = extract_relations_from_doc(doc, "d.pdf", 1)
    assert any(r.negated for r in rels), "expected a negated candidate relation"


def test_passive_voice(nlp_model):
    doc = nlp_model("Dr. Sharma was appointed by ABC University.")
    rels = extract_relations_from_doc(doc, "d.pdf", 1)
    assert len(rels) >= 1


def test_no_hallucination_on_fragment(nlp_model):
    doc = nlp_model("Hello. Wow.")
    rels = extract_relations_from_doc(doc, "d.pdf", 1)
    assert rels == []
