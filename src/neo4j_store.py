"""Neo4j persistence using the official driver + parameterized Cypher.

Schema (domain-independent — types come from grammar, never hardcoded domains):
  (:Document {name}) -[:CONTAINS]-> (:Entity {...})
  (:Entity)-[:APPROVE | :WORK_AT | :BORROW_FROM | ... {predicate, evidence[], ...}]->(:Entity)

Each relationship is stored under its own type derived from the extracted
predicate (e.g. "work at" -> :WORK_AT), so Neo4j Browser labels edges with the
real relation text. All values stay parameterized; only the sanitized type
name is embedded in the query string (Cypher cannot parameterize types).
"""
from __future__ import annotations

import logging
import re
import time

log = logging.getLogger(__name__)

CONSTRAINT_CYPHER = "CREATE CONSTRAINT entity_id_unique IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE"
DOC_CONSTRAINT = "CREATE CONSTRAINT document_name_unique IF NOT EXISTS FOR (d:Document) REQUIRE d.name IS UNIQUE"

MERGE_ENTITY = """
MERGE (e:Entity {id: $id})
ON CREATE SET e.name = $name, e.normalized_name = $norm,
              e.entity_type = $etype, e.mentions = $mentions,
              e.alternative_names = $alternatives,
              e.documents = $documents, e.pages = $pages
ON MATCH SET e.mentions = CASE WHEN e.mentions IS NULL THEN $mentions
                               ELSE [x IN e.mentions WHERE NOT x IN $mentions] + $mentions END,
             e.alternative_names = CASE WHEN e.alternative_names IS NULL THEN $alternatives
                               ELSE [x IN e.alternative_names WHERE NOT x IN $alternatives] + $alternatives END,
             e.documents = CASE WHEN e.documents IS NULL THEN $documents
                               ELSE [x IN e.documents WHERE NOT x IN $documents] + $documents END
"""

MERGE_DOCUMENT = """
MERGE (d:Document {name: $name})
ON CREATE SET d.pages = $pages
ON MATCH SET d.pages = CASE WHEN d.pages IS NULL THEN $pages
                            ELSE [x IN d.pages WHERE NOT x IN $pages] + $pages END
"""

LINK_DOC_ENTITY = """
MATCH (d:Document {name: $doc})
WITH d
MATCH (e:Entity {id: $eid})
MERGE (d)-[:CONTAINS]->(e)
"""

DELETE_EXCLUSIVE_RELATIONS = """
MATCH ()-[r]->()
WHERE $doc IN coalesce(r.source_documents, [r.source_document])
WITH r, [d IN coalesce(r.source_documents, [r.source_document])
         WHERE d IS NOT NULL AND d <> $doc] AS others
WHERE size(others) = 0
DELETE r
"""

SCRUB_SHARED_RELATIONS = """
MATCH ()-[r]->()
WHERE $doc IN coalesce(r.source_documents, [])
WITH r, [d IN coalesce(r.source_documents, []) WHERE d <> $doc] AS remaining
WHERE size(remaining) > 0
SET r.source_documents = remaining,
    r.source_document = CASE WHEN r.source_document = $doc
                             THEN remaining[0] ELSE r.source_document END
"""

DELETE_DOCUMENT_NODE = """
MATCH (d:Document {name: $doc})
DETACH DELETE d
"""

SCRUB_ENTITIES = """
MATCH (e:Entity)
WHERE $doc IN coalesce(e.documents, [])
SET e.documents = [d IN coalesce(e.documents, []) WHERE d <> $doc]
"""

DELETE_ORPHAN_ENTITIES = """
MATCH (e:Entity)
WHERE size(coalesce(e.documents, [])) = 0 AND NOT (e)--()
DETACH DELETE e
"""

def sanitize_rel_type(relation_type: str | None) -> str:
    """Turn a normalized predicate (e.g. "WORK_AT") into a safe Neo4j type name.

    Guarantees `^[A-Z][A-Z0-9_]*$` so the name can be embedded in Cypher.
    """
    name = re.sub(r"[^A-Za-z0-9_]+", "_", (relation_type or "").strip().upper()).strip("_")
    if not name:
        return "RELATED_TO"
    if not name[0].isalpha():
        name = "REL_" + name
    return name


def merge_relation_query(rel_type: str) -> str:
    """Build the MERGE query for one relationship type.

    The type is backtick-quoted and always passes through sanitize_rel_type,
    so no user/document text can ever reach the query string — values stay
    fully parameterized.
    """
    t = sanitize_rel_type(rel_type)
    return f"""
MATCH (a:Entity {{id: $sid}}), (b:Entity {{id: $oid}})
MERGE (a)-[r:`{t}` {{predicate: $predicate, negated: $negated}}]->(b)
ON CREATE SET r.relation_type = $rtype, r.evidence = [$evidence],
              r.source_document = $doc, r.source_documents = [$doc],
              r.page_number = $page, r.pages = [$page],
              r.extraction_method = $method, r.review_status = $status
ON MATCH SET r.evidence = CASE WHEN $evidence IN coalesce(r.evidence, []) THEN coalesce(r.evidence, [])
                               ELSE coalesce(r.evidence, []) + [$evidence] END,
             r.source_documents = CASE WHEN $doc IN coalesce(r.source_documents, []) THEN coalesce(r.source_documents, [])
                               ELSE coalesce(r.source_documents, []) + [$doc] END,
             r.pages = CASE WHEN $page IN coalesce(r.pages, []) THEN coalesce(r.pages, [])
                               ELSE coalesce(r.pages, []) + [$page] END
"""


