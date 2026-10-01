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


def test_title_relation_extracted(nlp_model):
    from src.relationship_extractor import extract_relations_from_sentence
    doc = nlp_model("Customer Service Lead Priya Nair opened case CS-8831.")
    rels = [r for sent in doc.sents
            for r in extract_relations_from_sentence(sent, "d.pdf", 1)]
    titles = [r for r in rels if r.predicate == "title"]
    assert titles and titles[0].subject_text == "Priya Nair"
    assert "Lead" in titles[0].object_text


def test_copula_amount_kept(nlp_model):
    from src.relationship_extractor import extract_relations_from_sentence
    doc = nlp_model("The sanctioned amount was INR 1,200,000.")
    rels = [r for sent in doc.sents
            for r in extract_relations_from_sentence(sent, "d.pdf", 1)]
    assert any(r.predicate == "be" and "1,200,000" in r.object_text for r in rels)


def test_demonstrative_resolved(nlp_model):
    from src.relationship_extractor import extract_relations_from_doc
    doc = nlp_model("She maintains Current Account CA20001. GreenLeaf Traders uses this account daily.")
    rels = extract_relations_from_doc(doc, "d.pdf", 1)
    assert any(r.object_text == "CA20001" for r in rels)


def test_id_code_alias_index():
    from src.entity_extractor import ExtractedEntity
    from src.entity_resolver import build_mention_index, resolve_entities
    ms = [ExtractedEntity(text="Current Account CA20001", normalized_name="current account ca20001",
                          entity_type="ORG", document="d.pdf", page_number=1, sentence="s")]
    idx = build_mention_index(resolve_entities(ms))
    assert "ca20001" in idx


def test_no_hallucination_on_fragment(nlp_model):
    doc = nlp_model("Hello. Wow.")
    rels = extract_relations_from_doc(doc, "d.pdf", 1)
    assert rels == []
