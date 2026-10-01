"""Entity resolution: canonical normalization + alias + fuzzy merging.

Stages (in order, safe -> risky):
1. Exact canonical match (normalize_name strips articles, honorifics,
   possessives, corp suffixes) — always.
2. Alias merge (always): subset tokens ("Priya" -> "Priya Singh"),
   acronyms ("NSF" -> "National Science Foundation"), NOUN_PHRASE <-> typed.
3. Fuzzy merge (opt-in use_fuzzy): rapidfuzz token_set_ratio.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field

from .entity_extractor import ExtractedEntity, normalize_name

log = logging.getLogger(__name__)


@dataclass
class ResolvedEntity:
    id: str
    name: str  # canonical display name (most informative mention)
    normalized_name: str  # canonical normalized form
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
    """0..1 string similarity. rapidfuzz if available, else difflib."""
    try:
        from rapidfuzz import fuzz  # type: ignore

        # max of plain ratio + token_set (handles "Bank ABC" vs "ABC Bank Ltd")
        return max(
            fuzz.ratio(a, b),
            fuzz.token_set_ratio(a, b),
        ) / 100.0
    except Exception:
        from difflib import SequenceMatcher

        return SequenceMatcher(None, a, b).ratio()


_STOP = {"the", "a", "an", "of", "and", "in", "on", "for", "to", "its", "it", "is", "at", "by"}

# Single generic tokens that must NOT merge by subset alone
# ("bank" alone should not auto-merge into "ABC Bank").
_GENERIC_SINGLETONS = {
    "bank", "company", "center", "centre", "university", "hospital",
    "court", "group", "team", "study", "paper", "system", "department",
    "agency", "firm", "corporation", "association", "institute", "school",
    "ministry", "committee", "board", "report", "project", "program",
}


def _content_tokens(name: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", name.lower()) if t not in _STOP}


def _compatible_types(a: str, b: str) -> bool:
    if a == b:
        return True
    # NOUN_PHRASE is untyped -> may merge into any typed entity
    if "NOUN_PHRASE" in (a, b):
        return True
    return False


def _is_acronym(short: str, long: str) -> bool:
    s = re.sub(r"[^a-z0-9]", "", short.lower())
    ltokens = [t for t in re.findall(r"[a-z0-9]+", long.lower()) if t not in _STOP]
    if not (2 <= len(s) <= 6) or len(ltokens) < 2:
        return False
    initials = "".join(t[0] for t in ltokens if t)
    if s == initials:
        return True
    # dotted / partial: "U S" vs "united states"
    nospace = re.sub(r"\s+", "", short.lower())
    if len(nospace) <= 6 and nospace == initials[: len(nospace)] and len(nospace) >= 2:
        return True
    return False


def _is_alias(a: ResolvedEntity, b: ResolvedEntity) -> bool:
    """Safe deterministic alias: subset tokens or acronym, compatible types."""
    if not _compatible_types(a.entity_type, b.entity_type):
        return False
    # Production guard: never subset-merge long noisy phrases (sentence chunks).
    if len(a.normalized_name) > 60 or len(b.normalized_name) > 60:
        return False
    if len(a.normalized_name.split()) > 6 or len(b.normalized_name.split()) > 6:
        return False
    ta, tb = _content_tokens(a.normalized_name), _content_tokens(b.normalized_name)
    if not ta or not tb:
        return False
    # subset: "priya" ⊂ "priya singh", "sharma" ⊂ "dr sharma" (already normalized)
    if ta <= tb or tb <= ta:
        shorter = ta if len(ta) < len(tb) else tb
        if len(shorter) == 1 and next(iter(shorter)) in _GENERIC_SINGLETONS:
            pass  # don't merge generic singletons by subset alone
        else:
            return True
    # acronym either direction
    if _is_acronym(a.normalized_name, b.normalized_name) or _is_acronym(
        b.normalized_name, a.normalized_name
    ):
        return True
    return False


def _merge_into(target: ResolvedEntity, source: ResolvedEntity) -> None:
    for m in source.mentions:
        if m not in target.mentions:
            target.mentions.append(m)
    for d in source.documents:
        if d not in target.documents:
            target.documents.append(d)
    for p in source.pages:
        if p not in target.pages:
            target.pages.append(p)
    for s in source.sentences:
        if s and s not in target.sentences:
            target.sentences.append(s)
    for alt in [source.name, *source.alternative_names]:
        if alt != target.name and alt not in target.alternative_names:
            target.alternative_names.append(alt)
    # canonical type: prefer real type over NOUN_PHRASE
    if target.entity_type == "NOUN_PHRASE" and source.entity_type != "NOUN_PHRASE":
        target.entity_type = source.entity_type
    # canonical name: prefer typed + longest informative mention
    cand = max([target.name, source.name], key=len)
    if cand != target.name:
        if target.name not in target.alternative_names:
            target.alternative_names.append(target.name)
        target.name = cand


def _merge_pass(
    entities: list[ResolvedEntity], should_merge,
) -> list[ResolvedEntity]:
    merged: list[ResolvedEntity] = []
    used: set[int] = set()
    for i, a in enumerate(entities):
        if i in used:
            continue
        for j in range(i + 1, len(entities)):
            if j in used:
                continue
            b = entities[j]
            if should_merge(a, b):
                _merge_into(a, b)
                used.add(j)
        merged.append(a)
        used.add(i)
    # refresh deterministic IDs on final canonical form
    for e in merged:
        e.id = deterministic_id(e.normalized_name, e.entity_type)
    return merged


def _alias_merge(entities: list[ResolvedEntity]) -> list[ResolvedEntity]:
    # longest first so short mentions merge INTO the informative entity
    entities = sorted(entities, key=lambda e: len(e.normalized_name), reverse=True)
    out: list[ResolvedEntity] = []
    for e in entities:
        placed = False
        for o in out:
            if _is_alias(o, e):
                # keep the longer/canonical as target
                target, src = (o, e) if len(o.normalized_name) >= len(e.normalized_name) else (e, o)
                if target is o:
                    _merge_into(o, e)
                else:
                    _merge_into(e, o)
                    out.remove(o)
                    out.append(e)
                placed = True
                break
        if not placed:
            out.append(e)
    for e in out:
        e.id = deterministic_id(e.normalized_name, e.entity_type)
    return out


def _fuzzy_merge(entities: list[ResolvedEntity], threshold: float) -> list[ResolvedEntity]:
    def should_merge(a: ResolvedEntity, b: ResolvedEntity) -> bool:
        if not _compatible_types(a.entity_type, b.entity_type):
            return False
        if _similar(a.normalized_name, b.normalized_name) < threshold:
            return False
        # require shared content token to avoid "Acme Corp" <-> "Apex Corp"
        if not (_content_tokens(a.normalized_name) & _content_tokens(b.normalized_name)):
            return False
        return True

    return _merge_pass(entities, should_merge)


def _canon(m: ExtractedEntity) -> str:
    c = normalize_name(m.text)
    if not c:
        c = normalize_name(m.normalized_name or "")
    return c or (m.normalized_name or "").strip().lower()


def resolve_entities(mentions: list[ExtractedEntity],
                     similarity_threshold: float = 0.85,
                     use_fuzzy: bool = False) -> list[ResolvedEntity]:
    """Group mentions into resolved entities.

    1. Exact canonical match (always).
    2. Alias merge (always): subset/acronym + NOUN_PHRASE bridging.
    3. Fuzzy merge (if use_fuzzy): rapidfuzz token_set_ratio >= threshold.
    """
    groups: dict[tuple[str, str], ResolvedEntity] = {}
    for m in mentions:
        canon = _canon(m)
        key = (canon, m.entity_type)
        if key not in groups:
            groups[key] = ResolvedEntity(
                id=deterministic_id(canon, m.entity_type),
                name=m.text,
                normalized_name=canon,
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
        if len(m.text) > len(g.name):
            if g.name not in g.alternative_names:
                g.alternative_names.append(g.name)
            g.name = m.text

    resolved = list(groups.values())
    resolved = _alias_merge(resolved)

    if use_fuzzy:
        resolved = _fuzzy_merge(resolved, similarity_threshold)

    for g in resolved:
        for m in g.mentions:
            if m != g.name and m not in g.alternative_names:
                g.alternative_names.append(m)

    log.info("Resolved %d mentions into %d entities.", len(mentions), len(resolved))
    return resolved


def build_mention_index(resolved: list[ResolvedEntity]) -> dict[str, str]:
    """Map mention -> entity id (canonical + raw lower, for relation linking).

    Also indexes bare ID codes found inside mentions ("Current Account CA20001"
    -> "ca20001"), so code-only endpoints link to the right node.
    """
    import re as _re

    _code = _re.compile(r"\b[A-Z]{2,}[-\/]?\d[\w-]*\b")
    index: dict[str, str] = {}
    for e in resolved:
        index[e.normalized_name] = e.id
        for alt in e.alternative_names + e.mentions:
            if not alt:
                continue
            index.setdefault(normalize_name(alt), e.id)
            index.setdefault(alt.strip().lower(), e.id)
            for code in _code.findall(alt):
                index.setdefault(code.lower(), e.id)
    return index