class Neo4jStore:
    def __init__(self, uri: str, username: str, password: str, database: str = "neo4j",
                 batch_size: int = 500, max_retries: int = 10, retry_delay: float = 3.0):
        self.uri = uri
        self.auth = (username, password)
        self.database = database
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._driver = None

    def connect(self):
        from neo4j import GraphDatabase

        last: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                self._driver = GraphDatabase.driver(self.uri, auth=self.auth)
                self._driver.verify_connectivity()
                log.info("Connected to Neo4j at %s (attempt %d).", self.uri, attempt)
                return self
            except Exception as exc:
                last = exc
                log.warning("Neo4j connection attempt %d/%d failed: %s",
                            attempt, self.max_retries, exc)
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay)
        raise ConnectionError(f"Could not connect to Neo4j at {self.uri}: {last}")

    def verify(self) -> bool:
        try:
            if self._driver is None:
                self.connect()
            self._driver.verify_connectivity()
            return True
        except Exception as exc:
            log.error("Neo4j verification failed: %s", exc)
            return False

    def close(self):
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *args):
        self.close()

    def init_db(self):
        with self._driver.session(database=self.database) as session:
            session.run(CONSTRAINT_CYPHER).consume()
            session.run(DOC_CONSTRAINT).consume()
        log.info("Neo4j constraints ensured.")

    def _session(self):
        return self._driver.session(database=self.database)

    def store(self, entities, relations) -> dict:
        self.init_db()
        n_nodes = self._store_entities(entities)
        n_rels = self._store_relations(relations)
        self._link_documents(entities)
        log.info("Wrote %d entities and %d relations to Neo4j.", n_nodes, n_rels)
        return {"entities_written": n_nodes, "relations_written": n_rels}

    def _store_entities(self, entities) -> int:
        count = 0
        with self._session() as session:
            batch = []
            def flush(tx_batch):
                nonlocal count
                for e in tx_batch:
                    session.run(MERGE_ENTITY, id=e.id, name=e.name, norm=e.normalized_name,
                                etype=e.entity_type, mentions=e.mentions or [e.name],
                                alternatives=e.alternative_names or [],
                                documents=e.documents or [], pages=e.pages or [])
                    session.run(MERGE_DOCUMENT, name=e.documents[0] if e.documents else "unknown",
                                pages=e.pages or [])
                    count += 1
            for e in entities:
                batch.append(e)
                if len(batch) >= self.batch_size:
                    flush(batch)
                    batch = []
            if batch:
                flush(batch)
        return count

    def _store_relations(self, relations) -> int:
        count = 0
        query_cache: dict[str, str] = {}
        with self._session() as session:
            for r in relations:
                if not r.subject_id or not r.object_id:
                    continue
                rel_type = sanitize_rel_type(r.relation_type)
                query = query_cache.get(rel_type)
                if query is None:
                    query = merge_relation_query(rel_type)
                    query_cache[rel_type] = query
                session.run(query, sid=r.subject_id, oid=r.object_id,
                            predicate=r.predicate, rtype=r.relation_type,
                            evidence=r.sentence, doc=r.document, page=r.page_number,
                            method=r.extraction_method, status=r.review_status,
                            negated=r.negated)
                count += 1
        return count

    def _link_documents(self, entities):
        with self._session() as session:
            for e in entities:
                for d in e.documents:
                    try:
                        session.run(MERGE_DOCUMENT, name=d, pages=e.pages or [])
                        session.run(LINK_DOC_ENTITY, doc=d, eid=e.id)
                    except Exception as exc:
                        log.warning("Doc-link failed %s -> %s: %s", d, e.id, exc)

    def delete_document(self, doc_name: str) -> dict:
        """Remove one document and everything that came only from it.

        - Deletes relationship edges exclusive to this document.
        - Scrubs the document from shared edges/entities (shared data survives).
        - Deletes the Document node and orphan entities left with no
          documents and no relations.
        Returns counts of what was removed.
        """
        counts: dict[str, int] = {}
        with self._session() as session:
            res = session.run(DELETE_EXCLUSIVE_RELATIONS, doc=doc_name).consume()
            counts["relations_deleted"] = res.counters.relationships_deleted
            res = session.run(SCRUB_SHARED_RELATIONS, doc=doc_name).consume()
            counts["relations_scrubbed"] = res.counters.properties_set
            res = session.run(DELETE_DOCUMENT_NODE, doc=doc_name).consume()
            counts["documents_deleted"] = res.counters.nodes_deleted
            res = session.run(SCRUB_ENTITIES, doc=doc_name).consume()
            counts["entities_scrubbed"] = res.counters.properties_set
            res = session.run(DELETE_ORPHAN_ENTITIES).consume()
            counts["orphan_entities_deleted"] = res.counters.nodes_deleted
        log.warning("Deleted document '%s' from Neo4j: %s", doc_name, counts)
        return counts

    def clear_app_data(self):
        """Explicit, safe clear of application-managed data only. Must be called deliberately."""
        with self._session() as session:
            session.run("MATCH (e:Entity)-[r]-() DELETE r").consume()
            session.run("MATCH (e:Entity) DETACH DELETE e").consume()
            session.run("MATCH (d:Document) DETACH DELETE d").consume()
        log.warning("Cleared application-managed Neo4j data (Entity/Document/relationships).")
