"""Conservative entity resolution: normalize, deterministic IDs, alias tracking.

Never merges ambiguous entities on similar names alone: exact normalized match
required by default; optional fuzzy threshold is opt-in and conservative.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .entity_extractor import ExtractedEntity, normalize_name

log = logging.getLogger(__name__)


@dataclass
class ResolvedEntity:
    id: str
    name: str  # canonical display name (first/longest mention)
    normalized_name: str
    entity_type: str
    mentions: list[str] = field(default_factory=list)
    alternative_names: list[str] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    sentences: list[str] = field(default_factory=list)


def deterministic_id(normalized: str, entity_type: str) -> str:
    h = hashlib.sha256(f"{entity_type}||{normalized}".encode("utf-8")).hexdigest()[:12]
    return f"entity_{h}"


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def resolve_entities(mentions: list[ExtractedEntity],
                     similarity_threshold: float = 0.85,
                     use_fuzzy: bool = False) -> list[ResolvedEntity]:
    """Group mentions into resolved entities.

    Default: exact (normalized_name, entity_type) match only — conservative.
    Fuzzy (use_fuzzy=True): additionally merges same-type names with similarity
    >= threshold AND sharing at least one non-stopword token.
    """
    groups: dict[tuple[str, str], ResolvedEntity] = {}
    for m in mentions:
        key = (m.normalized_name, m.entity_type)
        if key not in groups:
            groups[key] = ResolvedEntity(
                id=deterministic_id(m.normalized_name, m.entity_type),
                name=m.text,
                normalized_name=m.normalized_name,
                entity_type=m.entity_type,
            )
        g = groups[key]
        if m.text not in g.mentions:
            g.mentions.append(m.text)
        if m.document not in g.documents:
            g.documents.append(m.document)
        if m.page_number not in g.pages:
            g.pages.append(m.page_number)
        if m.sentence and m.sentence not in g.sentences:
            g.sentences.append(m.sentence)
        # canonical name: longest mention (most informative)
        if len(m.text) > len(g.name):
            if g.name not in g.alternative_names:
                g.alternative_names.append(g.name)
            g.name = m.text

    resolved = list(groups.values())

    if use_fuzzy:
        resolved = _fuzzy_merge(resolved, similarity_threshold)

    # ensure alternatives include all non-canonical mentions
    for g in resolved:
        for m in g.mentions:
            if m != g.name and m not in g.alternative_names:
                g.alternative_names.append(m)

    log.info("Resolved %d mentions into %d entities.", len(mentions), len(resolved))
    return resolved


_STOP = {"the", "a", "an", "of", "and", "in", "on", "for", "to", "its", "it", "is"}


def _content_tokens(name: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", name.lower()) if t not in _STOP}


def _fuzzy_merge(entities: list[ResolvedEntity], threshold: float) -> list[ResolvedEntity]:
    merged: list[ResolvedEntity] = []
    used = set()
    for i, a in enumerate(entities):
        if i in used:
            continue
        for j in range(i + 1, len(entities)):
            if j in used:
                continue
            b = entities[j]
            if a.entity_type != b.entity_type:
                continue
            if _similar(a.normalized_name, b.normalized_name) >= threshold:
                if _content_tokens(a.normalized_name) & _content_tokens(b.normalized_name):
                    # merge b into a
                    for m in b.mentions:
                        if m not in a.mentions:
                            a.mentions.append(m)
                    for d in b.documents:
                        if d not in a.documents:
                            a.documents.append(d)
                    for p in b.pages:
                        if p not in a.pages:
                            a.pages.append(p)
                    for s in b.sentences:
                        if s not in a.sentences:
                            a.sentences.append(s)
                    if len(b.name) > len(a.name):
                        if a.name not in a.alternative_names:
                            a.alternative_names.append(a.name)
                        a.name = b.name
                    elif b.name != a.name and b.name not in a.alternative_names:
                        a.alternative_names.append(b.name)
                    used.add(j)
        merged.append(a)
        used.add(i)
    return merged


def build_mention_index(resolved: list[ResolvedEntity]) -> dict[str, str]:
    """Map normalized mention -> entity id (for linking relation endpoints)."""
    index: dict[str, str] = {}
    for e in resolved:
        index[e.normalized_name] = e.id
        for alt in e.alternative_names + e.mentions:
            index.setdefault(normalize_name(alt), e.id)
    return index
