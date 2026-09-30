# Low-Level Design (LLD)

This document explains **how each function works, step by step**. Read
`docs/architecture.md` first (the big picture) and `docs/hld.md` (what each
module does). Language is kept simple; grammar terms and Cypher clauses are
explained where they appear.

We trace one example sentence through the whole system in section 13:

> "Rahul Sharma borrowed money from ABC Bank."

## 1. `pdf_processor.py` — reading PDFs

**`PageSegment`** — a dataclass (a simple container) for one page:
`document` (file name only, e.g. `"report.pdf"`), `page_number` (starts at 1,
because humans count pages from 1), `text`, plus `char_count`, `is_empty`
(computed automatically after creation), and `error`.

**`extract_pdf_pages(pdf_path, ocr_threshold=50)`** — the steps:

1. Open the file with pypdf's `PdfReader`.
2. For each page (numbered from 1): try `page.extract_text()`. If it throws,
   keep the page anyway with `text=""` and record the error message — a bad
   page must never silently vanish.
3. Wrap each result in a `PageSegment`. Log a warning for every empty page.
4. After all pages: if the whole PDF has fewer characters than
   `ocr_threshold`, log that OCR may be needed (this is the classic sign of a
   scanned PDF — pictures of text, not real text).
5. If the file itself cannot be opened, return a `PDFResult` with `error` set
   instead of crashing.

**`collect_pdfs(pdf, input_dir)`** — builds the work list: one `--pdf` file
(must exist, else `FileNotFoundError`), plus every `*.pdf`/`*.PDF` in
`--input` (must be a folder, else `NotADirectoryError`), de-duplicated by
resolved path so passing both never processes a file twice.

**`process_pdfs(paths, ocr_threshold)`** — runs `extract_pdf_pages` per file,
returns the flat list of all segments plus per-file summaries, and logs
totals (files, pages, empty pages).

## 2. `nlp_processor.py` — parsing text with spaCy

**`get_nlp(model)`** — loads the spaCy model once and reuses it (module-level
cache). Loading takes seconds and hundreds of MB of RAM, so loading per page
would be painfully slow. If the model is missing, it raises an error telling
you the exact install command.

**`process_segments_nlp(segments, model, batch_size)`** — the steps:

