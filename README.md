# Domain-Independent PDF Knowledge Graph Extractor

Extracts entities (spaCy NER + noun chunks) and **candidate** relationships (spaCy dependency parsing) from arbitrary PDFs, stores them in **Neo4j 5 Community**, and exports CSV/JSON. No LLMs, no domain-specific rules.

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — how the whole system fits together (services, data flow, key decisions).
- [`docs/hld.md`](docs/hld.md) — what each module does, its inputs and outputs.
- [`docs/lld.md`](docs/lld.md) — how each function works, step by step, with the actual Cypher queries.

All three are written in simple language. Start with `architecture.md`.

## Architecture

```
PDF files -> pypdf (page segments w/ provenance) -> spaCy en_core_web_lg (nlp.pipe)
  -> entity extraction -> dependency-based relation candidates
  -> entity resolution (canonical + alias + rapidfuzz) -> NetworkX (optional) -> Neo4j (persistent)
  -> queries + CSV/JSON exports
```

## Project Structure

```
.
├── main.py                 # terminal commands (process, check, clear)
├── config.yaml             # settings (model, thresholds, Neo4j, outputs)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml      # services: neo4j, app, frontend
├── .env.example
├── data/input/             # drop PDFs here (or upload via the web UI)
├── outputs/                # entities/relationships CSV + JSON after runs
├── docs/                   # architecture.md, hld.md, lld.md
├── frontend/app.py         # Streamlit web UI (Upload / Documents / Explore / Ask)
├── src/
│   ├── config.py           # settings loader (YAML + environment)
│   ├── pdf_processor.py    # page-by-page text extraction with provenance
│   ├── nlp_processor.py    # spaCy model loading + batch parsing
│   ├── entity_extractor.py # NER + noun-phrase mentions (canonical normalization)
│   ├── relationship_extractor.py  # grammar triples (SVO, prep, passive, neg)
│   ├── entity_resolver.py  # canonical + alias + rapidfuzz merging, stable IDs
│   ├── relationship_extractor.py  # (see above)
│   ├── validate.py         # dedup + contradiction flags + confidence tiers + dates
│   ├── events.py           # opt-in first-class Event nodes (events.enabled)
│   ├── vector_store.py     # Qdrant evidence index (Ollama embeddings, idempotent)
│   ├── graph_builder.py    # link triples to nodes, stats
│   ├── neo4j_store.py      # write / delete / clear in Neo4j
│   ├── graph_queries.py    # ready-made read queries (incl. review queue)
│   ├── exporter.py         # CSV + JSON exports (entities/relationships/events)
│   ├── rag.py              # GraphRAG retrieval + Ollama answering (read-only)
│   ├── api.py              # FastAPI: /healthz /readyz /metrics, /ingest, /ask
│   └── pipeline.py         # runs all stages in order (manifest + validate + events)
└── tests/                  # unit tests (mocked) + opt-in live Neo4j test
```

## Prerequisites

- Docker + Docker Compose
- (Local dev) Python 3.10+, `pip install -r requirements.txt`, `python -m spacy download en_core_web_lg`

## Setup

```bash
cp .env.example .env
# edit NEO4J_PASSWORD in .env
docker compose build
docker compose up -d neo4j
docker compose ps
```

## Usage

```bash
# single PDF
docker compose run --rm app --pdf data/input/document.pdf
# all PDFs in directory
docker compose run --rm app --input data/input/
# connectivity check
docker compose run --rm app --check-neo4j
# extract + export only (no Neo4j write)
docker compose run --rm app --input data/input/ --no-neo4j
# Neo4j Browser
# http://localhost:7474
# stop (keeps named volume neo4j_data)
docker compose down
```

## Frontend (upload / delete in the browser)

A Streamlit app at **http://localhost:8501** with four tabs:

```bash
docker compose up -d neo4j frontend
```

- **Upload** — drop in any PDFs; they are saved to `data/input/` and run through
  the full pipeline into Neo4j.
- **Documents** — every PDF on disk and every `Document` node in Neo4j, with
  per-document entity/relation counts. **Delete** removes the file *and* that
  document's Neo4j data (two-step confirm).
- **Explore** — per-document relations with evidence sentences, plus entity search.
- **Ask** — GraphRAG Q&A over the graph (provider dropdown: `ollama-local`
  no-key, or `ollama-cloud` with per-request API key; answers cite evidence
  facts, `POST /ask` is the API equivalent).

