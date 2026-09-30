"""End-to-end pipeline: PDF -> NLP -> entities -> relations -> resolve -> Neo4j."""
from __future__ import annotations

import logging
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


def run_pipeline(pdf: str | None = None, input_dir: str | None = None,
                 cfg: dict | None = None, write_to_neo4j: bool = True,
                 export: bool = True) -> dict:
    cfg = cfg or config_mod.load_config()
    model = cfg.get("spacy_model", "en_core_web_lg")
    batch_size = cfg.get("batch_size", 32)
    ocr_threshold = cfg.get("ocr_warning_threshold_chars", 50)
    ext = cfg.get("extraction", {})
    resol_cfg = cfg.get("entity_resolution", {})
    out_cfg = cfg.get("outputs", {})

    paths = collect_pdfs(pdf, input_dir)
    if not paths:
        raise FileNotFoundError("No PDF files found to process.")
    log.info("Found %d PDF(s).", len(paths))

    segments, pdf_results = process_pdfs(paths, ocr_threshold=ocr_threshold)
    if not segments:
        raise RuntimeError("No pages extracted from input PDFs.")

    segments_with_docs = list(process_segments_nlp(segments, model=model, batch_size=batch_size))

    entities = extract_entities(
        segments_with_docs,
        use_noun_chunks=ext.get("use_noun_chunks", True),
        min_chunk_tokens=ext.get("min_noun_chunk_tokens", 1),
        max_chunk_tokens=ext.get("max_noun_chunk_tokens", 6),
    )
    relations = extract_relationships(segments_with_docs)

    resolved = resolve_entities(
        entities,
        similarity_threshold=float(resol_cfg.get("similarity_threshold", 0.85)),
        use_fuzzy=bool(resol_cfg.get("use_fuzzy", False)),
    )
    link_relations_to_entities(relations, resolved)
    resolved = ensure_endpoint_entities(relations, resolved)
    # re-link after placeholders created
    link_relations_to_entities(relations, resolved)

    stats = graph_stats(resolved, relations)
    stats.update({
        "num_pdfs": len(pdf_results),
        "num_pages": sum(r.num_pages for r in pdf_results),
        "empty_pages": sum(r.empty_pages for r in pdf_results),
        "num_mentions": len(entities),
    })

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
            neo4j_result = store.store(resolved, relations)
        finally:
            store.close()
        stats.update(neo4j_result or {})

    exported: list[str] = []
    if export:
        exported = export_all(resolved, relations, Path(out_cfg.get("dir", "outputs")),
                              export_csv=bool(out_cfg.get("export_csv", True)),
                              export_json=bool(out_cfg.get("export_json", True)))
        stats["exported_files"] = exported

    # Optional in-memory graph for stats (NetworkX only, Neo4j is persistent store)
    try:
        g = build_networkx_graph(resolved, relations)
        stats["nx_nodes"] = g.number_of_nodes()
        stats["nx_edges"] = g.number_of_edges()
    except Exception as exc:
        log.warning("NetworkX graph build skipped: %s", exc)

    log.info("Pipeline complete: %s", {k: v for k, v in stats.items() if k != "exported_files"})
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
