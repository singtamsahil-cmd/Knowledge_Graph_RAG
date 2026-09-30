"""Pipeline + Neo4j store logic (mocked driver — no live Neo4j required)."""
from unittest.mock import MagicMock

from src.graph_builder import ensure_endpoint_entities, link_relations_to_entities
from src.relationship_extractor import CandidateRelation


def test_link_and_placeholder_entities():
    from src.entity_extractor import ExtractedEntity
    from src.entity_resolver import resolve_entities

    mentions = [ExtractedEntity(text="ABC University", normalized_name="abc university",
                                entity_type="ORG", document="d.pdf", page_number=1, sentence="s")]
    resolved = resolve_entities(mentions)
    rels = [CandidateRelation(subject_text="ABC University", predicate="appointed",
                              object_text="Dr. Sharma", relation_type="APPOINT",
                              sentence="s", document="d.pdf", page_number=1)]
    link_relations_to_entities(rels, resolved)
    assert rels[0].subject_id is not None
    assert rels[0].object_id is None
    resolved2 = ensure_endpoint_entities(rels, resolved)
    assert rels[0].object_id is not None
    assert len(resolved2) == 2


def test_neo4j_store_uses_parameterized_queries():
    from src.neo4j_store import Neo4jStore

    store = Neo4jStore(uri="bolt://x:7687", username="u", password="p", max_retries=1, retry_delay=0)
    session = MagicMock()
    session.__enter__.return_value = session
    session.__exit__.return_value = False
    store._session = lambda: session

    from src.entity_resolver import ResolvedEntity
    ents = [ResolvedEntity(id="entity_1", name="A", normalized_name="a", entity_type="ORG",
                           mentions=["A"], documents=["d.pdf"], pages=[1])]
    rels = [CandidateRelation(subject_text="A", predicate="bought", object_text="B",
                              relation_type="BOUGHT", sentence="A bought B.",
                              document="d.pdf", page_number=1,
                              subject_id="entity_1", object_id="entity_2")]
    store._store_entities(ents)
    store._store_relations(rels)
    assert session.run.call_count >= 2
    # verify parameterized (query string + kwargs, no f-string interpolation of values)
    for call in session.run.call_args_list:
        assert isinstance(call.args[0], str)
        assert call.kwargs, "Cypher must be parameterized"


def test_sanitize_rel_type():
    from src.neo4j_store import sanitize_rel_type

    assert sanitize_rel_type("WORK_AT") == "WORK_AT"
    assert sanitize_rel_type("borrow from") == "BORROW_FROM"
    assert sanitize_rel_type("") == "RELATED_TO"
    assert sanitize_rel_type(None) == "RELATED_TO"
    assert sanitize_rel_type("2022") == "REL_2022"
    # injection attempt collapses to safe characters only
    evil = sanitize_rel_type('X`) MATCH (n) DETACH DELETE n //')
    assert "`" not in evil and " " not in evil
    import re
    assert re.fullmatch(r"[A-Z][A-Z0-9_]*", evil)


def test_store_relations_uses_predicate_edge_type():
    from src.neo4j_store import Neo4jStore

    store = Neo4jStore(uri="bolt://x:7687", username="u", password="p", max_retries=1, retry_delay=0)
    session = MagicMock()
    session.__enter__.return_value = session
    session.__exit__.return_value = False
    store._session = lambda: session

    rels = [CandidateRelation(subject_text="A", predicate="work at", object_text="B",
                              relation_type="WORK_AT", sentence="A works at B.",
                              document="d.pdf", page_number=1,
                              subject_id="entity_1", object_id="entity_2")]
    store._store_relations(rels)
    assert session.run.call_count == 1
    query = session.run.call_args.args[0]
    assert "-[r:`WORK_AT`" in query  # Browser will label the edge WORK_AT
    assert session.run.call_args.kwargs["predicate"] == "work at"  # values stay parameterized
