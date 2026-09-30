"""Optional integration test — requires live Neo4j (skipped by default).

Run with:  RUN_NEO4J_TESTS=1 pytest tests/test_neo4j_integration.py
"""
import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_NEO4J_TESTS") != "1",
                                reason="Set RUN_NEO4J_TESTS=1 to run live Neo4j tests.")


def test_live_roundtrip():
    from src.entity_resolver import ResolvedEntity
    from src.neo4j_store import Neo4jStore
    from src.relationship_extractor import CandidateRelation
    from src import config as config_mod

    cfg = config_mod.load_config()
    n = cfg["neo4j"]
    store = Neo4jStore(n["uri"], n["username"], n["password"], n.get("database", "neo4j"),
                       max_retries=2, retry_delay=1)
    store.connect()
    try:
        store.init_db()
        ents = [ResolvedEntity(id="entity_test_a", name="Test Org", normalized_name="test org",
                               entity_type="ORG", mentions=["Test Org"], documents=["t.pdf"], pages=[1])]
        store.store(ents, [])
        assert store.verify()
    finally:
        store.close()
