# High-Level Design (HLD)

This document describes **what each part of the system does** and how the
parts talk to each other. It does not go into code-level detail — that is in
`docs/lld.md`. Language is kept simple on purpose.

Related: `docs/architecture.md` (the big picture), `docs/lld.md` (the details).

## 1. Module map

Every Python file in `src/` owns exactly one job. Nothing else in the system
does that job.

| Module | Job in one line | Receives | Produces |
|---|---|---|---|
| `config.py` | Loads settings from `config.yaml` + environment | file path (optional) | settings dictionary |
| `pdf_processor.py` | Reads PDFs page by page | PDF path(s) | `PageSegment` list (text + doc + page) |
| `nlp_processor.py` | Runs spaCy once, parses all pages in batches | `PageSegment` list | (segment, parsed-doc) pairs |
| `entity_extractor.py` | Finds things (NER + noun phrases) | parsed docs | `ExtractedEntity` mentions |
| `relationship_extractor.py` | Finds subject–verb–object triples from grammar | parsed docs | `CandidateRelation` triples |
| `entity_resolver.py` | Merges duplicate mentions into one node each | mentions | `ResolvedEntity` nodes + mention index |
| `graph_builder.py` | Connects triples to nodes, builds stats/graph | nodes + triples | linked triples, counts, NetworkX graph |
| `neo4j_store.py` | Writes to / deletes from Neo4j | nodes + triples | write/delete reports |
| `graph_queries.py` | Ready-made read queries | a DB session + filters | rows (entities, relations, evidence) |
| `exporter.py` | Saves CSV + JSON copies | nodes + triples | file paths in `outputs/` |
| `pipeline.py` | Runs all of the above in order | PDF path(s) + flags | stats + nodes + triples |

On top of these sit two user interfaces that only *call* the modules:

- `main.py` — terminal commands (`--pdf`, `--input`, `--check-neo4j`, `--clear-neo4j`, ...).
- `frontend/app.py` — web page with Upload / Documents / Explore tabs.

## 2. Data objects (the three dataclasses)

The modules pass three simple data containers between each other:

1. **`PageSegment`** (`pdf_processor.py`) — one PDF page of text.
   Fields: `document` (file name), `page_number` (starts at 1), `text`,
   plus `is_empty` / `error` flags so bad pages are visible, never hidden.

2. **`ExtractedEntity`** (`entity_extractor.py`) — one mention of one thing.
   Fields: original `text`, `normalized_name` (lowercased, single spaces),
   `entity_type` (spaCy label like `PERSON`, or `NOUN_PHRASE`), `document`,
   `page_number`, the full `sentence` it came from, and character offsets.

3. **`CandidateRelation`** (`relationship_extractor.py`) — one extracted fact.
   Fields: `subject_text`, `predicate` (e.g. `"borrow from"`),
   `object_text`, `relation_type` (e.g. `"BORROW_FROM"`), `sentence`
   (evidence), `document`, `page_number`, `extraction_method`
   (`"spacy_dependency"`), `review_status` (`"candidate"`), `negated`
   (True/False). `subject_id` / `object_id` start empty and are filled in
   later when the ends are matched to merged nodes.

After resolution, mentions become **`ResolvedEntity`** objects: one stable
`id`, a display `name`, the full list of `mentions` (spellings seen),
`alternative_names`, and the `documents` / `pages` / `sentences` where it
appeared.

## 3. The pipeline stages (HLD view)

`run_pipeline(pdf, input_dir, cfg, write_to_neo4j, export)` in
`src/pipeline.py` executes these stages. Each stage uses exactly one module:

1. **Collect** (`collect_pdfs`) — accept one `--pdf` file and/or one `--input`
   folder; return a de-duplicated list of PDF paths. Missing paths raise a
   clear error immediately.
2. **Extract** (`process_pdfs`) — `extract_pdf_pages` per file → flat list of
   `PageSegment`s plus per-file summaries (page count, empty pages).
3. **Parse** (`process_segments_nlp`) — one spaCy model load, then `nlp.pipe`
   over all non-empty pages. Empty pages yield `(segment, None)` so their
   provenance survives even though there is nothing to parse.
4. **Entities** (`extract_entities`) — NER mentions first, then noun chunks
   that do not overlap them.
5. **Relations** (`extract_relationships`) — grammar triples per sentence.
6. **Resolve** (`resolve_entities`) — group mentions into nodes.
7. **Link** (`link_relations_to_entities`, then `ensure_endpoint_entities`,
   then link again) — match triple ends to nodes; create minimal
   `NOUN_PHRASE` nodes for ends that matched nothing, so no evidence is lost.
8. **Store** (`Neo4jStore.store`) — write nodes, arrows, and document links;
   return counts. Skipped with `--no-neo4j`.