Delete semantics (see `Neo4jStore.delete_document`): only data exclusive to that
document is removed — relations solely from it, its `Document` node, and entities
left with no documents and no relations (orphans). Entities/relations shared
with other documents are scrubbed of that document's name but otherwise kept.

Local (without Docker): `streamlit run frontend/app.py` (needs Neo4j reachable,
e.g. `NEO4J_URI=bolt://localhost:7687`).

Local (without Docker):

```bash
pip install -r requirements.txt
python -m spacy download en_core_web_lg
python main.py --input data/input/ --no-neo4j   # quick check without DB
python main.py --check-neo4j
```

## Configuration

`.env` (see `.env.example`) + `config.yaml`. Key settings:

| Key | Meaning |
|---|---|
| `NEO4J_URI` | `bolt://neo4j:7687` inside compose, `bolt://localhost:7687` locally |
| `entity_resolution.similarity_threshold` | rapidfuzz token_set threshold (default 0.85) |
| `entity_resolution.use_fuzzy` | fuzzy merge on/off (default `true`) |
| `extraction.use_noun_chunks` | capture non-NER concepts |

Entity resolution is 3-stage: (1) exact canonical match
(`The ABC Bank Ltd.` / `ABC Bank's` → `abc bank`, `Dr. Sharma` → `sharma`),
(2) always-on alias merge (subset `Priya` → `Priya Singh`, acronyms
`NSF` → `National Science Foundation`, `NOUN_PHRASE` ↔ typed bridging),
(3) opt-out fuzzy merge via `rapidfuzz` token_set_ratio + shared-token guard.
Different real types (`ORG` vs `PERSON`) never merge; generic singletons
(`bank`, `company`) never merge by subset alone.

## Neo4j Schema (domain-independent)

```cypher
(:Entity {id, name, normalized_name, entity_type, mentions, alternative_names, documents, pages})
(:Document {name, pages})
(:Document)-[:CONTAINS]->(:Entity)
(:Entity)-[:APPROVE | :WORK_AT | :BORROW_FROM | ... {
  predicate, relation_type, evidence[], source_document,
  source_documents[], page_number, pages[], extraction_method, review_status, negated
}]->(:Entity)
```

Entity types are spaCy labels (`PERSON`, `ORG`, `GPE`, ...) or `NOUN_PHRASE`.
Each relationship is stored under its own type derived from the extracted
predicate (`"work at"` → `:WORK_AT`, `"borrow from"` → `:BORROW_FROM`), so
Neo4j Browser labels edges with the real relation text — no manual caption
setup needed. Types come from grammar, never from hardcoded domain rules, and
every read query matches relationships without specifying a type, so new
predicates work automatically.

## Example Cypher Queries

```cypher
// all entities
MATCH (e:Entity) RETURN e.name, e.entity_type LIMIT 100;
// all relationships with evidence
MATCH (a:Entity)-[r]->(b:Entity)
RETURN a.name, r.predicate, b.name, r.evidence LIMIT 100;
// search entities
MATCH (e:Entity) WHERE toLower(e.name) CONTAINS toLower('sharma') RETURN e;
// neighborhood
MATCH path = (e:Entity {id: 'entity_xxx'})-[r*1..2]-(n:Entity) RETURN path LIMIT 50;
// document subgraph
MATCH (a:Entity)-[r]->(b:Entity)
WHERE r.source_document = 'sample.pdf' RETURN a, r, b;
// evidence
MATCH (a:Entity {id:'...'})-[r]->(b:Entity {id:'...'}) RETURN r.evidence, r.source_documents;
```

Reusable versions live in `src/graph_queries.py`.

## From the notebook to Neo4j (replaces networkx/matplotlib)

If you followed `graph.ipynb` (spaCy NER + SVO/prep extraction → pandas → networkx drawing),
each notebook step maps to this pipeline, with Neo4j as the graph store:

