"""Unit tests for per-document deletion (mocked driver — no live Neo4j required)."""
from unittest.mock import MagicMock

from src import graph_queries
from src.neo4j_store import Neo4jStore


def _mock_session():
    session = MagicMock()
    session.__enter__.return_value = session
    session.__exit__.return_value = False
    return session


def test_delete_document_runs_surgical_parameterized_queries():
    store = Neo4jStore(uri="bolt://x:7687", username="u", password="p",
                       max_retries=1, retry_delay=0)
    session = _mock_session()
    store._session = lambda: session

    result = store.delete_document("gone.pdf")

    # 5 statements: exclusive edges, shared-edge scrub, doc node,
    # entity scrub, orphan cleanup.
    assert session.run.call_count == 5
    for c in session.run.call_args_list:
        assert isinstance(c.args[0], str)  # Cypher text, never f-string values
    doc_scoped = [c for c in session.run.call_args_list if c.kwargs.get("doc") == "gone.pdf"]
    assert len(doc_scoped) == 4  # orphan cleanup is global by construction
    assert set(result) == {"relations_deleted", "relations_scrubbed",
                           "documents_deleted", "entities_scrubbed",
                           "orphan_entities_deleted"}


def test_list_documents_returns_rows():
    session = MagicMock()
    session.run.return_value = [{"name": "a.pdf", "entities": 3, "relations": 2}]
    rows = graph_queries.list_documents(session)
    assert rows == [{"name": "a.pdf", "entities": 3, "relations": 2}]
    assert isinstance(session.run.call_args.args[0], str)
