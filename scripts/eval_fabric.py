"""Before/after evaluation: eval/baseline/*.json vs outputs/*.json.

Metrics: counts, dup rates, provenance coverage, review-status mix,
contradictions, noun-phrase ratio, avg confidence. Writes eval/results.json.
"""
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def metrics(ent_path, rel_path):
    entities = json.loads(Path(ent_path).read_text(encoding="utf-8"))
    relations = json.loads(Path(rel_path).read_text(encoding="utf-8"))
    triples = [(r.get("subject_id") or r.get("subject"),
                r.get("predicate"), r.get("object_id") or r.get("object"))
               for r in relations]
    with_ev = [r for r in relations if r.get("evidence")]
    return {
        "entities": len(entities),
        "relations": len(relations),
        "entity_types": dict(Counter(e.get("entity_type") for e in entities)),
        "dup_relation_rate": round(1 - len(set(map(str, triples))) / max(len(triples), 1), 4),
        "provenance_coverage": round(len(with_ev) / max(len(relations), 1), 4),
        "review_status": dict(Counter(r.get("status", "?") for r in relations)),
        "contradicted": sum(1 for r in relations if r.get("status") == "contradicted"),
        "avg_confidence": round(sum(float(r.get("confidence", 0.5)) for r in relations)
                                / max(len(relations), 1), 3),
        "with_event_date": sum(1 for r in relations if r.get("event_date")),
    }


def main():
    base = metrics(ROOT / "eval/baseline/entities.json",
                   ROOT / "eval/baseline/relationships.json")
    now = metrics(ROOT / "outputs/entities.json",
                  ROOT / "outputs/relationships.json")
    out = {"baseline": base, "current": now,
           "delta_relations": now["relations"] - base["relations"],
           "delta_entities": now["entities"] - base["entities"]}
    (ROOT / "eval/results.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