| Notebook cell | Pipeline equivalent |
|---|---|
| `pypdf` page loop | `src/pdf_processor.py` (keeps document + page per segment) |
| `doc.ents` loop | `src/entity_extractor.py` (NER + noun chunks, same labels) |
| `extract_relationships(doc)` | `src/relationship_extractor.py` (same SVO + `verb.lemma + prep` logic, e.g. `borrow from`, `work at`) |
| `pd.DataFrame(relationships)` | `outputs/relationships.csv` / `.json` (auto-exported) |
| `nx.MultiDiGraph` + `plt` drawing | Neo4j: `(:Entity)-[:WORK_AT {predicate, ...}]->(:Entity)` |

To reproduce the notebook's 12-sentence test doc in Neo4j:

```bash
docker compose up -d neo4j
docker compose run --rm app --pdf data/input/kg_test_document.pdf
```

Then open **Neo4j Browser at http://localhost:7474** and run:

```cypher
MATCH (a:Entity)-[r]->(b:Entity)
WHERE 'kg_test_document.pdf' IN coalesce(r.source_documents, [])
RETURN a, r, b;
```

The Browser draws the same picture as `nx.draw()` — nodes are entities, edge
captions are the predicates (`work at`, `borrow from`, `headquarter in`, ...).
Click any edge to see its properties: evidence sentence, source document, page,
`negated` flag (e.g. sentence 9, "does not own", is stored with `negated: true`
instead of being silently dropped) and `review_status: "candidate"`.

To load your Colab PDF instead, copy it into `data/input/` and run:

```bash
docker compose run --rm app --pdf data/input/fictional_bank_annual_operations_report.pdf
```

## Testing

```bash
pytest -v
# live Neo4j integration (needs running DB):
RUN_NEO4J_TESTS=1 pytest tests/test_neo4j_integration.py -v
# quality gates (needs outputs/*.json from a pipeline run):
python main.py --input data/input/ --no-neo4j --force
python scripts/eval_quality.py
# before/after comparison (eval/baseline = pre-improvement snapshot):
python scripts/eval_fabric.py
# multi-domain generalization (healthcare/legal/research/education/logistics):
python -m pytest tests/test_multidomain.py -v
```

Synthetic multi-domain fixtures (business, healthcare, education, research, legal) are generated on the fly in `tests/conftest.py` with reportlab.

## Production runbook

```bash
cp .env.example .env   # set NEO4J_PASSWORD, BACKUP_DIR
docker compose build   # required after requirements/config changes
docker compose up -d neo4j
docker compose run --rm app --check-neo4j

# re-ingest (required after filter changes — IDs/shape changed):
docker compose run --rm app --clear-neo4j
docker compose run --rm app --input data/input/        # skips unchanged PDFs; add --force to reprocess
python scripts/eval_quality.py   # gates: NOUN_PHRASE<=0.85, no be/have/do, no numeric nodes

docker compose up -d frontend    # http://localhost:8501, Neo4j Browser :7474
docker compose up -d api         # http://localhost:8000: /healthz /readyz /metrics, POST /ingest
```

* Incremental ingest: content hashes in `outputs/.ingest_manifest.json`;
  unchanged PDFs skip automatically (`--force` overrides). Config changes
  invalidate the manifest (filter change => reprocess).
* Qdrant (`docker compose up -d qdrant`, embeddings via host Ollama
  `nomic-embed-text`): semantic half of hybrid GraphRAG; indexed idempotently
  per ingest, scrubbed per document on delete.
* Review queue: every relation carries `confidence` (0..1 heuristic) +
  `review_status` (`verified/candidate/uncertain/contradicted`) in Neo4j +
  exports — weakest-first via `weakest_relationships()`, contradictions
  never silently merged.
* Boilerplate: repeated header/footer lines (≥3 pages) are stripped before
  NLP; scanned PDFs set `needs_ocr=True`.
* Backup: `./scripts/backup.ps1` (Windows) or `./scripts/backup.sh` (bash)
  tars the `neo4j_data` volume into `./backups`.
* Monitoring: `/healthz` (alive), `/readyz` (Neo4j reachable), `/metrics`
  (entity/relation counts + avg confidence); `docker compose ps/logs`.
* Image runs as non-root `appuser` with a `HEALTHCHECK`. Default-password
  use logs a warning — set a strong `NEO4J_PASSWORD`.
* Optional next models (not installed by default): `pip install fastcoref`
  + `coref.enabled: true` for pronouns; `GLiNER`/transformer NER via
  `SPACY_MODEL`. See `config.yaml`.

## GraphRAG (local or cloud LLM, RAG only)

