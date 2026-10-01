"""End-to-end pipeline: PDF -> NLP -> entities -> relations -> resolve -> Neo4j."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from . import config as config_mod
from .entity_extractor import extract_entities
from .entity_resolver import resolve_entities
from .exporter import export_all
from .graph_builder import (build_networkx_graph, ensure_endpoint_entities,
                            graph_stats, link_relations_to_entities)
from .neo4j_store import Neo4jStore
from .nlp_processor import process_segments_nlp
from .pdf_processor import collect_pdfs, process_pdfs
from .relationship_extractor import extract_relationships

log = logging.getLogger(__name__)

MANIFEST_NAME = ".ingest_manifest.json"
DEFAULT_PASSWORDS = {"password", "change_this_password", "neo4j"}


def _file_fingerprint(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_manifest(out_dir: Path) -> dict:
    try:
        return json.loads((out_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_manifest(out_dir: Path, manifest: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _config_fingerprint(cfg: dict) -> str:
    """Hash of settings that affect output shape. Config change => reprocess."""
    import hashlib as _hl
    import json as _js

    shape = {
        "spacy_model": cfg.get("spacy_model"),
        "batch_size": cfg.get("batch_size"),
        "ocr_warning_threshold_chars": cfg.get("ocr_warning_threshold_chars"),
        "extraction": cfg.get("extraction"),
        "entity_resolution": cfg.get("entity_resolution"),
        "coref": cfg.get("coref"),
        "ner": cfg.get("ner"),
    }
    return _hl.sha1(_js.dumps(shape, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _maybe_coref_note(cfg: dict) -> None:
    """Optional coreference hook: uses fastcoref/GLiNER only if installed.

    Keeps torch/heavy models optional so the default Docker image stays slim.
    Enable with: pip install fastcoref gliner ; coref.enabled=true in config.
    """
    coref_cfg = cfg.get("coref", {})
    if not coref_cfg.get("enabled"):
        return
    try:
        import fastcoref  # noqa: F401
        log.info("Coref enabled (fastcoref available).")
    except Exception:
        log.warning("coref.enabled=true but fastcoref not installed — continuing without coreference.")


def run_pipeline(pdf: str | None = None, input_dir: str | None = None,
                 cfg: dict | None = None, write_to_neo4j: bool = True,
                 export: bool = True, force: bool = False) -> dict:
    cfg = cfg or config_mod.load_config()
    model = cfg.get("spacy_model", "en_core_web_lg")
    batch_size = cfg.get("batch_size", 32)
    ocr_threshold = cfg.get("ocr_warning_threshold_chars", 50)
    ext = cfg.get("extraction", {})
    resol_cfg = cfg.get("entity_resolution", {})
    out_cfg = cfg.get("outputs", {})
    out_dir = Path(out_cfg.get("dir", "outputs"))

    ncfg = cfg.get("neo4j", {})
    if str(ncfg.get("password", "")) in DEFAULT_PASSWORDS:
        log.warning("Neo4j is using a default password — set a strong NEO4J_PASSWORD in .env for any shared host.")

    _maybe_coref_note(cfg)

    paths = collect_pdfs(pdf, input_dir)
    if not paths:
        raise FileNotFoundError("No PDF files found to process.")

    # Incremental ingest: skip files whose content hash is unchanged
    # AND whose config fingerprint matches (filter change => reprocess).
    manifest = _load_manifest(out_dir)
    cfg_fp = _config_fingerprint(cfg)
    if manifest.get("_config") != cfg_fp:
        if manifest and not force:
            log.info("Config changed since last ingest — reprocessing all PDFs.")
        manifest = {"_config": cfg_fp}
    if not force:
        fresh: list[Path] = []
        skipped = 0
        for p in paths:
            try:
                fp = _file_fingerprint(p)
            except Exception:
                fresh.append(p)
                continue
            if manifest.get(str(p.resolve())) == fp:
                skipped += 1
            else:
                fresh.append(p)
        if skipped and not fresh:
            log.info("All %d PDF(s) unchanged since last ingest — skipping. Use force=True to reprocess.", skipped)
            return {"stats": {"skipped_unchanged": skipped}, "entities": [], "relations": [],
                    "pdf_results": [], "exported": []}
        if skipped:
            log.info("Skipping %d unchanged PDF(s); processing %d.", skipped, len(fresh))
        paths = fresh
    log.info("Found %d PDF(s) to process.", len(paths))

    segments, pdf_results = process_pdfs(paths, ocr_threshold=ocr_threshold)
    if not segments:
        raise RuntimeError("No pages extracted from input PDFs.")

    segments_with_docs = list(process_segments_nlp(segments, model=model, batch_size=batch_size))

    entities = extract_entities(
        segments_with_docs,
        use_noun_chunks=ext.get("use_noun_chunks", True),
        min_chunk_tokens=ext.get("min_noun_chunk_tokens", 1),
        max_chunk_tokens=ext.get("max_noun_chunk_tokens", 4),
        max_chars=int(ext.get("max_entity_chars", 80)),
        drop_types=set(ext.get("drop_entity_types", ["CARDINAL", "ORDINAL", "QUANTITY", "PERCENT", "DATE", "TIME", "MONEY"])),
    )
    relations = extract_relationships(
        segments_with_docs,
        drop_predicates=set(p.lower() for p in ext.get("drop_predicates", ["be", "have", "do", "get", "exist"])),
    )

    resolved = resolve_entities(
        entities,
        similarity_threshold=float(resol_cfg.get("similarity_threshold", 0.85)),
        use_fuzzy=bool(resol_cfg.get("use_fuzzy", False)),
    )
    link_relations_to_entities(relations, resolved)
    resolved = ensure_endpoint_entities(relations, resolved)
    # re-link after placeholders created
    link_relations_to_entities(relations, resolved)

    from .validate import attach_temporal, validate_relations
    attach_temporal(relations, entities)
    vcfg = cfg.get("validation", {})
    relations, validation_report = validate_relations(
        relations, min_confidence=float(vcfg.get("min_confidence", 0.0)))

    from .events import build_events
    ecfg = cfg.get("events", {})
    events = build_events(relations,
                          min_confidence=float(ecfg.get("min_confidence", 0.6)),
                          max_events=int(ecfg.get("max_events", 500))) \
        if str(ecfg.get("enabled", False)).lower() in ("true", "1", "yes") else []

    stats = graph_stats(resolved, relations)
    confs = [float(getattr(r, "confidence", 0.5)) for r in relations]
    stats.update({
        "num_pdfs": len(pdf_results),
        "num_pages": sum(r.num_pages for r in pdf_results),
        "empty_pages": sum(r.empty_pages for r in pdf_results),
        "needs_ocr": sorted({r.path for r in pdf_results if getattr(r, "needs_ocr", False)}),
        "num_mentions": len(entities),
        "avg_confidence": round(sum(confs) / len(confs), 3) if confs else 0.0,
        "validation": validation_report,
    })

    # Qdrant vector index (best-effort sidecar; Neo4j stays source of truth)
    qcfg = cfg.get("qdrant", {})
    if str(qcfg.get("enabled", True)).lower() not in ("false", "0", "no"):
        try:
            from . import vector_store as vs
            qurl = qcfg.get("url") or os.environ.get("QDRANT_URL", "http://localhost:6333")
            qemb = qcfg.get("embed_base_url") or os.environ.get(
                "EMBED_BASE_URL", (cfg.get("ollama", {}) or {}).get("base_url", "http://localhost:11434"))
            stats["qdrant"] = vs.index_relations(
                relations, resolved, base_url=qemb,
                model=qcfg.get("embed_model", "nomic-embed-text"), url=qurl)
        except Exception as exc:
            log.warning("Qdrant indexing skipped: %s", exc)
            stats["qdrant"] = {"indexed": 0, "error": str(exc)[:200]}

    neo4j_result = None
    if write_to_neo4j:
        ncfg = cfg.get("neo4j", {})
        store = Neo4jStore(uri=ncfg.get("uri"), username=ncfg.get("username"),
                           password=ncfg.get("password"), database=ncfg.get("database", "neo4j"),
                           batch_size=int(ncfg.get("batch_size", 500)),
                           max_retries=int(ncfg.get("max_retries", 10)),
                           retry_delay=float(ncfg.get("retry_delay_seconds", 3)))
        store.connect()
        try:
            neo4j_result = store.store(resolved, relations, events=events)
        finally:
            store.close()
        stats.update(neo4j_result or {})
    stats["num_events"] = len(events)

    exported: list[str] = []
    if export:
        exported = export_all(resolved, relations, Path(out_cfg.get("dir", "outputs")),
                              export_csv=bool(out_cfg.get("export_csv", True)),
                              export_json=bool(out_cfg.get("export_json", True)),
                              events=events)
        stats["exported_files"] = exported

    # Optional in-memory graph for stats (NetworkX only, Neo4j is persistent store)
    try:
        g = build_networkx_graph(resolved, relations)
        stats["nx_nodes"] = g.number_of_nodes()
        stats["nx_edges"] = g.number_of_edges()
    except Exception as exc:
        log.warning("NetworkX graph build skipped: %s", exc)

    log.info("Pipeline complete: %s", {k: v for k, v in stats.items() if k != "exported_files"})
    if not stats.get("skipped_unchanged"):
        try:
            manifest["_config"] = cfg_fp
            for p in paths:
                manifest[str(p.resolve())] = _file_fingerprint(p)
            _save_manifest(out_dir, manifest)
        except Exception as exc:
            log.warning("Manifest save skipped: %s", exc)
    return {"stats": stats, "entities": resolved, "relations": relations,
            "pdf_results": pdf_results, "exported": exported}


def check_neo4j(cfg: dict | None = None) -> bool:
    cfg = cfg or config_mod.load_config()
    ncfg = cfg.get("neo4j", {})
    store = Neo4jStore(uri=ncfg.get("uri"), username=ncfg.get("username"),
                       password=ncfg.get("password"), database=ncfg.get("database", "neo4j"),
                       max_retries=3, retry_delay=2)
    try:
        store.connect()
        store.close()
        return True
    except Exception as exc:
        log.error("Neo4j check failed: %s", exc)
        return False