1. Collect the text of every segment.
2. Skip empty texts for parsing (spaCy's `nlp.pipe` is built for real text),
   but remember each text's original position by index.
3. Run `nlp.pipe(texts, batch_size=...)` — batch parsing, much faster than one
   call per page.
4. Yield `(segment, parsed_doc)` pairs in original order. Empty segments yield
   `(segment, None)` — downstream code skips `None` docs but the segment's
   provenance was never lost.

## 3. `entity_extractor.py` — finding things

**`normalize_name(text)`** — collapse all whitespace to single spaces,
strip ends, lowercase. `"  ABC   Bank "` → `"abc bank"`. This is the key used
for all comparisons.

**`extract_entities_from_doc(doc, document, page_number, ...)`** — two passes:

*Pass 1 — named entities.* For each `ent` in `doc.ents`, store an
`ExtractedEntity` with the original text, normalized name, spaCy's label
(`PERSON`, `ORG`, `GPE`, `DATE`, `MONEY`, ...), document, page, the full
sentence it sits in, and character offsets. Remember its token span so pass 2
can avoid it.

*Pass 2 — noun chunks.* For each `chunk` in `doc.noun_chunks` (spaCy's guess
at "a thing described by several words", e.g. "financial services"), keep it
only if ALL of these hold: token count within `[min, max]` (default 1–6,
ignoring punctuation), text at least 2 characters, NOT made only of stopwords
("the", "a") or pronouns ("he", "it"), and NOT overlapping a pass-1 entity.
 survivors are stored with type `"NOUN_PHRASE"` and method
`"spacy_noun_chunk"`. This catches useful concepts NER misses, without
duplicating what NER found.

## 4. `relationship_extractor.py` — finding facts in grammar

Small vocabulary first (these are spaCy **dependency labels** — the grammar
role of each word):

- Subjects: `nsubj` ("Sharma" in "Sharma works"), `nsubjpass` (passive:
  "Finance is headquartered"), `csubj`/`csubjpass` (clausal subjects).
- Objects: `dobj`/`obj` (direct object: "money" in "borrowed money"), `attr`
  ("engineer" in "is an engineer"), `oprd`, `dative`.
- Prepositions: `prep` ("at", "from") → `pobj`/`obl` (the noun after it).
- `neg` — a negation word ("not", "never").
- `agent` — the "by ..." phrase in passive sentences.

**`normalize_relation(predicate)`** — `"borrow from"` → `"BORROW_FROM"`
(lowercase, non-alphanumerics become `_`, strip edge underscores, uppercase;
empty becomes `"RELATED_TO"`). This normalized form is what becomes the Neo4j
arrow type.

**`_expand_phrase(token)`** — turn one grammar node into readable text.
Take the token's whole subtree (it plus all words grammatically under it),
drop other verbs (they belong to other facts), and take the text span. Safety
cap: if the span is wider than 12 tokens, fall back to just compounds,
adjectives and the token itself ("ABC", "Technologies") so one node never
swallows half a sentence.

**`extract_relations_from_sentence(sent, document, page_number)`** — for every
verb/AUX token in the sentence:

1. Find its subjects (`SUBJ_DEPS`). If the verb is a `conj` (e.g. "founded"
   in "appointed X and founded Y") with no subject of its own, borrow the
   head verb's subject.
2. No subject → skip (a verb without a subject cannot make a fact — this is
   the "no inventing" rule).
3. Check negation (`neg` child anywhere on the verb).
4. For each subject × direct-object pair → one relation with
   `predicate = verb.lemma_` (base form: "borrowed" → `"borrow"`).
5. For each `prep → pobj` under the verb → one relation with
   `predicate = "verb lemma + preposition"` (`"borrow from"`, `"work at"`).
   A second branch handles Universal-Dependencies style (`obl` + `case`),
   producing the same shape.
6. Passive voice: if the subject is `nsubjpass`, also look for an `agent`
   ("by ...") child and emit agent → verb → subject, so "X was approved by
   the board" yields `board —approve→ X`.

Sentences with fewer than 3 real tokens are skipped up front.

## 5. `entity_resolver.py` — merging duplicates

**`deterministic_id(normalized, entity_type)`** — `sha256("ORG||abc bank")`,
first 12 hex chars, prefixed: `"entity_..."`. Same input → same ID across
runs and machines. No database lookup needed, no randomness.

**`resolve_entities(mentions, threshold, use_fuzzy)`** — group by the exact
key `(normalized_name, entity_type)`. "ABC Bank"/ORG and "abc bank"/ORG merge;
"Apple"/ORG and "apple"/PRODUCT do not (different type = possibly different
things). Per group it accumulates: all spellings (`mentions`), documents,
pages, sentences; the display `name` is the longest spelling seen (most
informative), demoted spellings move to `alternative_names`.

**Fuzzy merging (off by default)** — only when `use_fuzzy=True`, pairs of the
same type with `SequenceMatcher` similarity ≥ threshold merge, and ONLY if
they also share at least one meaningful word (`_content_tokens` drops "the",
"of", "and", ...). So "ABC Bank" / "ABC Banking Corp" may merge, but "ABC
Bank" / "XYZ Bank" never do (no shared content word... "Bank" is shared —
the similarity score then decides; the threshold stays high at 0.85).

**`build_mention_index(resolved)`** — dictionary from every known spelling
(normalized) to node ID, so relation ends can be matched to nodes.

## 6. `graph_builder.py` — connecting triples to nodes

**`link_relations_to_entities`** — for each triple, look up normalized
subject/object text in the mention index; fill `subject_id`/`object_id`.
Triples that match nothing keep `None` (they are not thrown away yet).

**`ensure_endpoint_entities`** — for every still-unlinked end, create a
minimal `NOUN_PHRASE` node (deterministic ID, carries document/page/sentence)
and point the triple at it. Rationale: extraction evidence must never be
lost just because NER missed a noun. Afterwards the pipeline links once more,
so every triple ends with both IDs set.

**`build_networkx_graph`** — optional in-memory `MultiDiGraph` (nodes keyed
by ID, edges carrying predicate/evidence). Used only for stats; Neo4j is the
real store.

**`graph_stats`** — counts: entities, relations, how many triples got both
ends linked.

## 7. `neo4j_store.py` — writing and deleting

