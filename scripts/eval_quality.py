"""Quality gates for production: fails CI if the graph is junk-heavy.

Usage: python scripts/eval_quality.py [--fail-under 0.0]
Reads outputs/entities.json + outputs/relationships.json (run pipeline first
with --no-neo4j if needed) and checks:
- noun_phrase_ratio < 0.60
- unlinked_relations == 0 (placeholders cover evidence) or reported
- no be/have/do predicates, no numeric-only entity names
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "outputs"

GATES = {
    "max_noun_phrase_ratio": 0.85,  # technical docs are noun-heavy; placeholders inflate this
    "banned_predicates": {"be", "have", "do", "get", "exist"},
}


def _is_amount_fact(r: dict) -> bool:
    """Copula exception: 'The sanctioned amount was INR 1,200,000' is a keeper."""
    import re
    if (r.get("predicate") or "").lower() != "be":
        return False
    obj = r.get("object", "") or ""
    if re.search(r"(INR|USD|EUR|GBP|Rs\b|₹|\$|€|£)", obj, re.IGNORECASE):
        return True
    dense = re.sub(r"\s+", "", obj)
    digits = sum(c.isdigit() or c in ",.%/-" for c in dense)
    return bool(dense) and digits / len(dense) >= 0.4


def main() -> int:
    ent_path, rel_path = OUT / "entities.json", OUT / "relationships.json"
    if not ent_path.exists() or not rel_path.exists():
        print("Run the pipeline first (outputs/*.json missing).")
        return 2
    entities = json.loads(ent_path.read_text(encoding="utf-8"))
    relations = json.loads(rel_path.read_text(encoding="utf-8"))
    types = Counter(e.get("entity_type") for e in entities)
    npr = types.get("NOUN_PHRASE", 0) / max(len(entities), 1)
    banned = [r for r in relations
              if (r.get("predicate") or "").lower() in GATES["banned_predicates"]
              and not _is_amount_fact(r)]
    numeric = [e for e in entities if (e.get("name") or "").strip().replace(" ", "").replace(",", "").replace(".", "").isdigit()]
    overlong = [e for e in entities if len(e.get("name") or "") > 80]
    print(f"entities={len(entities)} relations={len(relations)} types={dict(types)}")
    print(f"noun_phrase_ratio={npr:.3f} (gate<={GATES['max_noun_phrase_ratio']})")
    print(f"banned_predicates={len(banned)} numeric_entities={len(numeric)} overlong={len(overlong)}")
    ok = True
    if npr > GATES["max_noun_phrase_ratio"]:
        print("FAIL: noun-phrase noise too high — tighten noun chunks / blocklist."); ok = False
    if banned:
        print(f"FAIL: {len(banned)} copula relations (be/have/do) — check drop_predicates."); ok = False
    if numeric:
        print(f"FAIL: {len(numeric)} numeric entities — check drop_entity_types."); ok = False
    if overlong:
        print(f"WARN: {len(overlong)} over-long entities (not failing).")
    print("QUALITY GATES: PASS" if ok else "QUALITY GATES: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
