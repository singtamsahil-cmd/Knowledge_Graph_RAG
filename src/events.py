"""First-class Event nodes (gated by config `events.enabled`, default off).

An Event is created only when a relation carries all three:
an event_date (DATE mention in the evidence sentence), both endpoints linked,
and confidence >= threshold. The original relation is KEPT — the event adds
n-ary structure (participants, roles, date, status), it never replaces facts.

Status changes are preserved: re-ingest merges on the deterministic event ID
and unions documents/evidence instead of overwriting.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class Event:
    id: str
    name: str
    event_type: str  # verb lemma, e.g. "approve", "transfer"
    event_date: str
    documents: list[str] = field(default_factory=list)
    sentences: list[str] = field(default_factory=list)
    participants: list[dict] = field(default_factory=list)  # {entity_id, role}
    source_relation: str = ""  # predicate of the triggering relation


def _event_id(predicate: str, date: str, subj: str, obj: str) -> str:
    raw = f"{predicate}||{date}||{subj}||{obj}".encode("utf-8", "ignore")
    return "event_" + hashlib.sha1(raw).hexdigest()[:12]


def build_events(relations, min_confidence: float = 0.6,
                 max_events: int = 500) -> list[Event]:
    events: list[Event] = []
    for r in relations:
        date = getattr(r, "event_date", None)
        sid, oid = getattr(r, "subject_id", None), getattr(r, "object_id", None)
        conf = float(getattr(r, "confidence", 0.5) or 0.5)
        if not (date and sid and oid and conf >= min_confidence):
            continue
        etype = (getattr(r, "predicate", "") or "").split()[0].lower() or "event"
        subj = getattr(r, "subject_text", "")
        obj = getattr(r, "object_text", "")
        eid = _event_id(etype, date, subj, obj)
        events.append(Event(
            id=eid, name=f"{etype} on {date}", event_type=etype,
            event_date=date, documents=[getattr(r, "document", "")],
            sentences=[getattr(r, "sentence", "")],
            participants=[{"entity_id": sid, "role": "subject"},
                          {"entity_id": oid, "role": "object"}],
            source_relation=getattr(r, "relation_type", "")))
        if len(events) >= max_events:
            break
    if events:
        log.info("Built %d event nodes.", len(events))
    return events