Connection: `Neo4jStore(uri, username, password, database, batch_size,
max_retries, retry_delay)`. `connect()` retries with a pause between tries,
then raises `ConnectionError` naming the URI. Also usable as a context
manager (`with ... as store`). All Cypher uses parameters (`$doc`, `$sid`,
...); values never touch the query string — except the sanitized arrow type
(see below), because Cypher cannot parameterize types.

Startup: `init_db()` creates two constraints if missing — `Entity.id`
unique, `Document.name` unique. These are what make re-runs merge instead of
duplicate.

**`MERGE_ENTITY`** (per node): `MERGE (e:Entity {id})` then set all fields on
create; on match, union each list field (mentions, alternative names,
documents) with the new values instead of overwriting — `ON MATCH SET
e.mentions = [old ones not in new] + [new ones]`. This is how a second PDF
mentioning "ABC Bank" enriches the same node.

**`MERGE_DOCUMENT`** — same merge-or-enrich pattern for `Document` nodes
(union of page lists).

**`LINK_DOC_ENTITY`** — connect document to node. Written as `MATCH (d)
WITH d MATCH (e) MERGE (d)-[:CONTAINS]->(e)` — the `WITH` avoids a
cartesian-product warning Neo4j raises for two disconnected patterns.

**`sanitize_rel_type(t)`** — guarantees `^[A-Z][A-Z0-9_]*$`: uppercase,
non-alphanumerics → `_`, empty → `"RELATED_TO"`, leading digit → `"REL_"`
prefix. Anything hostile (quotes, spaces, Cypher keywords) collapses to
harmless characters. Unit-tested, including an injection attempt.

**`merge_relation_query(rel_type)`** — builds the per-type MERGE, with the
sanitized type backtick-quoted:

```cypher
MATCH (a:Entity {id: $sid}), (b:Entity {id: $oid})
MERGE (a)-[r:`WORK_AT` {predicate: $predicate, negated: $negated}]->(b)
ON CREATE SET r.relation_type = $rtype, r.evidence = [$evidence], ...
ON MATCH SET r.evidence = (... append only if new ...),
             r.source_documents = (... append only if new ...),
             r.pages = (... append only if new ...)
```

Dedup key = arrow type + predicate + negated between the same two nodes;
repeats only grow the evidence/document/page lists.

**`_store_entities`** — writes in batches (`batch_size`, default 500). Quirk
to know: the `Document` node is (re)merged from each entity's *first*
document with that entity's pages — harmless because merging is idempotent
and `_link_documents` afterwards links every entity to *all* its documents.

**`delete_document(doc_name)`** — five statements, all scoped by `$doc`
except the last (global by construction), counts read from the result
summary:

1. `DELETE_EXCLUSIVE_RELATIONS` — delete arrows whose source list contains
   only this doc (missing list falls back to the singular `source_document`
   for old data).
2. `SCRUB_SHARED_RELATIONS` — remove the doc from shared arrows' lists; if
   the singular `source_document` pointed at it, repoint to a remaining doc.
3. `DELETE_DOCUMENT_NODE` — `MATCH (d:Document {name}) DETACH DELETE d`.
4. `SCRUB_ENTITIES` — remove the doc from entities' document lists.
5. `DELETE_ORPHAN_ENTITIES` — delete nodes with no documents left AND no
   arrows left (`WHERE size(...) = 0 AND NOT (e)--()`).

Returns `relations_deleted / relations_scrubbed / documents_deleted /
entities_scrubbed / orphan_entities_deleted`. Shared data always survives.

**`clear_app_data()`** — the big red button: deletes all arrows, then all
`Entity` and `Document` nodes. Only called from `--clear-neo4j`, never
automatically.

## 8. `graph_queries.py` — reading (8 helpers)

All take a session, all parameterized, none specify an arrow type (so every
predicate type matches): `get_all_entities`, `get_all_relationships` (also
returns `type(r)` so callers see the edge label), `search_entities_by_name`
(case-insensitive substring), `find_relationships_for_entity`,
`entity_neighborhood` (paths of length 1–2 with node names + predicates),
`graph_for_document` (arrows from one doc, either source field),
`evidence_for_relationship` (evidence + documents + pages for one arrow),
`list_documents` (one row per `Document` with entity count via
`[:CONTAINS]` and arrow count via pattern comprehension on
`source_documents`).

## 9. `exporter.py`, `pipeline.py`, `main.py`, `frontend/app.py`

