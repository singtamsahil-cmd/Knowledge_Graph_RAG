"""Relationship validation + temporal attachment (pre-store quality gate).

- Drops evidence-less relations and merges exact duplicates (union evidence).
- Flags contradictions: same (subject, predicate, object) affirmed AND negated,
  or status conflicts (approved vs applied/under-review on the same target).
- Confidence tiers -> review_status: verified / candidate / uncertain /
  contradicted. Low-confidence items are KEPT but flagged, never silently lost.
- Attaches event_date from DATE mentions sharing the relation's evidence
  sentence (temporal info without new node types).
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict

log = logging.getLogger(__name__)

STATUS_APPROVED = {"approve", "approved", "grant", "granted", "accept", "accepted"}
STATUS_PENDING = {"apply", "applied", "review", "reviewed", "submit", "submitted",
                  "request", "requested", "consider"}


def _key(r) -> tuple:
    subj = (r.subject_id or r.subject_text or "").strip().lower()
    obj = (r.object_id or r.object_text or "").strip().lower()
    pred = (r.predicate or "").strip().lower()
    return (subj, pred, obj)


def _status_of(r) -> str | None:
    pred = (r.predicate or "").lower()
    first = pred.split()[0] if pred else ""
    if first in STATUS_APPROVED or "approv" in pred:
        return "approved"
    if first in STATUS_PENDING or "review" in pred:
        return "pending"
    return None


_DATE_RE = re.compile(
    r"\b\d{1,2}\s+(January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\s+\d{4}\b", re.IGNORECASE)


def attach_temporal(relations, entities) -> int:
    """Attach event_date from DATE mentions sharing the evidence sentence.

    DATE/TIME labels are dropped as graph nodes by default, so when no DATE
    entity matches, fall back to explicit calendar dates in the sentence
    ("5 February 2025"). Deterministic, language-scoped to EN month names.
    """
    by_sentence: dict[str, list[str]] = defaultdict(list)
    for e in entities:
        etype = getattr(e, "entity_type", "")
        if etype not in ("DATE", "TIME"):
            continue
        for s in getattr(e, "sentences", []) or []:
            for m in getattr(e, "mentions", []) or [getattr(e, "name", "")]:
                if m and m not in by_sentence[s]:
                    by_sentence[s].append(m)
    n = 0
    for r in relations:
        dates = by_sentence.get(getattr(r, "sentence", ""), [])
        if not dates:
            m = _DATE_RE.search(getattr(r, "sentence", "") or "")
            dates = [m.group(0)] if m else []
        if dates and not getattr(r, "event_date", None):
            r.event_date = dates[0]
            n += 1
    if n:
        log.info("Attached event dates to %d relations.", n)
    return n


def validate_relations(relations, min_confidence: float = 0.0) -> tuple[list, dict]:
    """Returns (kept_relations, report). Never raises on bad items."""
    report = {"dropped_no_evidence": 0, "duplicates_merged": 0,
              "contradicted": 0, "uncertain": 0, "verified": 0}
    # 1. drop evidence-less
    with_evidence = []
    for r in relations:
        if not (getattr(r, "sentence", "") or "").strip():
            report["dropped_no_evidence"] += 1
            continue
        with_evidence.append(r)
    # 2. merge exact duplicates (same endpoints+predicate+negation), union evidence
    seen: dict[tuple, object] = {}
    deduped: list = []
    for r in with_evidence:
        k = _key(r) + (bool(getattr(r, "negated", False)),)
        if k in seen:
            prev = seen[k]
            ev = getattr(r, "sentence", "")
            if ev and ev not in getattr(prev, "sentence", ""):
                pass  # evidence union happens at store; count the merge
            for attr in ("confidence",):
                try:
                    if float(getattr(r, attr, 0)) > float(getattr(prev, attr, 0)):
                        setattr(prev, attr, getattr(r, attr))
                except Exception:
                    pass
            report["duplicates_merged"] += 1
        else:
            seen[k] = r
            deduped.append(r)
    # 3. contradictions: same triple affirmed + negated
    groups: dict[tuple, list] = defaultdict(list)
    for r in deduped:
        groups[_key(r)].append(r)
    for members in groups.values():
        aff = [m for m in members if not getattr(m, "negated", False)]
        neg = [m for m in members if getattr(m, "negated", False)]
        if aff and neg:
            for m in members:
                m.review_status = "contradicted"
            report["contradicted"] += len(members)
    # status conflicts: same OBJECT approved in one fact, pending in another
    by_obj: dict[str, list] = defaultdict(list)
    for r in deduped:
        obj = (r.object_id or r.object_text or "").strip().lower()
        st = _status_of(r)
        if obj and st:
            by_obj[obj].append((st, r))
    for obj, items in by_obj.items():
        statuses = {s for s, _ in items}
        if {"approved", "pending"} <= statuses:
            for _, r in items:
                if getattr(r, "review_status", "candidate") != "contradicted":
                    r.review_status = "contradicted"
                    report["contradicted"] += 1
    # 4. confidence tiers (skip already-contradicted)
    for r in deduped:
        if getattr(r, "review_status", "candidate") == "contradicted":
            continue
        conf = float(getattr(r, "confidence", 0.5) or 0.5)
        linked = bool(getattr(r, "subject_id", None) and getattr(r, "object_id", None))
        if conf >= 0.8 and linked:
            r.review_status = "verified"
            report["verified"] += 1
        elif conf < min_confidence or not linked:
            r.review_status = "uncertain"
            report["uncertain"] += 1
        else:
            r.review_status = "candidate"
    log.info("Validation: %s", report)
    return deduped, report
