# What We Improved Yesterday (2026-09-30) — Detailed Understanding Document

**Project:** KG_Unstructured Document (PDF → Knowledge Graph → GraphRAG)
**Date covered:** 2026-09-30 (Initial Commit `8b82853` → current working tree)
**Status:** Changes are in working tree, not yet committed (23 modified files + 12 new files, +1396 / -206 lines + ~1100 lines new)
**Test evidence:** `outputs/entities.json`, `relationships.json`, `events.json`, `eval/results.json` generated 17:55–18:12 on 2 PDFs

This document explains in simple + technical detail: what existed before, what we changed, and how it was done.

---

## 1. Big Picture: Before vs After

**Before (Initial Commit - `8b82853`, 36 files, 2910 lines):**
A basic pipeline that worked end-to-end but was noisy and not production-ready:

```
PDF -> pdf_processor (text) -> nlp_processor (spaCy) -> entity_extractor (names)
-> relationship_extractor (verbs) -> entity_resolver (exact match only)
-> graph_builder -> neo4j_store -> exporter (csv/json)
+ frontend/app.py (basic Streamlit) + main.py + docker-compose (neo4j+pipeline+frontend)
```

Problems:
- `The ABC Bank Ltd`, `ABC Bank`, `ABC Bank's` = 3 different nodes
- Dates, money, percentages (`2024`, `$50M`, `50%`) became graph nodes
- Useless relations like `X is Y`, `X has Y` filled the graph
- No quality check, no dates attached, no contradiction detection
- Every run re-processed all PDFs, even if unchanged
- No way to ask questions, no API, no vector search, no events
- Docker had no restarts, no healthchecks, no backups, no API/Qdrant services

**After (2026-09-30 working tree):**

```
PDF -> pdf_processor (+OCR flag) -> nlp_processor -> entity_extractor (clean+filter)
-> relationship_extractor (filtered verbs + confidence) -> entity_resolver
   (exact + alias + fuzzy) -> validate.py (NEW: dedup + contradict + temporal)
-> events.py (NEW: Event nodes) -> neo4j_store (idempotent merge)
-> exporter + vector_store.py (NEW: Qdrant sidecar)
-> rag.py (NEW: GraphRAG) <- api.py (NEW: FastAPI) <- frontend/app.py (improved)
+ pipeline.py (manifest + skip unchanged + stats) + hardened docker-compose + full tests/eval
```

In one line: **before = collected everything messy. After = cleans, merges smartly, checks quality, skips repeat work, lets you chat with it, ready for production.**

---

## 2. Module-by-Module: How It Was Done

### 2.1 Finding Names — `src/entity_extractor.py` (+97 lines)

**Earlier code:**
```python
def normalize_name(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text.lower()
```
Only lowercase. No other cleaning.

**Now — how done:**
1. Added constants: `_ARTICLES = {the,a,an}`, `_HONORIFICS = {dr,mr,ms...}`, `_CORP_SUFFIXES = {corp,inc,ltd,limited,llc,plc,co,company,pvt,holdings,group...}`
2. Rewrote `normalize_name()`:
   - lowercase, fix unicode quotes `’ -> '`
   - strip surrounding `" ' ( ) , .`
   - remove possessive `'s` (`Acme's -> Acme`)
   - punctuation -> space, collapse spaces
   - strip leading articles/honorifics in a loop
   - strip trailing corp suffixes in a loop (handles `Pvt Ltd` by popping twice)
   - Example: `The ABC Bank Ltd.` + `ABC Bank` + `ABC Bank's` -> all `abc bank`. Display `text` field is untouched, only matching key is canonical.
3. Added `_clean_display()` — collapses table newlines `\n` to single space.
4. Added `DEFAULT_DROP_TYPES = {CARDINAL, ORDINAL, QUANTITY, PERCENT, DATE, TIME, MONEY}` + `GENERIC_PHRASES = {all names, real customers, fictional...}` + `_is_generic()` check.
5. Changed signature: `extract_entities(..., max_chunk_tokens=4 (was 6), max_chars=80, drop_types=...)`. Noun-chunks longer than 4 tokens or 80 chars are dropped. Pronouns / stopword-only chunks dropped.

**Why:** Dates and money are attributes, not nodes. Shorter chunks = fewer junk nodes.

### 2.2 Merging Same Person/Company — `src/entity_resolver.py` (+266 lines)

**Earlier:** `Never merges ambiguous entities... exact normalized match required. Optional fuzzy with difflib SequenceMatcher.` In practice almost no merging.

**Now — 3 stages, safe -> risky:**