9. **Export** (`export_all`) — write the four CSV/JSON files. Skipped with
   `--no-export`.
10. **Stats** (`graph_stats` + a throwaway NetworkX graph) — summarize what
    happened for the logs and the UI.

`check_neo4j(cfg)` is a tiny separate function: connect, say OK/FAILED.

## 4. Neo4j data model (HLD view)

- **Node labels:** `Entity` (things) and `Document` (PDF files).
- **Arrow types:** `:CONTAINS` (document → its entities) and one type per
  predicate (`:WORK_AT`, `:APPROVE`, `:BORROW_FROM`, `:BELONG_TO`, ... —
  the type is simply the normalized predicate).
- **Identity:** `Entity.id` is unique (database constraint) and deterministic
  — same normalized name + type always yields the same ID, so re-processing a
  PDF updates instead of duplicating. `Document.name` is unique too.
- **Deduplication:** writing the same fact twice merges into one arrow and
  only appends new evidence sentences / documents / pages.
- **Provenance:** every arrow stores its evidence sentences, source
  document(s), page(s), extraction method, review status, and negation flag.
  Every node stores which documents mention it. Nothing is stored without
  knowing where it came from.

## 5. Frontend design (HLD view)

`frontend/app.py` (Streamlit, port 8501) has three tabs and a sidebar:

- **Sidebar** — Neo4j connection status; live counts of documents, entities,
  relations; link to Neo4j Browser.
- **Upload tab** — file picker (PDF, multiple). New files are saved to
  `data/input/` (existing names are skipped, never overwritten), then each is
  processed by the same `run_pipeline()` the terminal uses.
- **Documents tab** — one row per document, merging two sources: files on
  disk and `Document` nodes in Neo4j (via `list_documents`). Each row shows
  whether the file exists, whether it is stored, and its entity/relation
  counts. Delete is two-step (Delete → Yes/No) and calls
  `delete_document_everywhere`: remove the file if present, then
  `Neo4jStore.delete_document` if the node exists.
- **Explore tab** — pick a stored document, see its relations with evidence
  sentences in expandable rows; plus a free-text entity search box.

The frontend holds no state of its own except the per-row delete
confirmations; everything else is read fresh from disk + Neo4j on every page
load, so it can never show stale data.

## 6. Delete design (HLD view)

Deleting a document must not damage other documents' data, because nodes and
arrows can be shared (e.g. "Rahul Sharma" appears in two PDFs).
`Neo4jStore.delete_document(name)` therefore works in five careful steps:

1. Delete arrows that came **only** from this document.
2. Scrub the document's name out of **shared** arrows (they stay, minus this
   source).
3. Delete the `Document` node (and its `:CONTAINS` arrows).
4. Scrub the document's name out of **entities'** document lists.
5. Delete **orphan** entities — nodes with no documents left *and* no arrows
   left.

It returns a report: how many arrows/nodes were deleted vs. scrubbed. See
`docs/lld.md` for the exact queries.

## 7. Configuration (HLD view)

Settings come from two places, merged by `config.py`:

- `config.yaml` — normal settings (model name, batch size, thresholds,
  output flags). It supports `${VAR:-default}` placeholders.
- Environment variables / `.env` file — secrets and deployment differences
  (`NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE`).

Rule: code never contains an address, password, or tuning constant directly —
everything flows through this one loader, so Docker, local runs, and tests
all configure the same way.

Main knobs: spaCy model, NLP batch size, OCR-warning threshold, noun-chunk
length limits, entity-resolution threshold + fuzzy on/off, Neo4j connection +
retry policy + write batch size, and output toggles (CSV/JSON).

## 8. Testing strategy (HLD view)

- **Unit tests** (default, no database needed): synthetic PDFs are generated
  on the fly with reportlab across five domains (business, healthcare,
  education, research, legal). They cover page provenance, entity extraction,
  relation extraction incl. negation/passive/fragments, conservative
  resolution rules, graph linking, store query structure (mocked driver), and
  the delete flow (mocked driver).
- **Integration test** (opt-in): `RUN_NEO4J_TESTS=1` runs a live round-trip
  against a real Neo4j.
- **Live verification** (manual, done after changes): process a real PDF
  through Docker, check counts and sample arrows with `cypher-shell`, check
  the frontend health endpoint.

## 9. Non-goals (what the system deliberately does NOT do)

- No OCR (scanned PDFs are reported, not read).
- No coreference ("he", "it", "the bank" are not linked to names).
- No cross-sentence relations (each fact comes from a single sentence).
- No fact-checking (output is candidates with evidence, for humans to review).
- No user accounts or authentication on the web UI (it is a local tool).
