"""Knowledge Graph Extractor frontend.

Upload PDFs for extraction, browse what's stored, and delete documents
(file on disk + their Neo4j data). Run with:
    streamlit run frontend/app.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src import config as config_mod  # noqa: E402
from src import graph_queries  # noqa: E402
from src.neo4j_store import Neo4jStore  # noqa: E402
from src.pipeline import run_pipeline  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
log = logging.getLogger("frontend")

INPUT_DIR = PROJECT_ROOT / "data" / "input"
INPUT_DIR.mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title="PDF Knowledge Graph", layout="wide")


@st.cache_resource
def get_config() -> dict:
    return config_mod.load_config()


def make_store(cfg: dict) -> Neo4jStore:
    n = cfg.get("neo4j", {})
    return Neo4jStore(uri=n.get("uri"), username=n.get("username"),
                      password=n.get("password"), database=n.get("database", "neo4j"),
                      max_retries=3, retry_delay=2)


def neo4j_ok(cfg: dict) -> bool:
    store = make_store(cfg)
    try:
        store.connect()
        return True
    except Exception as exc:
        log.error("Neo4j check failed: %s", exc)
        return False
    finally:
        store.close()


def fetch_documents(cfg: dict) -> list[dict]:
    """Merge Neo4j Document nodes with files on disk into one listing."""
    store = make_store(cfg)
    try:
        store.connect()
        with store._session() as session:
            rows = [dict(r) for r in graph_queries.list_documents(session)]
    finally:
        store.close()
    by_name = {r["name"]: r for r in rows}
    for f in sorted(INPUT_DIR.glob("*.pdf")) + sorted(INPUT_DIR.glob("*.PDF")):
        by_name.setdefault(f.name, {"name": f.name, "pages": None,
                                    "entities": 0, "relations": 0})
    for r in by_name.values():
        r["on_disk"] = (INPUT_DIR / r["name"]).exists()
        r["in_neo4j"] = r["name"] in {x["name"] for x in rows}
    return [by_name[k] for k in sorted(by_name)]


def delete_document_everywhere(cfg: dict, name: str) -> dict:
    """Delete the PDF file (if present) and its Neo4j + Qdrant data."""
    report: dict = {"file_deleted": False, "neo4j": None, "qdrant_deleted": 0}
    path = INPUT_DIR / name
    if path.exists() and path.is_file():
        path.unlink()
        report["file_deleted"] = True
    store = make_store(cfg)
    try:
        store.connect()
        with store._session() as session:
            present = session.run(
                "MATCH (d:Document {name: $doc}) RETURN count(d) AS n",
                doc=name).single()["n"] > 0
        if present:
            report["neo4j"] = store.delete_document(name)
    finally:
        store.close()
    qcfg = cfg.get("qdrant", {})
    if str(qcfg.get("enabled", True)).lower() not in ("false", "0", "no"):
        try:
            import os as _os
            from src import vector_store as _vs
            qurl = qcfg.get("url") or _os.environ.get("QDRANT_URL", "http://localhost:6333")
            report["qdrant_deleted"] = _vs.delete_document(name, url=qurl)
        except Exception as exc:
            log.warning("Qdrant delete skipped: %s", exc)
    return report


cfg = get_config()
connected = neo4j_ok(cfg)

st.title("PDF Knowledge Graph Extractor")
with st.sidebar:
    st.header("Status")
    st.write("Neo4j:", "Connected" if connected else "Not reachable")
    if connected:
        docs = fetch_documents(cfg)
        with make_store(cfg) as store:
            with store._session() as session:
                n_ent = session.run("MATCH (e:Entity) RETURN count(e) AS n").single()["n"]
                n_rel = session.run("MATCH ()-[r]->() RETURN count(r) AS n").single()["n"]
        st.metric("Documents", len(docs))
        st.metric("Entities", n_ent)
        st.metric("Relations", n_rel)
    st.divider()
    st.link_button("Open Neo4j Browser", "http://localhost:7474")

if not connected:
    st.error("Cannot reach Neo4j. Start it with `docker compose up -d neo4j` and refresh.")
    st.stop()

tab_upload, tab_docs, tab_explore, tab_ask = st.tabs(["Upload", "Documents", "Explore", "Ask"])

with tab_upload:
    st.subheader("Upload PDFs")
    uploads = st.file_uploader("Choose PDF files", type=["pdf"], accept_multiple_files=True)
    if uploads:
        existing = [u.name for u in uploads if (INPUT_DIR / u.name).exists()]
        if existing:
            st.warning(f"Already on disk, will be skipped: {', '.join(existing)}")
        if st.button("Process uploaded PDFs", type="primary"):
            for u in uploads:
                dest = INPUT_DIR / u.name
                if dest.exists():
                    continue
                dest.write_bytes(u.getbuffer())
                with st.spinner(f"Extracting {u.name} ..."):
                    try:
                        # force=True: user explicitly asked to (re)process this file,
                        # so the incremental manifest must not skip it.
                        result = run_pipeline(pdf=str(dest), cfg=cfg,
                                              write_to_neo4j=True, export=False,
                                              force=True)
                        st.success(f"{u.name}: {result['stats'].get('num_entities', 0)} "
                                   f"entities, {result['stats'].get('num_relations', 0)} relations stored.")
                        if u.name in (result["stats"].get("needs_ocr") or []):
                            st.warning(f"{u.name}: scanned text detected — "
                                       "pre-process with OCRmyPDF/tesseract and re-ingest for full coverage.")
                    except Exception as exc:
                        log.exception("Pipeline failed for %s", u.name)
                        st.error(f"{u.name} failed: {exc}")
            st.rerun()

with tab_docs:
    st.subheader("Documents")
    docs = fetch_documents(cfg)
    if not docs:
        st.info("No documents yet. Upload a PDF first.")
    for d in docs:
        name = d["name"]
        col1, col2, col3, col4, col5 = st.columns([4, 1, 1, 1, 1])
        col1.write(f"**{name}**")
        col2.write("disk" if d["on_disk"] else "—")
        col3.write(f"{d['entities']} entities" if d["in_neo4j"] else "not stored")
        col4.write(f"{d['relations']} relations" if d["in_neo4j"] else "")
        confirm_key = f"confirm_{name}"
        if not st.session_state.get(confirm_key):
            if col5.button("Delete", key=f"del_{name}"):
                st.session_state[confirm_key] = True
                st.rerun()
        else:
            col5.warning("Sure?")
            yes, no = st.columns(2)
            if yes.button("Yes", key=f"yes_{name}"):
                with st.spinner(f"Deleting {name} ..."):
                    report = delete_document_everywhere(cfg, name)
                msg = f"Deleted {name}."
                if report["neo4j"]:
                    n = report["neo4j"]
                    msg += (f" Neo4j: {n['relations_deleted']} relations, "
                            f"{n['orphan_entities_deleted']} orphan entities removed.")
                st.success(msg)
                del st.session_state[confirm_key]
                st.rerun()
            if no.button("No", key=f"no_{name}"):
                del st.session_state[confirm_key]
                st.rerun()

with tab_explore:
    st.subheader("Explore the graph")
    docs = fetch_documents(cfg)
    stored = [d["name"] for d in docs if d["in_neo4j"]]
    if not stored:
        st.info("Nothing stored yet.")
    else:
        choice = st.selectbox("Document", stored)
        store = make_store(cfg)
        try:
            store.connect()
            with store._session() as session:
                rels = [dict(r) for r in graph_queries.graph_for_document(session, choice)]
        finally:
            store.close()
        st.write(f"{len(rels)} relations from **{choice}**")
        for r in sorted(rels[:500], key=lambda x: float(x.get("confidence", 0.5))):
            conf = float(r.get("confidence", 0.5))
            badge = "low" if conf < 0.6 else ("med" if conf < 0.8 else "high")
            with st.expander(f"{r['subject']} — {r['predicate']} — {r['object']}  [{badge} {conf:.2f}]"):
                st.caption(f"p. {r.get('page_number')} | confidence {conf:.2f}")
                for s in (r.get("evidence") or []):
                    st.write(s)
        q = st.text_input("Search entities")
        if q:
            store = make_store(cfg)
            try:
                store.connect()
                with store._session() as session:
                    hits = [dict(x) for x in graph_queries.search_entities_by_name(session, q)]
            finally:
                store.close()
            st.dataframe(hits, use_container_width=True)

with tab_ask:
    st.subheader("Ask the graph (RAG)")
    lcfg = cfg.get("llm", {}) or {}
    prov_options = ["ollama-local", "ollama-cloud"]
    prov_default = prov_options.index(lcfg.get("provider", "ollama-local")) \
        if lcfg.get("provider", "ollama-local") in prov_options else 0
    provider = st.selectbox("Provider", prov_options, index=prov_default,
                            help="ollama-local: your machine (no key). "
                                 "ollama-cloud: https://ollama.com/api (needs API key).")
    if provider == "ollama-cloud":
        model_default = lcfg.get("cloud_model", "gemma4:31b")
        import os as _os
        env_key = (_os.environ.get("OLLAMA_API_KEY") or "").strip() or (lcfg.get("api_key") or "").strip()
        model = st.text_input("Cloud model", value=model_default)
        if env_key:
            st.caption("API key loaded from environment — no need to paste it.")
            api_key = None  # server resolves it from OLLAMA_API_KEY env
        else:
            api_key_in = st.text_input("API key", value="", type="password",
                                       placeholder="paste OLLAMA_API_KEY (not stored)",
                                       help="Sent only with this request, never written to disk.")
            api_key = api_key_in.strip() or None
    else:
        model = st.text_input("Local model",
                              value=lcfg.get("local_model",
                                             (cfg.get("ollama", {}) or {}).get("model", "llama3.1:8b")))
        api_key = None
        st.caption(f"via `{(cfg.get('ollama', {}) or {}).get('base_url', 'http://localhost:11434')}` — no key needed.")
    question = st.text_input("Question", placeholder="e.g. Who approved Home Loan LN30002?")
    if st.button("Ask", type="primary") and (question or "").strip():
        from src.rag import answer_question
        store = make_store(cfg)
        try:
            store.connect()
        except Exception:
            store = None
        with st.spinner("Retrieving facts + asking the model ..."):
            try:
                res = answer_question(question, cfg=cfg, store=store,
                                      provider=provider, model=(model or None),
                                      api_key=api_key)
            except (ConnectionError, RuntimeError, ValueError) as exc:
                st.error(f"RAG failed: {exc}")
                res = None
            finally:
                if store is not None:
                    try:
                        store.close()
                    except Exception:
                        pass
        if res:
            st.write(res["answer"])
            st.caption(f"provider: {res.get('provider')} | retrieval: {res['retrieval']} | "
                       f"model: {res['model']} | facts: {len(res['facts'])}")
            for i, f in enumerate(res["facts"], 1):
                with st.expander(f"[{i}] {f.get('subject')} — {f.get('predicate')} — {f.get('object')}"):
                    st.caption(f"doc {f.get('source_document') or f.get('document')} "
                               f"p.{f.get('page_number') or f.get('page')} "
                               f"| conf {float(f.get('confidence', 0.5)):.2f}")
                    st.write(f.get("evidence"))
