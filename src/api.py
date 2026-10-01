"""Minimal production API: health, readiness, stats, ingest trigger.

Run: uvicorn src.api:app --host 0.0.0.0 --port 8000
No auth (local tool per scope); put behind a reverse proxy with auth to expose.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="KG Extractor API", version="1.0")


class IngestRequest(BaseModel):
    pdf: str | None = None
    input_dir: str | None = None
    no_neo4j: bool = False
    force: bool = False


class AskRequest(BaseModel):
    question: str
    top_k: int | None = None
    use_neo4j: bool = True
    provider: str | None = None  # ollama-local | ollama-cloud
    model: str | None = None
    api_key: str | None = None  # cloud key per-request; never stored/logged


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict:
    from . import config as config_mod
    from .pipeline import check_neo4j

    try:
        cfg = config_mod.load_config()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"config error: {exc}") from exc
    if not check_neo4j(cfg):
        raise HTTPException(status_code=503, detail="neo4j unreachable")
    return {"status": "ready"}


@app.get("/metrics")
def metrics() -> dict:
    """Lightweight metrics from last pipeline outputs (no DB hit)."""
    out = Path("outputs")
    try:
        entities = json.loads((out / "entities.json").read_text(encoding="utf-8"))
        relations = json.loads((out / "relationships.json").read_text(encoding="utf-8"))
    except Exception:
        return {"entities": -1, "relations": -1, "note": "run pipeline first"}
    confs = [float(r.get("confidence", 0.5)) for r in relations]
    return {
        "entities": len(entities),
        "relations": len(relations),
        "avg_confidence": round(sum(confs) / len(confs), 3) if confs else 0.0,
    }


@app.post("/ingest")
def ingest(req: IngestRequest) -> dict:
    from . import config as config_mod
    from .pipeline import run_pipeline

    if not req.pdf and not req.input_dir:
        raise HTTPException(status_code=400, detail="provide pdf or input_dir")
    cfg = config_mod.load_config()
    try:
        result = run_pipeline(pdf=req.pdf, input_dir=req.input_dir, cfg=cfg,
                              write_to_neo4j=not req.no_neo4j, export=True,
                              force=req.force)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"stats": result["stats"]}


@app.post("/ask")
def ask(req: AskRequest) -> dict:
    """GraphRAG: retrieve subgraph facts, answer with local Ollama (host, not Docker)."""
    from . import config as config_mod
    from .neo4j_store import Neo4jStore
    from .rag import answer_question

    if not (req.question or "").strip():
        raise HTTPException(status_code=400, detail="question is empty")
    cfg = config_mod.load_config()
    if req.top_k:
        cfg = dict(cfg)
        oll = dict((cfg.get("llm", {}) or {}))
        oll["top_k"] = req.top_k
        cfg["llm"] = oll
    store = None
    if req.use_neo4j:
        ncfg = cfg.get("neo4j", {})
        store = Neo4jStore(uri=ncfg.get("uri"), username=ncfg.get("username"),
                           password=ncfg.get("password"), database=ncfg.get("database", "neo4j"),
                           max_retries=1, retry_delay=0)
        try:
            store.connect()
        except Exception:
            store = None  # retrieval falls back to outputs/*.json
    try:
        out_cfg = cfg.get("outputs", {})
        result = answer_question(req.question, cfg=cfg, store=store,
                                 out_dir=out_cfg.get("dir", "outputs"),
                                 provider=req.provider, model=req.model,
                                 api_key=req.api_key)
    except (ConnectionError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
    return result