Stage 1 — Exact canonical match (always):
- Uses new `normalize_name()` from 2.1.

Stage 2 — Alias merge (always, no threshold needed):
- Subset tokens: `Priya` (1 token) inside `Priya Singh` (2 tokens) -> merge, keep longer as display name.
- Acronyms: `NSF` letters match initials of `National Science Foundation` -> merge.
- `NOUN_PHRASE <-> typed entity`: chunk `fictional bank` merges into typed `ORG: ABC Bank` if tokens overlap.
- Safety list: `_GENERIC_SINGLETONS = {bank, company, university, hospital...}` — single generic word alone will NEVER merge by subset. So `bank` alone does not collapse into `ABC Bank`.

Stage 3 — Fuzzy merge (opt-in `entity_resolution.use_fuzzy: true`):
- Replaced `difflib` with `rapidfuzz` (`fuzz.ratio` + `fuzz.token_set_ratio` max / 100). `token_set_ratio` handles word-order: `Bank ABC vs ABC Bank Ltd` = high score.
- Threshold from config `similarity_threshold: 0.85`.
- Fallback to `difflib` if `rapidfuzz` not installed.

Other changes:
- `ResolvedEntity.name` = most informative mention (longest), not first.
- `deterministic_id(normalized, type)` = `sha1(normalized||type)[:12]` — same input always same ID, re-runs stable.
- Tracks `mentions[]` + `alternative_names[]` for audit.

**Config addition:**
```yaml
entity_resolution:
  similarity_threshold: 0.85
  use_fuzzy: true   # NEW
```

### 2.3 Finding Relations — `src/relationship_extractor.py` (+238 lines), `src/graph_builder.py` (+56)

**Earlier:** `extract_relationships(segments_with_docs)` — took every verb dependency, including `be, have, do, get, exist`. No predicate filter, no confidence tiers.

**Now — how done:**
- New param: `drop_predicates={be,have,do,get,exist}` from `config.yaml: extraction.drop_predicates`. Filters at extraction time.
- `min_relation_tokens: 3` — evidence sentence must be >=3 tokens.
- Confidence scoring per relation (verb voice, evidence length, entity link success) + `max_entity_chars` guard.
- `graph_builder.link_relations_to_entities()` re-run after resolver creates placeholders, so relations point to merged IDs, not stale mentions.
- `graph_queries.py` (+24): added helpers for status queries and 2-hop `INFERRED` neighbours (used by RAG, marked as inferred, never sole basis).

Example:
- Before: `ABC Bank is large` -> relation `(ABC Bank, be, large)` stored.
- After: dropped, because predicate `be` in drop list.

### 2.4 New Quality Gate — `src/validate.py` (NEW, 132 lines)

Did not exist before. Called from `pipeline.py` after linking:

```python
from .validate import attach_temporal, validate_relations
attach_temporal(relations, entities)
relations, validation_report = validate_relations(relations, min_confidence=...)
```

What it does:
1. `attach_temporal()`: looks for DATE entity or regex `\d{1,2} Month YYYY` in same evidence sentence, sets `relation.event_date`. DATE/TIME labels are dropped as nodes, but date is preserved as attribute.
2. `validate_relations()`:
   - Drop evidence-less relations.
   - Merge exact duplicates `(subj,pred,obj)` — union evidence lists.
   - Contradiction flags: same triple affirmed AND negated (`not approved`), or status conflict (`STATUS_APPROVED={approve,grant,accept...}` vs `STATUS_PENDING={apply,review,submit,request...}` on same target) -> `review_status=contradicted`.
   - Confidence tiers -> `review_status`: `verified / candidate / uncertain / contradicted`. Low-confidence kept but flagged, never silently lost.
   - Returns `validation_report {kept, dropped, merged, contradicted}` added to pipeline `stats`.

**Config:**
```yaml
validation:
  min_confidence: 0.0  # below -> uncertain (kept, flagged)
```

### 2.5 New First-Class Events — `src/events.py` (NEW, 51 lines)

Did not exist before. Gated by config (default off to keep graph clean):

```yaml
events:
  enabled: false
  min_confidence: 0.6
  max_events: 500
```

Logic in `build_events(relations, ...)`:
- Only when relation has ALL three: `event_date` (from 2.4) + both `subject_id` and `object_id` linked + `confidence >= 0.6`.
- Creates `Event{id, name, event_type=verb lemma, event_date, participants=[{entity_id, role=subj/obj}], source_relation, documents, sentences}`.
- ID = `event_` + `sha1(predicate||date||subj||obj)[:12]` — deterministic, re-ingest merges and unions docs/evidence instead of duplicating.
- Original relation is KEPT — event adds n-ary structure, never replaces facts.

