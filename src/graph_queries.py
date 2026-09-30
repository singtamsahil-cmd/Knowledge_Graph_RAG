"""Reusable read-only graph queries (parameterized Cypher)."""
from __future__ import annotations


def get_all_entities(session, database: str = "neo4j", limit: int = 1000):
    return list(session.run(
        "MATCH (e:Entity) RETURN e.id AS id, e.name AS name, "
        "e.entity_type AS entity_type LIMIT $limit", limit=limit))


def get_all_relationships(session, limit: int = 1000):
    return list(session.run(
        "MATCH (a:Entity)-[r]->(b:Entity) "
        "RETURN a.name AS subject, type(r) AS relation, r.predicate AS predicate, b.name AS object, "
        "r.relation_type AS relation_type, r.evidence AS evidence, "
        "r.source_document AS source_document, r.page_number AS page_number "
        "LIMIT $limit", limit=limit))


def search_entities_by_name(session, name: str, limit: int = 25):
    return list(session.run(
        "MATCH (e:Entity) WHERE toLower(e.name) CONTAINS toLower($name) "
        "RETURN e.id AS id, e.name AS name, e.entity_type AS entity_type LIMIT $limit",
        name=name, limit=limit))


def find_relationships_for_entity(session, entity_id: str):
    return list(session.run(
        "MATCH (e:Entity {id: $eid})-[r]-(other:Entity) "
        "RETURN e.name AS entity, type(r) AS rel_kind, r.predicate AS predicate, "
        "other.name AS other, r.evidence AS evidence", eid=entity_id))


def entity_neighborhood(session, entity_id: str, depth: int = 2):
    return list(session.run(
        "MATCH path = (e:Entity {id: $eid})-[r*1..2]-(n:Entity) "
        "RETURN [x IN nodes(path) | x.name] AS nodes, "
        "[x IN relationships(path) | x.predicate] AS predicates LIMIT 100",
        eid=entity_id))


def graph_for_document(session, document: str):
    return list(session.run(
        "MATCH (a:Entity)-[r]->(b:Entity) "
        "WHERE r.source_document = $doc OR $doc IN coalesce(r.source_documents, []) "
        "RETURN a.name AS subject, r.predicate AS predicate, b.name AS object, "
        "r.evidence AS evidence, r.page_number AS page_number", doc=document))


def list_documents(session):
    """One row per Document node with entity/relation counts. No LIMIT (usually few)."""
    return list(session.run(
        "MATCH (d:Document) "
        "RETURN d.name AS name, d.pages AS pages, "
        "size([(d)-[:CONTAINS]->(e:Entity) | e]) AS entities, "
        "size([(a:Entity)-[r]->(b:Entity) "
        "WHERE d.name IN coalesce(r.source_documents, [r.source_document]) | r]) AS relations "
        "ORDER BY name"))


def evidence_for_relationship(session, subject_id: str, object_id: str, predicate: str):
    return list(session.run(
        "MATCH (a:Entity {id: $sid})-[r {predicate: $pred}]->(b:Entity {id: $oid}) "
        "RETURN r.evidence AS evidence, r.source_documents AS documents, r.pages AS pages",
        sid=subject_id, oid=object_id, pred=predicate))
