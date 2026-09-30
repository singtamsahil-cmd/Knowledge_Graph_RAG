from src.entity_extractor import ExtractedEntity
from src.entity_resolver import build_mention_index, deterministic_id, resolve_entities


def _m(text, etype="ORG"):
    return ExtractedEntity(text=text, normalized_name=text.strip().lower(), entity_type=etype,
                           document="d.pdf", page_number=1, sentence="s")


def test_exact_match_merges():
    mentions = [_m("Acme Corporation"), _m("acme corporation "), _m("Beta Inc")]
    resolved = resolve_entities(mentions)
    assert len(resolved) == 2
    acme = next(e for e in resolved if e.normalized_name == "acme corporation")
    assert len(acme.mentions) == 2
    assert acme.id == deterministic_id("acme corporation", "ORG")


def test_different_types_not_merged():
    mentions = [_m("Apple", "ORG"), _m("apple", "PRODUCT")]
    resolved = resolve_entities(mentions)
    assert len(resolved) == 2


def test_conservative_no_fuzzy_by_default():
    mentions = [_m("Acme Corp"), _m("Acme Corporation")]
    resolved = resolve_entities(mentions, use_fuzzy=False)
    assert len(resolved) == 2


def test_mention_index():
    resolved = resolve_entities([_m("Dr. Sharma", "PERSON")])
    idx = build_mention_index(resolved)
    assert idx["dr. sharma"] == resolved[0].id
