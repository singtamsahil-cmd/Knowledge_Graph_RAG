# System Architecture

This document explains how the whole system fits together. It is written in
simple language. No prior knowledge of knowledge graphs is assumed.

## 1. What the system does (in one paragraph)

You give the system PDF files. It reads the text, finds the important things
mentioned in the text (people, companies, places, dates, accounts, and other
concepts), figures out how those things relate to each other from the grammar
of each sentence (for example, "Rahul Sharma works at ABC Technologies" gives
`Rahul Sharma —WORK_AT→ ABC Technologies`), and saves everything as a graph in
a Neo4j database. You can then browse, search, and delete documents through a
web page, or look at the graph visually in Neo4j Browser.

A **knowledge graph** is just a set of dots (nodes) connected by arrows
(edges). Each dot is a thing. Each arrow is a fact found in a sentence, stored
together with the sentence itself as proof (called **evidence**).

## 2. Big picture

```
                        +---------------------+
                        |   Your PDF files    |
                        |   data/input/*.pdf  |
                        +----------+----------+
                                   |
                    +--------------+---------------+
                    |                              |
           +--------v--------+            +--------v--------+
           |  CLI (main.py)  |            | Web UI (Streamlit|
           |  Terminal use   |            | port 8501       |
           +--------+--------+            +--------+--------+
                    |                              |
                    +--------------+---------------+
                                   |
                        +----------v----------+
                        |     Pipeline        |
                        |  (src/pipeline.py)  |
                        |  PDF -> NLP ->      |
                        |  entities ->        |
                        |  relations -> graph |
                        +----------+----------+
                                   |
                    +--------------+---------------+
                    |                              |
           +--------v--------+            +--------v--------+
           |  Neo4j database |            | CSV + JSON files|
           |  (the graph)    |            | (outputs/)      |
           +-----------------+            +-----------------+
```

There are two ways to use the system (terminal or web page), but both run the
same pipeline and write to the same Neo4j database.

## 3. The Docker services

The system runs as separate programs (called **services**) managed by
Docker Compose. Each service does one job:

| Service    | What it is                        | Port | Job |
|------------|-----------------------------------|------|-----|
| `neo4j`    | Neo4j 5 Community database        | 7474 (browser), 7687 (data) | Stores the graph permanently |
| `qdrant`   | Qdrant vector index               | 6333 | Semantic search over relation evidence |
| `app`      | Python batch program (`main.py`)  | none | Processes PDFs from the terminal |
| `frontend` | Streamlit web page (`frontend/app.py`) | 8501 | Upload, browse, and delete PDFs in a browser |
| `api` | FastAPI (`src/api.py`) | 8000 | Health/readiness, metrics, ingest trigger, GraphRAG |

Important rules of this design:

- **Only Neo4j stores data.** The `app` and `frontend` services keep nothing
  themselves. If you delete and restart them, your graph is still there,
  because Neo4j keeps its files in a named Docker volume (`neo4j_data`) that
  survives restarts. `docker compose down` does NOT delete that volume.
- **Services talk over the Docker network.** Inside Docker, the Python code
  connects to `bolt://neo4j:7687` (the service name `neo4j` is the address).
  On your own computer (outside Docker) the same code uses
  `bolt://localhost:7687`. This switch happens through the `NEO4J_URI`
  setting — you never hardcode addresses in the code.
- **`app` only reads PDFs; `frontend` can also save them.** The `app` service
  mounts `data/input` as read-only. The `frontend` service mounts it
  read-write so uploads from the browser land in the same folder.

## 4. How data flows (the pipeline)

Every PDF goes through the same 9 stages, in order:

1. **Collect** — find the PDF file(s) to process (`collect_pdfs`),
   skipping files unchanged since last ingest via content hashes in
   `outputs/.ingest_manifest.json` (`--force` overrides).
2. **Extract text** — read the PDF page by page with pypdf. Every chunk of
   text remembers its document name and page number. Empty pages are kept and
   reported, never silently dropped; repeated header/footer lines (≥3 pages)
   are stripped as boilerplate; near-zero-text PDFs are flagged
   `needs_ocr=True` (`pdf_processor.py`).
3. **Language analysis** — run the text through spaCy (`en_core_web_lg`) in
   batches. Each page becomes a parsed document with words, sentences, entity
   labels, and grammar links (`nlp_processor.py`).
4. **Find entities** — take spaCy's named entities (PERSON, ORG, GPE, DATE,
   MONEY, ...) plus useful noun phrases spaCy did not label, e.g. "financial
   services" (`entity_extractor.py`).
5. **Find relations** — look at the grammar of each sentence and pull out
    subject–verb–object triples such as `Rahul Sharma —borrow from→ ABC Bank`.
    Every triple keeps its sentence as evidence plus a `confidence` score
    (0..1 heuristic: verb specificity + endpoint shape), stored in Neo4j and
    exports for the review queue (`relationship_extractor.py`).
6. **Merge duplicates** — canonical normalization + alias + fuzzy merge
   into one node with one stable ID, remembering every original spelling
   (`entity_resolver.py`). Example: `"The ABC Bank Ltd."`, `"ABC Bank's"`,
   `"ABC Bank"` → `abc bank`; `"Dr. Sharma"` → `sharma`;
   `"Priya"` → `"Priya Singh"` (subset alias);
   `"NSF"` → `"National Science Foundation"` (acronym);
   `NOUN_PHRASE "ABC Bank"` bridges into `ORG "ABC Bank"`.
