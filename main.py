"""CLI entrypoint."""
from __future__ import annotations

import argparse
import logging
import sys

from src import config as config_mod
from src.pipeline import check_neo4j, run_pipeline


def setup_logging(level: str = "INFO"):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Domain-independent PDF knowledge graph extractor.")
    p.add_argument("--pdf", default=None, help="Path to a single PDF file.")
    p.add_argument("--input", default=None, help="Directory of PDF files.")
    p.add_argument("--config", default=None, help="Path to config.yaml.")
    p.add_argument("--check-neo4j", action="store_true", help="Verify Neo4j connectivity and exit.")
    p.add_argument("--no-neo4j", action="store_true", help="Skip writing to Neo4j (extract + export only).")
    p.add_argument("--no-export", action="store_true", help="Skip CSV/JSON exports.")
    p.add_argument("--clear-neo4j", action="store_true",
                   help="Delete application-managed Neo4j data (Entity/Document/relationships) and exit.")
    p.add_argument("--force", action="store_true",
                   help="Reprocess even PDFs unchanged since last ingest (skip manifest).")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = config_mod.load_config(args.config)
    import os
    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))

    if args.check_neo4j:
        ok = check_neo4j(cfg)
        print("Neo4j connectivity: OK" if ok else "Neo4j connectivity: FAILED")
        return 0 if ok else 1

    if args.clear_neo4j:
        from src.neo4j_store import Neo4jStore
        ncfg = cfg.get("neo4j", {})
        store = Neo4jStore(uri=ncfg.get("uri"), username=ncfg.get("username"),
                           password=ncfg.get("password"), database=ncfg.get("database", "neo4j"))
        store.connect()
        try:
            store.clear_app_data()
        finally:
            store.close()
        print("Application-managed Neo4j data cleared.")
        return 0

    if not args.pdf and not args.input:
        print("Provide --pdf <file> or --input <dir>. Use --help for details.", file=sys.stderr)
        return 2

    result = run_pipeline(pdf=args.pdf, input_dir=args.input, cfg=cfg,
                          write_to_neo4j=not args.no_neo4j, export=not args.no_export,
                          force=args.force)
    stats = result["stats"]
    print("Pipeline complete.")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
