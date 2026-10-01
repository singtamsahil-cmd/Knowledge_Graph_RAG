"""Qdrant vector index over relation evidence (hybrid GraphRAG sidecar).

Embeddings come from the host Ollama embedding model (nomic-embed-text,
already present — no new model downloads). Indexing is best-effort and
idempotent: point IDs are deterministic hashes, so re-ingest overwrites
instead of duplicating. Neo4j remains the source of truth for the graph.
"""
from __future__ import annotations

import hashlib  # noqa: F401 (kept for backward-compat imports)
import json
import logging
import urllib.request

log = logging.getLogger(__name__)

COLLECTION = "kg_evidence"
DIM = 768  # nomic-embed-text


def _point_id(document: str, sentence: str, predicate: str) -> str:
    import uuid
    raw = f"{document}||{sentence}||{predicate}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, raw))  # deterministic UUID


def _embed_one(args) -> list[float]:
    text, base_url, model, timeout = args
    payload = json.dumps({"model": model, "prompt": text[:2000]}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/embeddings", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise ConnectionError(
            f"Cannot reach embedding model '{model}' at {base_url} "
            f"(ollama serve + `ollama pull {model}`?). Original: {exc}") from exc
    vec = body.get("embedding")
    if not vec:
        raise RuntimeError(f"Empty embedding for text: {text[:80]}")
    return vec


def embed_texts(texts: list[str], base_url: str, model: str,
                timeout: int = 60, workers: int = 8) -> list[list[float]]:
    """Embed via Ollama /api/embeddings (host, not Docker), threaded."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(_embed_one,
                             [(t, base_url, model, timeout) for t in texts]))


def get_client(url: str):
    from qdrant_client import QdrantClient
    # minor-version skew vs server is tolerated (same wire protocol)
    return QdrantClient(url=url, timeout=30, check_compatibility=False)


def ensure_collection(client, dim: int = DIM) -> None:
    from qdrant_client.models import Distance, VectorParams
    try:
        info = client.get_collection(COLLECTION)
        if info.config.params.vectors.size != dim:
            log.warning("Qdrant collection dim mismatch — recreating %s.", COLLECTION)
            client.recreate_collection(COLLECTION, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
    except Exception:
        from qdrant_client.models import Distance, VectorParams
        client.create_collection(COLLECTION, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
        log.info("Created Qdrant collection %s.", COLLECTION)


def index_relations(relations, resolved=None, base_url: str = "http://localhost:11434",
                    model: str = "nomic-embed-text", url: str = "http://localhost:6333",
                    batch: int = 32) -> dict:
    """Upsert relation evidence points. Returns counts; raises only on embed failure."""
    from qdrant_client.models import PointStruct

    client = get_client(url)
    ensure_collection(client)
    texts, payloads, ids = [], [], []
    for r in relations:
        ev = r.sentence if hasattr(r, "sentence") else r.get("evidence", "")
        subj = r.subject_text if hasattr(r, "subject_text") else r.get("subject", "")
        pred = r.predicate if hasattr(r, "predicate") else r.get("predicate", "")
        obj = r.object_text if hasattr(r, "object_text") else r.get("object", "")
        doc = r.document if hasattr(r, "document") else r.get("document", "")
        page = r.page_number if hasattr(r, "page_number") else r.get("page", 0)
        conf = float(getattr(r, "confidence", 0.5)) if hasattr(r, "confidence") else float(r.get("confidence", 0.5))
        if not (ev or "").strip():
            continue
        texts.append(f"{subj} {pred} {obj}. {ev}")
        payloads.append({"subject": subj, "predicate": pred, "object": obj,
                         "evidence": ev, "document": doc, "page": page,
                         "confidence": conf,
                         "subject_id": getattr(r, "subject_id", None) or r.get("subject_id"),
                         "object_id": getattr(r, "object_id", None) or r.get("object_id")})
        ids.append(_point_id(doc, ev, pred))
    if not texts:
        return {"indexed": 0, "skipped_no_evidence": len(relations)}
    vectors = embed_texts(texts, base_url=base_url, model=model)
    for i in range(0, len(ids), batch):
        client.upsert(COLLECTION, points=[
            PointStruct(id=ids[j], vector=vectors[j], payload=payloads[j])
            for j in range(i, min(i + batch, len(ids)))])
    log.info("Indexed %d evidence points into Qdrant.", len(ids))
    return {"indexed": len(ids), "skipped_no_evidence": len(relations) - len(ids)}


def search(query: str, base_url: str, model: str = "nomic-embed-text",
           url: str = "http://localhost:6333", limit: int = 8) -> list[dict]:
    """Semantic search over evidence. Returns payloads with _vector_score."""
    client = get_client(url)
    vec = embed_texts([query], base_url=base_url, model=model)[0]
    hits = client.query_points(COLLECTION, query=vec, limit=limit).points
    out = []
    for h in hits:
        p = dict(h.payload or {})
        p["_vector_score"] = round(float(h.score), 4)
        p["_origin"] = "vector"
        out.append(p)
    return out


def delete_document(document: str, url: str = "http://localhost:6333") -> int:
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    client = get_client(url)
    try:
        res = client.delete(COLLECTION, points_selector=Filter(
            must=[FieldCondition(key="document", match=MatchValue(value=document))]))
        return int(getattr(res, "deleted", 0) or 0)
    except Exception as exc:
        log.warning("Qdrant delete skipped: %s", exc)
        return 0