7. **Validate** — drop evidence-less relations, merge exact duplicates,
   flag contradictions (affirmed-vs-negated, approved-vs-pending on the same
   target) and tier confidence into `verified/candidate/uncertain/contradicted`
   (`validate.py`). Dates in evidence attach as `event_date`.
8. **Link and store** — connect each relation's two ends to the merged nodes
   and write nodes + arrows into Neo4j (`graph_builder.py` + `neo4j_store.py`).
   Optionally build first-class `Event` nodes (`events.enabled`) with
   `PARTICIPATED_IN` edges. Evidence points are upserted into Qdrant
   (`vector_store.py`, embeddings from host Ollama `nomic-embed-text`).
9. **Export (optional)** — also save `entities`, `relationships` (and `events`
   when enabled) as CSV and JSON files in `outputs/` (`exporter.py`).

The pipeline function is `run_pipeline()` in `src/pipeline.py`. Both the
terminal program and the web page call it — there is only one implementation,
so both behave identically.

## 5. How the graph looks in Neo4j

Five kinds of items exist across the stores (three in Neo4j, one opt-in, one in Qdrant):

- **`Entity` nodes** — one per real-world thing. Example properties:
  `name: "Rahul Sharma"`, `entity_type: "PERSON"`, plus the list of documents
  it appeared in. The node ID (`id: "entity_..."`) is unique and stable: the
  same canonical name always gets the same ID, so re-running a PDF never creates
  duplicates. Canonical = lowercase, no articles/honorifics/possessives, no
  trailing Corp/Inc/Ltd suffixes.
- **Relationship arrows** — one per extracted fact. The arrow's *type* is the
  predicate itself: `:WORK_AT`, `:APPROVE`, `:BORROW_FROM`, `:BELONG_TO`.
  That is why Neo4j Browser labels edges with the real relation text. All the
  details (evidence sentences, source document, page, negation flag,
  confidence, event_date, review_status) live in the arrow's properties.
- **`Event` nodes** (opt-in via `events.enabled`) — dated occurrences with
  `PARTICIPATED_IN` edges carrying subject/object roles.
- **Qdrant points** — one vector per relation evidence (`kg_evidence`
  collection, deterministic UUID ids), the semantic half of hybrid retrieval.
- **`Document` nodes** — one per PDF, connected to its entities with
  `:CONTAINS` arrows. These exist so the system always knows *which file* each
  fact came from, which is what makes per-document delete possible.

Example (exactly as stored):

```cypher
(:Entity {name: "Rahul Sharma"})-[:WORK_AT {
  predicate: "work at",
  evidence: ["Rahul Sharma works at ABC Technologies."],
  source_document: "kg_test_document.pdf",
  page_number: 1,
  negated: false,
  review_status: "candidate"
}]->(:Entity {name: "ABC Technologies"})
```

## 6. Key design decisions (and why)

- **No LLMs, no paid APIs.** Everything runs locally with spaCy. This keeps
  the system free, private (documents never leave your machine), and
  reproducible (same input → same output).
- **No domain rules.** The extractor only understands grammar (subjects,
  verbs, objects, prepositions). It works on banking, medical, legal, or
  technical PDFs without changes — but it also means output quality depends on
  how clearly the sentences are written.
- **Relations are candidates, not facts.** Grammar alone can be wrong
  (negation, sarcasm, complex clauses). Every arrow carries
  `review_status: "candidate"` and its evidence sentence, so a human can
  always check *why* the system claims something.
- **3-stage merging (safe → risky).** (1) Exact canonical match
  (`The ABC Bank Ltd.` → `abc bank`). (2) Always-on alias merge:
  subset (`Priya` → `Priya Singh`), acronyms (`NSF` → its full name),
  `NOUN_PHRASE` ↔ typed bridging. (3) Fuzzy merge via `rapidfuzz`
  token_set_ratio + shared-token guard (on by default, `use_fuzzy: true`,
  threshold 0.85). Different real types never merge; generic singletons
  (`bank`, `company`) never merge by subset alone — so `ABC Bank` and
  `ABC Finance` stay separate.
- **Surgical delete.** Entities can be shared between documents, so deleting
  one PDF removes only what came solely from it and scrubs shared items,
  instead of blindly deleting nodes other documents still need.
- **One pipeline, two front-ends.** The CLI and the web UI share
  `run_pipeline()`, `Neo4jStore`, and the queries — a bug fix helps both.

## 7. Failure handling (summary)

- Neo4j is down → the store retries the connection (configurable count and
  delay), then raises a clear error. The web UI shows "Not reachable" instead
  of crashing.
- A PDF page has no text → the page is kept with empty text and a warning is
  logged. If a whole PDF has almost no text, the log says OCR may be needed
  (for scanned PDFs).
- A sentence has no usable grammar → no relation is created. The system
  prefers missing a fact over inventing one.
- Deleting a document that does not exist → file step is skipped, Neo4j step
  finds nothing, and an empty report is returned. Nothing breaks.

## 8. Where to read next

- `docs/hld.md` — what each module does, its inputs and outputs.
- `docs/lld.md` — how each function works, step by step, including the Cypher
  queries.
