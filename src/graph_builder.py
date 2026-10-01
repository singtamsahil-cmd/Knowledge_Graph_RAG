"""Assembles resolved entities + candidate relations into a graph (NetworkX optional)."""
from __future__ import annotations

import logging

from .entity_extractor import normalize_name
from .entity_resolver import ResolvedEntity, build_mention_index
from .relationship_extractor import CandidateRelation

log = logging.getLogger(__name__)


def link_relations_to_entities(relations: list[CandidateRelation],
                               resolved: list[ResolvedEntity]) -> list[CandidateRelation]:
    """Attach subject_id/object_id by matching endpoint text to resolved entities.

    Relations whose endpoints cannot be linked are kept but flagged (IDs None);
    pipeline may create placeholder entities for them.
    """
    index = build_mention_index(resolved)
    by_id = {e.id: e for e in resolved}
    # also allow lookup by normalized endpoint directly
    for r in relations:
        r.subject_id = index.get(normalize_name(r.subject_text))
        r.object_id = index.get(normalize_name(r.object_text))
    linked = sum(1 for r in relations if r.subject_id and r.object_id)
    log.info("Linked %d/%d relation endpoints to resolved entities.", linked, len(relations))
    return relations


def ensure_endpoint_entities(relations: list[CandidateRelation],
                             resolved: list[ResolvedEntity],
                             max_chars: int = 80) -> list[ResolvedEntity]:
    """Create minimal NOUN_PHRASE entities for unlinked endpoints so no evidence is lost.

    Production guard: skip numeric / over-long endpoints instead of
    materializing sentence-chunks as nodes. Such relations keep IDs None
    and are reported in stats (not silently dropped, not node-spammed).
    """
    from .entity_resolver import deterministic_id

    out = list(resolved)
    for r in relations:
        for text, attr in ((r.subject_text, "subject_id"), (r.object_text, "object_id")):
            if getattr(r, attr):
                continue
            norm = normalize_name(text)
            if not norm or len(text.strip()) > max_chars:
                continue
            if norm.replace(" ", "").replace(",", "").replace(".", "").isdigit():
                continue
            match = next((e for e in out if e.normalized_name == norm), None)
            if match is None:
                eid = deterministic_id(norm, "NOUN_PHRASE")
                match = ResolvedEntity(
                    id=eid, name=text, normalized_name=norm,
                    entity_type="NOUN_PHRASE", mentions=[text],
                    documents=[r.document], pages=[r.page_number],
                    sentences=[r.sentence],
                )
                out.append(match)
            else:
                if r.document not in match.documents:
                    match.documents.append(r.document)
                if r.page_number not in match.pages:
                    match.pages.append(r.page_number)
            setattr(r, attr, match.id)
    return out


def build_networkx_graph(resolved: list[ResolvedEntity],
                         relations: list[CandidateRelation]):
    """Optional intermediate NetworkX MultiDiGraph for analysis/export."""
    import networkx as nx

    g = nx.MultiDiGraph()
    for e in resolved:
        g.add_node(e.id, name=e.name, normalized_name=e.normalized_name,
                   entity_type=e.entity_type, documents=";".join(e.documents))
    for r in relations:
        if r.subject_id and r.object_id:
            g.add_edge(r.subject_id, r.object_id, predicate=r.predicate,
                       relation_type=r.relation_type, evidence=r.sentence,
                       document=r.document, page=r.page_number)
    return g


def graph_stats(resolved, relations) -> dict:
    from collections import Counter
    type_counts = dict(Counter(getattr(e, "entity_type", "?") for e in resolved))
    return {
        "num_entities": len(resolved),
        "num_relations": len(relations),
        "linked_relations": sum(1 for r in relations if r.subject_id and r.object_id),
        "unlinked_relations": sum(1 for r in relations if not (r.subject_id and r.object_id)),
        "entity_types": type_counts,
        "noun_phrase_ratio": round(type_counts.get("NOUN_PHRASE", 0) / max(len(resolved), 1), 3),
    }