Extraction stays spaCy/grammar — the LLM never writes to the graph. At ask
time we retrieve top-k subgraph facts (Neo4j, falling back to
`outputs/*.json`) and send them with a grounding prompt to your chosen model.

Provider selection (Ask tab, or `POST /ask` with `provider`/`model`/`api_key`):

| Provider | Endpoint | Key | Default model |
|---|---|---|---|
| `ollama-local` | your host (`localhost`, or `host.docker.internal` in Docker) | none | `llama3.1:8b` |
| `ollama-cloud` | `https://ollama.com/api` | `OLLAMA_API_KEY` | `gemma4:31b` |

```bash
ollama pull llama3.1:8b
docker compose up -d api          # POST /ask  {"question": "..."}
docker compose up -d frontend     # "Ask" tab: provider dropdown + model + key field
```

Cloud setup: create a key at ollama.com, then either set `OLLAMA_API_KEY`
in `.env` or paste it in the Ask tab per question (sent only with that
request, never written to disk or logs). Notes: cloud uses the same model
names as the library (`gemma4:31b`, no `-cloud` suffix needed) but speaks
the `/api/chat` endpoint (local uses `/api/generate`) — handled
automatically; and keep `OLLAMA_BASE_URL` unset in `.env` so containers
resolve `host.docker.internal` for local while cloud uses
`https://ollama.com/api`.

Knobs in `config.yaml:ollama` (`OLLAMA_BASE_URL`, `OLLAMA_MODEL`,
`top_k`, `max_context_chars`). Default is `llama3.1:8b` (accurate at
subject/object attribution; measured correct with citation on the sample
corpus). Drop to `OLLAMA_MODEL=llama3.2:3b` for speed — it answers faster
but fumbles attribution questions. Every answer ships with its evidence
facts, so verify from source.

Troubleshooting `RAG failed: Connection refused`: keep `OLLAMA_BASE_URL`
**unset** in `.env` — local code defaults to `localhost:11434` while
containers need `host.docker.internal:11434` (compose default). Pinning it
to `localhost` in `.env` breaks RAG inside Docker. Also ensure
`ollama serve` is running and the model is pulled (`ollama list`).

* Scanned PDFs: pipeline flags `needs_ocr=True` and continues with available
  text. Pre-process with OCRmyPDF/tesseract, then re-ingest.
* Backup: Neo4j data lives in the `neo4j_data` volume (`./backups` is mounted
  at `/backups`). Stop writes, then `docker run --rm -v kg_data:/data -v
  ./backups:/backups alpine tar czf /backups/neo4j-$(date +%F).tgz /data`.
* Monitoring: `docker compose ps`, `docker compose logs --tail=100 neo4j app
  frontend`; log rotation (10m×3) and `restart: unless-stopped` are baked in.
* Image runs as non-root `appuser` with a `HEALTHCHECK`.
* Next for scale: `fastcoref` pronouns, `GLiNER` domain NER, embedding dedup,
  Wikidata linking, async workers + GraphRAG search.

## Limitations (read before trusting output)

- Relationships are **candidates** from grammar, not verified facts. `review_status='candidate'`.
- Dependency parsing misses cross-sentence relations, coreference ("it", "the center"), and complex clauses.
- NER misses rare/domain terms; noun chunks compensate but add noise (`NOUN_PHRASE` — bridged to typed entities when alias/fuzzy matches).
- Entity resolution is 3-stage (canonical + alias always, rapidfuzz fuzzy when `use_fuzzy: true`). Still no coreference (`he`/`it`/`the company`) and no cross-document acronyms without shared tokens — `fastcoref` + embeddings are the next step.
- Numeric/date/money labels (`CARDINAL`, `DATE`, `MONEY`, ...) are dropped as nodes by default (`drop_entity_types`); copula verbs (`be/have/do/...`) are dropped as relations (`drop_predicates`). Tune both in `config.yaml`.
- Scanned PDFs set `needs_ocr=True` (see runbook for the OCRmyPDF pre-process step).
- Large PDFs are slower on CPU; `en_core_web_lg` ~500MB.

## Sample Output

See `outputs/` after a run (`entities.json`, `relationships.json`, `entities.csv`, `relationships.csv`).
Example: `"ABC Bank" -[:APPROVE]-> "Business Loan LN30002 for Priya Singh"` with evidence sentence + page.
