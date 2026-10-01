"""Validation, events, hybrid retrieval (mocked; no Neo4j/Qdrant/LLM needed)."""
from src.entity_extractor import ExtractedEntity
from src.events import build_events
from src.relationship_extractor import CandidateRelation
from src.validate import attach_temporal, validate_relations


def _rel(s, p, o, ev="Some evidence sentence here.", neg=False, conf=0.8,
         sid="s1", oid="o1", doc="d.pdf"):
    return CandidateRelation(subject_text=s, predicate=p, object_text=o,
                             relation_type=p.upper().replace(" ", "_"),
                             sentence=ev, document=doc, page_number=1,
                             negated=neg, confidence=conf,
                             subject_id=sid, object_id=oid)


def test_contradiction_affirmed_vs_negated():
    kept, rep = validate_relations([
        _rel("A", "approve", "B"), _rel("A", "approve", "B", neg=True)])
    assert rep["contradicted"] == 2
    assert all(r.review_status == "contradicted" for r in kept)


def test_status_conflict_approved_vs_pending():
    kept, rep = validate_relations([
        _rel("Bank", "approve", "Loan X", sid="s", oid="loanx"),
        _rel("Clerk", "review", "Loan X", sid="c", oid="loanx")])
    assert rep["contradicted"] == 2


def test_duplicate_merges_and_verified_tier():
    kept, rep = validate_relations([_rel("A", "buy", "B"), _rel("A", "buy", "B")])
    assert rep["duplicates_merged"] == 1 and len(kept) == 1
    assert kept[0].review_status == "verified"


def test_uncertain_below_threshold_unlinked():
    kept, rep = validate_relations(
        [_rel("A", "buy", "B", conf=0.2, sid=None, oid=None)], min_confidence=0.5)
    assert kept[0].review_status == "uncertain"
    assert rep["uncertain"] == 1


def test_evidence_less_dropped():
    kept, rep = validate_relations([_rel("A", "buy", "B", ev="  ")])
    assert rep["dropped_no_evidence"] == 1 and kept == []


def test_attach_temporal_from_date_mention():
    ents = [ExtractedEntity(text="5 May 2026", normalized_name="5 may 2026",
                            entity_type="DATE", document="d.pdf", page_number=1,
                            sentence="Paid on 5 May 2026.")]
    # DATE mention text must match: use mentions via resolve-like shape
    from src.entity_resolver import ResolvedEntity
    resolved_like = [ResolvedEntity(id="e", name="5 May 2026",
                                    normalized_name="5 may 2026", entity_type="DATE",
                                    mentions=["5 May 2026"],
                                    sentences=["Paid on 5 May 2026."])]
    rels = [_rel("A", "pay", "B", ev="Paid on 5 May 2026.")]
    assert attach_temporal(rels, resolved_like) == 1
    assert rels[0].event_date == "5 May 2026"


def test_build_events_gated_and_shaped():
    rels = [_rel("A", "transfer", "B", ev="A transferred on 1 June.", conf=0.9)]
    rels[0].event_date = "1 June"
    evs = build_events(rels)
    assert len(evs) == 1
    assert evs[0].participants == [{"entity_id": "s1", "role": "subject"},
                                   {"entity_id": "o1", "role": "object"}]
    low = [_rel("A", "transfer", "B", conf=0.2)]
    low[0].event_date = "1 June"
    assert build_events(low) == []


def test_hybrid_dedupe_and_hop2_flags():
    from src.rag import _deduplicate, _multihop_expand
    a = {"subject": "A", "predicate": "p", "object": "B"}
    assert len(_deduplicate([[a], [dict(a)]])) == 1
    seeds = [{"subject": "A", "predicate": "p", "object": "B"}]
    pool = [dict(a), {"subject": "B", "predicate": "q", "object": "C"}]
    extra = _multihop_expand(seeds, pool)
    assert len(extra) == 1 and extra[0]["_inferred"] is True


def test_vector_point_id_is_uuid():
    from src.vector_store import _point_id
    import re
    assert re.fullmatch(r"[0-9a-f-]{36}", _point_id("d", "s", "p"))
    assert _point_id("d", "s", "p") == _point_id("d", "s", "p")


def test_hybrid_strategy_entity_first(tmp_path):
    import json
    from src.rag import hybrid_retrieve
    rels = [{"subject": "A", "predicate": "pay", "object": "B",
             "evidence": "A paid B.", "document": "d.pdf", "page": 1,
             "confidence": 0.9}]
    (tmp_path / "relationships.json").write_text(json.dumps(rels), encoding="utf-8")
    cfg = {"qdrant": {"enabled": False}, "ollama": {"top_k": 5}}
    facts, meta = hybrid_retrieve('What about "TXN90001"?', cfg, store=None, out_dir=tmp_path)
    assert meta["strategy"] == "graph"  # vector disabled -> graph only
    assert meta["graph_hits"] >= 0


def test_hybrid_marks_hop2_inferred(tmp_path):
    import json
    from src.rag import hybrid_retrieve
    rels = [
        {"subject": "NorthStar Bank", "predicate": "approve", "object": "Loan X",
         "evidence": "Bank approved Loan X.", "document": "d.pdf", "page": 1, "confidence": 0.9},
        {"subject": "Loan X", "predicate": "fund", "object": "Project Y",
         "evidence": "Loan X funded Project Y.", "document": "d.pdf", "page": 1, "confidence": 0.8},
        {"subject": "Project Y", "predicate": "employ", "object": "Ravi Kumar",
         "evidence": "Project Y employs Ravi Kumar.", "document": "d.pdf", "page": 2, "confidence": 0.8},
    ]
    (tmp_path / "relationships.json").write_text(json.dumps(rels), encoding="utf-8")
    cfg = {"qdrant": {"enabled": False}, "ollama": {"top_k": 5}}
    facts, meta = hybrid_retrieve("Who approved Loan X?", cfg, store=None, out_dir=tmp_path)
    assert meta["hop2"] >= 1
    assert any(f.get("_inferred") for f in facts)
