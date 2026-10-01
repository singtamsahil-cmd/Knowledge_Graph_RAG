"""CSV/JSON exports of extracted graph data."""
from __future__ import annotations

import csv
import json
from pathlib import Path


def export_all(entities, relations, out_dir: str | Path,
               export_csv: bool = True, export_json: bool = True,
               events=None) -> list[str]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    ent_rows = [dict(id=e.id, name=e.name, normalized_name=e.normalized_name,
                     entity_type=e.entity_type, mentions="|".join(e.mentions),
                     documents="|".join(e.documents)) for e in entities]
    rel_rows = [dict(subject_id=r.subject_id, subject=r.subject_text, predicate=r.predicate,
                     object=r.object_text, object_id=r.object_id,
                     relation_type=r.relation_type, evidence=r.sentence,
                     document=r.document, page=r.page_number,
                     method=r.extraction_method, status=r.review_status,
                     confidence=getattr(r, "confidence", 0.5),
                     event_date=getattr(r, "event_date", None),
                     negated=r.negated) for r in relations]
    if export_json:
        p = out_dir / "entities.json"
        p.write_text(json.dumps(ent_rows, indent=2, ensure_ascii=False), encoding="utf-8")
        written.append(str(p))
        p = out_dir / "relationships.json"
        p.write_text(json.dumps(rel_rows, indent=2, ensure_ascii=False), encoding="utf-8")
        written.append(str(p))
        if events is not None:
            ev_rows = [dict(id=v.id, name=v.name, event_type=v.event_type,
                            event_date=v.event_date,
                            documents="|".join(v.documents),
                            participants="|".join(
                                f"{x.get('entity_id')}:{x.get('role')}" for x in v.participants))
                       for v in events]
            p = out_dir / "events.json"
            p.write_text(json.dumps(ev_rows, indent=2, ensure_ascii=False), encoding="utf-8")
            written.append(str(p))
    if export_csv:
        for name, rows in (("entities.csv", ent_rows), ("relationships.csv", rel_rows)):
            p = out_dir / name
            if rows:
                with open(p, "w", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                    w.writeheader()
                    w.writerows(rows)
            else:
                p.write_text("", encoding="utf-8")
            written.append(str(p))
    return written
