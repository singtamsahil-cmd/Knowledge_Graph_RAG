"""GraphRAG over the extracted knowledge graph using a LOCAL Ollama model.

Design (RAG only — extraction stays spaCy/grammar, no LLM writes to the graph):
1. Retrieve: keyword overlap against entity names + relation endpoints/evidence,
   from Neo4j when reachable, else from outputs/*.json. Top-k relations with
   evidence sentences + confidence become the context.
2. Generate: single POST to Ollama /api/generate (stream=false) with a
   grounding system prompt (answer ONLY from context, cite evidence, say
   "I don't know" when context is thin).

Ollama runs on the HOST (not Docker). Local runs use http://localhost:11434;
containers reach it via http://host.docker.internal:11434 (see compose).
Default model llama3.1:8b (fast, local); llama3.1:8b for harder questions.

Providers: "ollama-local" (no key) and "ollama-cloud" (https://ollama.com/api
with OLLAMA_API_KEY Bearer auth, e.g. gemma4:31b-cloud). Same /api/generate
shape both sides.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

PROVIDER_LOCAL = "ollama-local"
PROVIDER_CLOUD = "ollama-cloud"

CLOUD_BASE_URL = "https://ollama.com/api"
CLOUD_DEFAULT_MODEL = "gemma4:31b"  # cloud model names have no -cloud suffix
LOCAL_DEFAULT_MODEL = "llama3.1:8b"

SYSTEM_PROMPT = (
    "You answer questions using ONLY the knowledge-graph facts below. "
    "Each fact has labeled Subject / Relation / Object roles plus evidence text. Rules: "
    "a fact matches even when the asked entity appears inside a longer Subject/Object phrase; "
    "a question 'who <verb>ed X' is answered by the Subject of the fact whose Relation is that "
    "verb and whose Subject or Object mentions X; status questions ('was X approved/done') "
    "are answered from evidence about status: applied-for/under-review with explicit "
    "'no approval recorded' means the answer is No, quoted from that evidence; "
    "amounts and dates live inside evidence text — quote the exact figure when asked; "
    "facts tagged INFERRED are 2-hop neighbors: use them only as supporting context, "
    "never as the sole basis — say which facts are explicit vs inferred; "
    "be concise; cite the evidence you use; "
    "if no fact matches, say you don't know — never invent entities, numbers, or relations."
)

_STOP = {
    "the", "a", "an", "of", "and", "in", "on", "for", "to", "is", "are", "was",
    "were", "what", "which", "who", "whom", "where", "when", "why", "how",
    "does", "do", "did", "tell", "about", "me", "give", "list", "show",
}


def question_tokens(question: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (question or "").lower())
            if t not in _STOP and len(t) > 1}


def _score_overlap(tokens: set[str], *texts: str) -> int:
    hay_tokens = set()
    for text in texts:
        hay_tokens.update(re.findall(r"[a-z0-9]+", text.lower()))
    score = 0
    for t in tokens:
        if t in hay_tokens:
            score += 1
            continue
        # stem-ish fallback: shared 6-char prefix ("approved"~"approval")
        if len(t) >= 6 and any(len(h) >= 6 and h[:6] == t[:6] for h in hay_tokens):
            score += 1
    return score


def retrieve_from_json(question: str, out_dir: str | Path = "outputs",
                       top_k: int = 8) -> list[dict]:
    """Keyword retrieval over exported relations (no DB needed)."""
    out = Path(out_dir)
    try:
        relations = json.loads((out / "relationships.json").read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("RAG retrieval: outputs missing (%s). Run the pipeline first.", exc)
        return []
    tokens = question_tokens(question)
    if not tokens:
        return []
    scored = []
    for r in relations:
        s = _score_overlap(tokens, r.get("subject", ""), r.get("predicate", ""),
                           r.get("object", ""), r.get("evidence", ""))
        if s > 0:
            scored.append((s, float(r.get("confidence", 0.5)), r))
    # overlap first, then confidence
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [r for _, _, r in scored[:top_k]]


def retrieve_from_neo4j(question: str, store, top_k: int = 8) -> list[dict]:
    """Keyword retrieval over Neo4j: match entities, expand 1-hop, rank by overlap."""
    tokens = question_tokens(question)
    if not tokens:
        return []
    keywords = sorted(tokens)
    rows: list[dict] = []
    with store._session() as session:
        res = session.run(
            "MATCH (a:Entity)-[r]->(b:Entity) "
            "RETURN a.name AS subject, r.predicate AS predicate, b.name AS object, "
            "r.evidence AS evidence, r.source_document AS source_document, "
            "r.page_number AS page_number, coalesce(r.confidence, 0.5) AS confidence "
            "LIMIT 2000")
        for rec in res:
            r = dict(rec)
            ev = r.get("evidence")
            ev_text = ev[0] if isinstance(ev, list) and ev else (ev or "")
            s = _score_overlap(tokens, str(r.get("subject", "")),
                               str(r.get("predicate", "")), str(r.get("object", "")),
                               str(ev_text))
            if s > 0:
                r["evidence"] = ev_text
                rows.append((s, float(r.get("confidence", 0.5)), r))
    _ = keywords  # reserved for future full-text index path
    rows.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [r for _, _, r in rows[:top_k]]


def build_context(facts: list[dict], max_chars: int = 4000) -> str:
    lines = []
    for i, r in enumerate(facts, 1):
        tag = "INFERRED (2-hop neighbor, weaker)" if r.get("_inferred") else "EXPLICIT"
        lines.append(
            f"[{i}:{tag}] Subject: {r.get('subject')} | Relation: {r.get('predicate')} | "
            f"Object: {r.get('object')} | Source: {r.get('source_document') or r.get('document')}, "
            f"page {r.get('page_number') or r.get('page')}, "
            f"confidence {float(r.get('confidence', 0.5)):.2f} | "
            f"Evidence: {r.get('evidence')}")
    return "\n".join(lines)[:max_chars]


def ask_ollama(question: str, context: str, model: str = "llama3.1:8b",
               base_url: str = "http://localhost:11434",
               timeout: int = 120, api_key: str | None = None,
               chat_api: bool = False) -> str:
    """Non-streaming generation. Local Ollama uses /api/generate;
    Ollama Cloud uses /api/chat (generate 404s there).

    api_key adds `Authorization: Bearer` (Ollama Cloud) — the key itself is
    never logged or stored.
    """
    if chat_api:
        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",
                 "content": f"Context facts:\n{context}\n\nQuestion: {question}\nAnswer:"},
            ],
            "stream": False,
            "options": {"temperature": 0.1},
        }).encode("utf-8")
        path, get_answer = "/api/chat", lambda b: ((b.get("message") or {}).get("content") or "")
    else:
        payload = json.dumps({
            "model": model,
            "prompt": f"Context facts:\n{context}\n\nQuestion: {question}\nAnswer:",
            "system": SYSTEM_PROMPT,
            "stream": False,
            "options": {"temperature": 0.1},
        }).encode("utf-8")
        path, get_answer = "/api/generate", lambda b: (b.get("response") or "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    # base may or may not include the /api prefix — normalize once.
    root = base_url.rstrip("/")
    if root.endswith("/api"):
        root = root[: -len("/api")]
    req = urllib.request.Request(
        f"{root}{path}", data=payload,
        headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        hint = ("is `ollama serve` running? model pulled?"
                if not api_key else "is the API key valid? model name correct?")
        raise ConnectionError(
            f"Cannot reach LLM at {base_url} ({hint} "
            f"model '{model}'). Original: {exc}") from exc
    answer = get_answer(body).strip()
    if not answer:
        raise RuntimeError(f"LLM returned an empty response: {body}")
    return answer


def resolve_llm(cfg: dict | None = None, provider: str | None = None,
                model: str | None = None) -> dict:
    """Merge explicit args > llm: section > legacy ollama: section > defaults."""
    from . import config as config_mod

    cfg = cfg or config_mod.load_config()
    lcfg = cfg.get("llm", {}) or {}
    ocfg = cfg.get("ollama", {}) or {}
    prov = (provider or lcfg.get("provider") or "ollama-local").strip().lower()
    if prov not in (PROVIDER_LOCAL, PROVIDER_CLOUD):
        raise ValueError(f"Unknown LLM provider '{prov}' (use ollama-local|ollama-cloud).")
    if prov == PROVIDER_CLOUD:
        return {
            "provider": prov,
            "model": model or lcfg.get("cloud_model") or CLOUD_DEFAULT_MODEL,
            "base_url": lcfg.get("cloud_base_url") or ocfg.get("cloud_base_url") or CLOUD_BASE_URL,
            "api_key": (os.environ.get("OLLAMA_API_KEY") or lcfg.get("api_key") or "").strip() or None,
            "timeout": int(lcfg.get("timeout_seconds", ocfg.get("timeout_seconds", 180))),
            "top_k": int(lcfg.get("top_k", ocfg.get("top_k", 8))),
            "max_chars": int(lcfg.get("max_context_chars", ocfg.get("max_context_chars", 4000))),
        }
    return {
        "provider": prov,
        "model": model or lcfg.get("local_model") or ocfg.get("model") or LOCAL_DEFAULT_MODEL,
        "base_url": ocfg.get("base_url", "http://localhost:11434"),
        "api_key": None,
        "timeout": int(lcfg.get("timeout_seconds", ocfg.get("timeout_seconds", 120))),
        "top_k": int(lcfg.get("top_k", ocfg.get("top_k", 8))),
        "max_chars": int(lcfg.get("max_context_chars", ocfg.get("max_context_chars", 4000))),
    }


def _deduplicate(fact_lists: list[list[dict]]) -> list[dict]:
    seen, out = set(), []
    for facts in fact_lists:
        for r in facts:
            k = ((r.get("subject") or "").lower(), (r.get("predicate") or "").lower(),
                 (r.get("object") or "").lower())
            if k not in seen:
                seen.add(k)
                out.append(r)
    return out


def _multihop_expand(seed_facts: list[dict], all_relations: list[dict],
                     max_extra: int = 6) -> list[dict]:
    """2-hop expansion: relations touching seed endpoints, marked inferred.

    Hop-2 facts are contextually connected, not directly matched — flagged
    `_inferred: True` so answers distinguish explicit vs inferred.
    """
    names: set[str] = set()
    for r in seed_facts:
        names.add((r.get("subject") or "").lower())
        names.add((r.get("object") or "").lower())
    seed_keys = {(f.get("subject") or "").lower() + "||" + (f.get("predicate") or "").lower()
                 + "||" + (f.get("object") or "").lower() for f in seed_facts}
    extra = []
    for r in all_relations:
        k = (r.get("subject") or "").lower() + "||" + (r.get("predicate") or "").lower() \
            + "||" + (r.get("object") or "").lower()
        if k in seed_keys:
            continue
        if (r.get("subject") or "").lower() in names or (r.get("object") or "").lower() in names:
            r = dict(r)
            r["_inferred"] = True
            extra.append(r)
            if len(extra) >= max_extra:
                break
    return extra


def _load_all_relations_json(out_dir: str | Path) -> list[dict]:
    try:
        return json.loads((Path(out_dir) / "relationships.json").read_text(encoding="utf-8"))
    except Exception:
        return []


def hybrid_retrieve(question: str, cfg: dict | None, store=None,
                    out_dir: str | Path = "outputs") -> tuple[list[dict], dict]:
    """Graph (keyword) + vector (semantic) + 2-hop expansion.

    Strategy: entity-leaning questions (ID codes / quoted names) weight graph
    first; open-ended questions weight vector first. Always unions both when
    available, then expands 2-hop. Returns (facts, meta).
    """
    from . import config as config_mod
    from . import vector_store as vs

    cfg = cfg or config_mod.load_config()

    qcfg = cfg.get("qdrant", {})
    ocfg = cfg.get("ollama", {})
    top_k = int(qcfg.get("hybrid_top_k", ocfg.get("top_k", 8)))
    meta: dict = {"strategy": "graph", "vector_hits": 0, "graph_hits": 0, "hop2": 0}

    graph_facts: list[dict] = []
    if store is not None:
        try:
            graph_facts = retrieve_from_neo4j(question, store, top_k=top_k)
        except Exception as exc:
            log.warning("RAG Neo4j retrieval failed, falling back to JSON: %s", exc)
    if not graph_facts:
        graph_facts = retrieve_from_json(question, out_dir=out_dir, top_k=top_k)
    meta["graph_hits"] = len(graph_facts)

    vector_facts: list[dict] = []
    if str(qcfg.get("enabled", True)).lower() not in ("false", "0", "no"):
        try:
            qurl = qcfg.get("url") or os.environ.get("QDRANT_URL", "http://localhost:6333")
            qemb = qcfg.get("embed_base_url") or os.environ.get(
                "EMBED_BASE_URL", ocfg.get("base_url", "http://localhost:11434"))
            vector_facts = vs.search(
                question, base_url=qemb,
                model=qcfg.get("embed_model", "nomic-embed-text"),
                url=qurl, limit=top_k)
            meta["vector_hits"] = len(vector_facts)
        except Exception as exc:
            log.warning("RAG vector retrieval skipped: %s", str(exc)[:150])

    entity_like = bool(re.search(r"\b[A-Z]{2,}[-\/]?\d[\w-]*\b", question or "") or
                       re.search(r'"[^"]+"', question or ""))
    if vector_facts and graph_facts:
        meta["strategy"] = "hybrid" + ("+entity-first" if entity_like else "+semantic-first")
    elif vector_facts:
        meta["strategy"] = "vector"
    facts = _deduplicate([graph_facts, vector_facts])[: top_k]

    hop2: list[dict] = []
    if store is not None:
        try:
            with store._session() as session:
                for r in facts[:4]:
                    for end in (r.get("subject"), r.get("object")):
                        if not end or len(hop2) >= 6:
                            continue
                        rows = session.run(
                            "MATCH (e:Entity)-[r2]-(n:Entity) "
                            "WHERE toLower(e.name) = toLower($name) "
                            "RETURN e.name AS subject, r2.predicate AS predicate, "
                            "n.name AS object, r2.evidence AS evidence, "
                            "coalesce(r2.confidence, 0.5) AS confidence LIMIT 6",
                            name=end)
                        for rec in rows:
                            d = dict(rec)
                            ev = d.get("evidence")
                            d["evidence"] = ev[0] if isinstance(ev, list) and ev else ev
                            d["_inferred"] = True
                            hop2.append(d)
        except Exception as exc:
            log.warning("RAG 2-hop Neo4j expansion skipped: %s", str(exc)[:150])
    if not hop2:
        hop2 = _multihop_expand(facts, _load_all_relations_json(out_dir))
    meta["hop2"] = len(hop2)
    facts = _deduplicate([facts, hop2])[: top_k + len(hop2)]
    return facts, meta


def answer_question(question: str, cfg: dict | None = None,
                    store=None, out_dir: str | Path = "outputs",
                    provider: str | None = None, model: str | None = None,
                    api_key: str | None = None) -> dict:
    """Full GraphRAG turn. Never raises for thin context — returns don't-know."""
    llm = resolve_llm(cfg, provider=provider, model=model)
    if llm["provider"] == PROVIDER_CLOUD:
        if api_key:
            llm["api_key"] = api_key
        if not llm["api_key"]:
            raise ValueError("Ollama Cloud needs an API key: set OLLAMA_API_KEY "
                             "in .env or pass it with the request.")
    base_url, timeout = llm["base_url"], llm["timeout"]
    top_k, max_chars = llm["top_k"], llm["max_chars"]
    model = llm["model"]

    facts, retrieval_meta = hybrid_retrieve(question, cfg, store=store, out_dir=out_dir)
    source = retrieval_meta.get("strategy", "graph")
    if not facts:
        return {"answer": "I don't know — no relevant facts in the knowledge graph.",
                "facts": [], "retrieval": source, "model": model,
                "provider": llm["provider"], "retrieval_meta": retrieval_meta}
    context = build_context(facts, max_chars=max_chars)
    answer = ask_ollama(question, context, model=model, base_url=base_url,
                        timeout=timeout, api_key=llm["api_key"],
                        chat_api=(llm["provider"] == PROVIDER_CLOUD))
    return {"answer": answer, "facts": facts, "retrieval": source,
            "model": model, "provider": llm["provider"],
            "retrieval_meta": retrieval_meta}