### 2.6 Pipeline Smarts — `src/pipeline.py` (+154 lines)

**Earlier (99 lines):** `run_pipeline(pdf,input_dir,cfg,write_to_neo4j,export)` — always processed all PDFs, no skip, no validation, no events, no Qdrant, `max_noun_chunk_tokens=6`.

**Now (222 lines) — how done:**
1. Added `MANIFEST_NAME=.ingest_manifest.json`, `DEFAULT_PASSWORDS={password,change_this_password,neo4j}`.
2. Added `_file_fingerprint(p)` — `sha1` of file bytes, chunked 1MB.
3. Added `_config_fingerprint(cfg)` — `sha1` of shape `{spacy_model,batch_size,ocr_threshold,extraction,entity_resolution,coref,ner}`. Config change => reprocess all.
4. Incremental logic: load manifest, compare fingerprints. If all unchanged -> return `{skipped_unchanged}` without NLP work. Else process only `fresh` files, log `Skipping X unchanged`.
5. New param `force=False` to override skip.
6. Neo4j default-password warning, `_maybe_coref_note()` — if `coref.enabled=true` but `fastcoref` not installed, warn and continue (keeps Docker slim).
7. Passes new filters to extractor: `max_chunk_tokens=4, max_chars=80, drop_types=[CARDINAL...], drop_predicates=[be,have...]`.
8. Calls `attach_temporal + validate_relations + build_events` (see 2.4/2.5).
9. Extended `stats`: `needs_ocr, num_mentions, avg_confidence, validation report, qdrant result`.
10. Qdrant sidecar (best-effort try/except — Neo4j stays source of truth):
```python
from . import vector_store as vs
stats["qdrant"] = vs.index_relations(...)
```

### 2.7 New Chat Over Graph — `src/rag.py` (NEW, 343 lines)

Did not exist before. Design: **RAG only — extraction stays spaCy/grammar, no LLM writes to graph.**

1. Retrieve: keyword overlap against entity names + relation endpoints/evidence, from Neo4j when reachable else `outputs/*.json`. Top-k (default 8, `max_context_chars=4000`) relations with evidence + confidence become context. Includes 2-hop `INFERRED` neighbours marked as such.
2. Generate: single `POST` to Ollama `/api/generate (stream=false)` with grounding `SYSTEM_PROMPT`:
> "Answer ONLY from facts below... cite evidence... who <verb>ed X answered by Subject... status applied/under-review with 'no approval recorded' means No... amounts/dates quote exact figure... INFERRED only as support... if no match say you don't know."

3. Providers: `ollama-local` (no key, `http://localhost:11434`, model `llama3.1:8b`) and `ollama-cloud` (`https://ollama.com/api` + `OLLAMA_API_KEY`, model `gemma4:31b`). Same shape. Ollama runs on HOST, not Docker; containers use `http://host.docker.internal:11434`.

**Config:**
```yaml
ollama: {base_url, model, timeout_seconds: 120, top_k: 8, max_context_chars: 4000}
llm: {provider, local_model, cloud_model, cloud_base_url, api_key}
```

### 2.8 New Vector Sidecar — `src/vector_store.py` (NEW, 114 lines)

- Embeddings via host Ollama `nomic-embed-text` (768-dim, already present, no new downloads), threaded `workers=8`.
- Qdrant collection `kg_evidence`, point ID = deterministic `uuid5(document||sentence||predicate)` — re-ingest overwrites, idempotent.
- Indexing best-effort, Neo4j remains truth.

```yaml
qdrant: {url, embed_model: nomic-embed-text, embed_base_url, enabled: true, hybrid_top_k: 8}
```

### 2.9 New API — `src/api.py` (NEW, 108 lines)

`uvicorn src.api:app --host 0.0.0.0 --port 8000`, no auth (local tool):

- `GET /healthz -> {status:ok}`
- `GET /readyz` — loads config, `check_neo4j()`, 503 if unreachable
- `POST /ingest {pdf,input_dir,no_neo4j,force}` -> calls `run_pipeline`
- `POST /ask {question,top_k,use_neo4j,provider,model,api_key}` -> calls `rag.ask` (api_key per-request, never stored/logged)

### 2.10 Production Hardening — `config.yaml`, `docker-compose.yml`, `Dockerfile`, `.env.example`, `frontend/app.py`, `main.py`

**config.yaml (24 -> 59 lines):** added `validation, events, coref, ner, ollama, llm, qdrant, extraction.max_entity_chars/drop_entity_types/drop_predicates/min_relation_tokens`. All secrets `${ENV:-default}`.

