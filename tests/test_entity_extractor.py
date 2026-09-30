from src.entity_extractor import extract_entities_from_doc


def test_entity_extraction_multi_domain(nlp_model):
    from tests.conftest import SAMPLE_TEXTS

    for domain, text in SAMPLE_TEXTS.items():
        doc = nlp_model(text)
        ents = extract_entities_from_doc(doc, f"{domain}.pdf", 1)
        assert len(ents) >= 1, f"no entities for domain {domain}"
        for e in ents:
            assert e.text and e.normalized_name
            assert e.document == f"{domain}.pdf"
            assert e.page_number == 1
            assert e.sentence


def test_noun_chunks_capture_concepts(nlp_model):
    doc = nlp_model("The research center published a breakthrough quantum study.")
    ents = extract_entities_from_doc(doc, "d.pdf", 1, use_noun_chunks=True)
    texts = [e.text.lower() for e in ents]
    assert any("research" in t or "study" in t or "quantum" in t for t in texts)
