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
  -> conservative entity resolution -> NetworkX (optional) -> Neo4j (persistent)
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
├── frontend/app.py         # Streamlit web UI (Upload / Documents / Explore)
├── src/
│   ├── config.py           # settings loader (YAML + environment)
│   ├── pdf_processor.py    # page-by-page text extraction with provenance
│   ├── nlp_processor.py    # spaCy model loading + batch parsing
│   ├── entity_extractor.py # NER + noun-phrase mentions
│   ├── relationship_extractor.py  # grammar triples (SVO, prep, passive, neg)
│   ├── entity_resolver.py  # duplicate merging, stable IDs
│   ├── graph_builder.py    # link triples to nodes, stats
│   ├── neo4j_store.py      # write / delete / clear in Neo4j
│   ├── graph_queries.py    # ready-made read queries
│   ├── exporter.py         # CSV + JSON exports
│   └── pipeline.py         # runs all stages in order
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

A Streamlit app at **http://localhost:8501** with three tabs:

```bash
docker compose up -d neo4j frontend
```

- **Upload** — drop in any PDFs; they are saved to `data/input/` and run through
  the full pipeline into Neo4j.
- **Documents** — every PDF on disk and every `Document` node in Neo4j, with
  per-document entity/relation counts. **Delete** removes the file *and* that
  document's Neo4j data (two-step confirm).
- **Explore** — per-document relations with evidence sentences, plus entity search.

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
| `entity_resolution.similarity_threshold` | fuzzy threshold (fuzzy off by default) |
| `extraction.use_noun_chunks` | capture non-NER concepts |

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
```

Synthetic multi-domain fixtures (business, healthcare, education, research, legal) are generated on the fly in `tests/conftest.py` with reportlab.

## Limitations (read before trusting output)

- Relationships are **candidates** from grammar, not verified facts. `review_status='candidate'`.
- Dependency parsing misses cross-sentence relations, coreference ("it", "the center"), and complex clauses.
- NER misses rare/domain terms; noun chunks compensate but add noise (`NOUN_PHRASE`).
- Entity resolution is conservative: exact normalized match by default; enable `use_fuzzy` cautiously.
- Scanned PDFs need OCR (we log a warning when extractable text is near-zero).
- Large PDFs are slower on CPU; `en_core_web_lg` ~500MB.

## Sample Output

See `outputs/` after a run (`entities.json`, `relationships.json`, `entities.csv`, `relationships.csv`).
Example: `"ABC Bank" -[:APPROVE]-> "Business Loan LN30002 for Priya Singh"` with evidence sentence + page.