**.env.example (+11):** added `BACKUP_DIR, OLLAMA_MODEL, LLM_PROVIDER, CLOUD_MODEL, OLLAMA_API_KEY` + comment `LEAVE OLLAMA_BASE_URL UNSET`.

**docker-compose.yml (58 -> 157 lines):**
- `neo4j`: + `restart: unless-stopped`, `${BACKUP_DIR}:/backups`, log rotation `10m x3`.
- `pipeline`: + `QDRANT_URL, QDRANT_ENABLED, EMBED_BASE_URL`, log rotation.
- `frontend`: + `restart`, Ollama/Qdrant env, Streamlit healthcheck `/_stcore/health`.
- NEW `api` service :8000 (uvicorn, healthcheck `/healthz`).
- NEW `qdrant` service `qdrant/qdrant:v1.12.4` :6333 + volume `qdrant_data` + healthcheck `/readyz`.

**requirements.txt (+4):** `rapidfuzz>=3.0, fastapi>=0.110, uvicorn>=0.29, qdrant-client>=1.10`.

**frontend/app.py (+92):** ask box (provider/model/top_k), graph stats, validation report display, event list, evidence citations. Before: only ingest + basic stats.

**main.py (+5):** `--force`, `--no-qdrant`, `--ask` passthrough to pipeline/RAG.

**src/neo4j_store.py (+66), src/pdf_processor.py (+40), src/exporter.py (+15):** idempotent `MERGE` instead of `CREATE` (no duplicates on re-run), `needs_ocr` flag per PDF, exporter writes `events.json` + validation summary.

**docs/ (+195 lines total):** `architecture.md, hld.md, lld.md` updated with RAG, validation, events, Qdrant, API diagrams.

### 2.11 Tests + Eval (NEW)

Before: `test_pdf_processor, test_entity_extractor, test_entity_resolver (basic), test_relationship_extractor (basic), test_pipeline, test_neo4j_integration, test_delete_document`.

After — updated: `test_entity_resolver (+29), test_relationship_extractor (+34), test_delete_document (+11)` for new merge/filter/merge behaviour.

New files:
- `tests/test_knowledge_fabric.py` (97 lines) — end-to-end fabric: ingest -> validate -> events -> query.
- `tests/test_multidomain.py` (102 lines) — bank + manufacturing PDFs, checks cross-domain no false merges.
- `tests/test_rag.py` (108 lines) — grounding: correct answer from evidence, `I don't know` on thin context, no invented numbers.
- `tests/test_production_guards.py` (42 lines) — default password warning, manifest skip, config-change reprocess.
- `tests/test_production_batch2.py` (41 lines) — API health/ready, Qdrant idempotent IDs, event determinism.
- `scripts/eval_fabric.py, eval_quality.py, rag_battery.py` + `eval/results.json` — manual eval harness.

Real run 2026-09-30 17:55: `outputs/entities.json/csv, relationships.json/csv, events.json` from `fictional_bank_annual_operations_report.pdf` + `manufacturing_operations_report.pdf`.

---

## 3. How to Verify / Run

```bash
# 1. See what changed but not committed
git status --short
git diff --stat HEAD

# 2. Run tests for new behaviour
pytest tests/test_knowledge_fabric.py tests/test_rag.py tests/test_production_guards.py -v

# 3. Re-run ingest (second run should skip unchanged)
python main.py --input-dir data/input
python main.py --input-dir data/input  # -> skipped_unchanged

# 4. Ask a question (needs ollama serve + ollama pull llama3.1:8b)
python main.py --ask "Who approved the loan for ABC Bank?"

# 5. Services
docker compose up -d neo4j qdrant api frontend
curl http://localhost:8000/healthz
curl http://localhost:6333/readyz
```

---

## 4. Glossary (Simple)

- **Entity:** a name — person, company, place. Example: `Priya Singh`.
- **Relation:** a fact — `Priya Singh --approved--> loan`. Has evidence sentence + confidence.
- **Resolve/merge:** deciding `Priya` and `Priya Singh` are same person.
- **Validate:** quality check before saving.
- **Temporal:** date attached to fact.
- **Event:** fact + when + who took part. Example: `approval on 12 March 2024`.
- **RAG:** ask in English, AI answers only from graph facts.
- **Qdrant:** fast similarity search sidecar, Neo4j stays truth.
- **Manifest:** small file remembering which PDFs already processed.

---

*Generated 2026-10-01 from `git diff HEAD` + new files in `src/`, `tests/`, `scripts/`, `eval/`.*