**`export_all`** — flattens nodes/triples into dict rows (lists joined with
`|` for CSV) and writes `entities.json`, `relationships.json`,
`entities.csv`, `relationships.csv` into `outputs/` (created if missing).
Returns the written paths.

**`run_pipeline`** — the 10-stage order from the HLD; `write_to_neo4j=False`
and `export=False` flags skip storing/exporting (used by `--no-neo4j`,
`--no-export`, and the frontend, which exports nothing). Returns
`{stats, entities, relations, pdf_results, exported}`.

**`main.py` flags** — `--pdf` (one file), `--input` (folder),
`--config` (custom yaml), `--check-neo4j` (connectivity test + exit),
`--no-neo4j`, `--no-export`, `--clear-neo4j` ( unités: wipe + exit).
Exit codes: 0 ok, 1 connection failed, 2 no input given.

**`frontend/app.py`** — helpers: `get_config` (cached), `make_store`,
`neo4j_ok`, `fetch_documents` (merges Neo4j `list_documents` rows with
`*.pdf` files on disk into one listing with `on_disk`/`in_neo4j` flags),
`delete_document_everywhere` (delete file if present + `delete_document` if
the node exists). Tabs: Upload (skip existing filenames, per-file spinner +
stats, then `st.rerun`), Documents (two-step delete via `st.session_state`
confirm keys, unique button keys per row), Explore (doc selectbox → relation
expanders with evidence; entity search box → dataframe). Sidebar shows
connection status and live counts. If Neo4j is unreachable the page stops
with an error message instead of crashing.

## 10. `config.py` — settings loading

`load_config(path)` reads YAML (or warns + continues on missing file),
expands every `${VAR:-default}` placeholder recursively through dicts/lists,
then fills Neo4j/model/batch defaults from environment variables. One loader
serves CLI, web UI, and tests.

## 11. Logging and errors (per area)

- PDF: per-page warnings (empty/unreadable), whole-file OCR warning, loud
  failure if the file won't open. Full document text is never logged.
- NLP: model load logged once; missing model raises with install command.
- Store: connection retries logged per attempt; doc-link failures warn but
  don't abort the batch; delete/clear log at WARNING with counts.
- Pipeline/frontend: per-stage counts logged (`Entities: N`,
  `Relationships: N`); the frontend surfaces exceptions per file and
  continues with the rest.

## 12. Tests (what guards what)

`tests/conftest.py` builds one synthetic PDF per domain (business,
healthcare, education, research, legal) with reportlab, once per session.
`test_pdf_processor` (provenance, blank pages kept, collect),
`test_entity_extractor` (every domain yields entities with doc/page/sentence;
noun chunks catch non-NER concepts), `test_relationship_extractor` (SVO
fields + evidence, negation flag, passive, fragment → no relations),
`test_entity_resolver` (exact merge, type separation, no fuzzy by default,
index), `test_pipeline` (linking + placeholders, parameterized store calls,
sanitizer incl. injection, per-type edge query), `test_delete_document`
(5 statements, 4 doc-scoped, result keys), `test_neo4j_integration` (live,
only with `RUN_NEO4J_TESTS=1`).

## 13. Worked example (one sentence, end to end)

Input: `"Rahul Sharma borrowed money from ABC Bank."` (page 1).

1. PDF → `PageSegment(document="x.pdf", page_number=1, text="...")`.
2. spaCy: `Rahul/Sharma` = PERSON (compound + head), `borrowed` = VERB/ROOT,
   `money` = NOUN/dobj, `from` = prep, `ABC/Bank` = ORG (compound + head).
3. Entities: "Rahul Sharma"/PERSON, "ABC Bank"/ORG, plus noun chunk
   "money"/NOUN_PHRASE.
4. Relations: subjects of `borrowed` = [Sharma]; objects = [money] →
   `Rahul Sharma —borrow→ money`; preps: `from → Bank` →
   `Rahul Sharma —borrow from→ ABC Bank`. No `neg` child → `negated=False`.
5. Resolution: three nodes with deterministic IDs, e.g. `entity_<hash>`.
6. Linking: both triple ends match the index.
7. Neo4j: `MERGE (a)-[r:`BORROW` {predicate:"borrow",...}]->(b)` and
   `(a)-[r:`BORROW_FROM` {predicate:"borrow from",...}]->(b)`, each with the
   sentence as evidence. Open Neo4j Browser: two labeled arrows —
   `BORROW` and `BORROW_FROM` — exactly like the reference picture.
