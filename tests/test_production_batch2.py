"""Tests for the production batch: dedup, confidence, incremental manifest."""
from pathlib import Path

from src.pdf_processor import PageSegment, _strip_boilerplate
from src.relationship_extractor import score_relation


def test_boilerplate_strips_repeated_headers():
    header = "Fictional Bank Annual Operations Report 2024 - Confidential"
    segs = [PageSegment(document="d.pdf", page_number=i + 1,
                        text=f"{header}\nUnique content page {i}")
            for i in range(4)]
    out = _strip_boilerplate(segs, min_repeat=3, min_chars=20)
    assert all(header not in s.text for s in out)
    assert all(f"Unique content page {i}" in s.text for i, s in enumerate(out))


def test_confidence_orders_specific_over_vague():
    good = score_relation("Rahul Sharma", "work at", "ABC Bank")
    vague = score_relation("a very long and vague descriptive phrase with many extra words here",
                           "transfer from", "another very long vague phrase with many words inside it",
                           via_prep=True)
    assert 0.0 <= vague <= 1.0
    assert good > vague


def test_incremental_manifest_skips_unchanged(tmp_path):
    import json
    from src import pipeline as pl

    out = tmp_path / "outputs"
    out.mkdir()
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-fake-content")
    (out / pl.MANIFEST_NAME).write_text(
        json.dumps({str(pdf.resolve()): pl._file_fingerprint(pdf)}), encoding="utf-8")
    manifest = pl._load_manifest(out)
    assert manifest.get(str(pdf.resolve())) == pl._file_fingerprint(pdf)


def test_config_change_invalidates_manifest():
    from src import pipeline as pl

    a = {"extraction": {"max_noun_chunk_tokens": 4}, "spacy_model": "en_core_web_lg"}
    b = {"extraction": {"max_noun_chunk_tokens": 3}, "spacy_model": "en_core_web_lg"}
    assert pl._config_fingerprint(a) != pl._config_fingerprint(b)
    assert pl._config_fingerprint(a) == pl._config_fingerprint(dict(a))


def test_api_imports_without_server():
    import src.api as api
    routes = {r.path for r in api.app.routes}
    assert {"/healthz", "/readyz", "/metrics", "/ingest"} <= routes
    assert api.healthz() == {"status": "ok"}
