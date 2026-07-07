# Knowledge-Graph Ingestion — How It Works

Status: built, debugged, and successfully loaded into Neo4j Aura (75 nodes / 38 relationships from the resume). This doc explains the code so you can review it before we move to the next step (vector-store ingestion).

## The big picture

The goal: turn career documents (resume, later a personal narrative) into a typed graph in Neo4j — `(Person)-[:WORKED_AT]->(Company)`, `(Project)-[:USED]->(Technology)`, etc. — that a Q&A assistant can traverse instead of re-reading prose every time.

The pipeline is a straight line, one file per stage:

```
sources.py  →  extractor.py  →  resolver.py  →  cypher.py  →  loader.py
(load docs)    (LLM passes)     (dedupe)        (render)      (write to Neo4j)
```

All of it is orchestrated by `pipeline.py`, run as `python -m app.ingestion.graph.pipeline`.

## 1. `app/core/gemini.py` — the shared LLM client

One function everything else calls: `generate_structured(prompt, schema, model, ...)`.

- Wraps `google-genai`, forcing JSON output constrained to a Pydantic schema (`response_schema=schema`). This is what lets the extractor ask for `list[ExtractedEntity]` and get back validated objects, not raw text to parse.
- Retries on 429 (free-tier rate limit) **and** 5xx errors — you were hitting `503 UNAVAILABLE` a lot on the free tier, so `_is_retryable` treats that the same as a rate limit and backs off exponentially (15s → 60s, up to 5 attempts).
- Model defaults to `gemini-2.5-flash-lite` (free tier), but every call accepts `model=` so it can be overridden — see the model note below, this matters a lot for extraction quality.

## 2. `app/ingestion/sources.py` — loading documents with provenance

- `SourceDoc` wraps a document's text with a marker like `[RESUME]...[/RESUME]`. Every fact the LLM extracts later gets tagged with which marker block it came from, so you can trace any graph fact back to its source document.
- `load_sources()` walks a small registry (`DEFAULT_SOURCES`) of `(SourceType, path)` pairs and only loads files that exist. Today only `temp_data/main.md` (resume) exists; `temp_data/narrative.md` is checked for automatically — once you write it, it's picked up with zero code changes.
- `build_marked_corpus()` just concatenates all loaded docs into one big string, each still wrapped in its marker block. This is the exact text fed to every LLM pass.

## 3. `app/ingestion/graph/schema.py` — the fixed ontology

This file is the contract the LLM must operate inside — it can't invent node or edge types outside of it.

- **8 node types**: Person, Company, Role, Project, Skill, Technology, Publication, Achievement.
- **9 relationship types**, each with a fixed `(source type) -> (target type)` pair in `RELATION_SCHEMA`, e.g. `USED: (Project)->(Technology)`, `WORKED_AT: (Person)->(Company)`.
- **Extraction wire models**: `ExtractedEntity` and `ExtractedRelationship` are the Pydantic shapes the LLM fills in. Note `properties` is a `list[KVProperty(key, value)]` rather than a dict — Gemini's structured output can't do free-form JSON maps, so key/value pairs are flattened to a dict later in code (`property_dict()`).
- `ProposedGraph` bundles a full extraction (`entities` + `relationships`) — this is the object passed between every stage.

## 4. `app/ingestion/graph/extractor.py` — the extraction logic (the core of the work)

This is genuinely **three LLM passes**, not two, and the third one exists because of a debugging problem worth understanding.

**Pass 1 — entity pass** (`entity_pass`): Feed the whole marked corpus, ask for a deduplicated list of entities strictly typed against the 8 node types. The prompt is deliberately strict about *not* creating noise entities — e.g. resume section headings like "LLM & AI Systems" or generic nouns like "biotech" must not become fake Skill nodes. It also asks the model to merge surface variants ("AWS Bedrock" / "Bedrock" → one node) before emitting, with the losers listed as `aliases`.

**Pass 2 — general relationship pass** (`relationship_pass`): Feed the entity list back in, ask the model to connect them with the "administrative" edge types — WORKED_AT, HELD_ROLE, AT, LED, PUBLISHED, WON, PART_OF. Explicitly **excludes** USED and DEMONSTRATES (see pass 3). After the LLM responds, `_validate_edges()` drops any edge whose endpoints don't exist or whose types don't match `RELATION_SCHEMA` — this is a hard structural filter, not just a prompt instruction.

**Pass 3 — per-project edge pass** (`project_scoped_edges_pass`): Run once *per Project entity*. Instead of asking the model "who does this technology belong to," it asks a narrower question: "given this one project and the full list of Technology/Skill candidates, which ones belong to it?" The model returns only `target_id` + confidence — **it is never asked for `source_id` at all**. The code hardcodes `source_id=project.id` when building the edge.

### Why pass 3 exists — the debugging story

The first attempt used a single combined relationship pass (asking for USED/DEMONSTRATES alongside everything else) on `gemini-2.5-flash-lite`. Result: 137 entities extracted fine, but only 35 of 101 relationship edges survived schema validation. The failure mode was consistent: the model kept emitting edges like `(Person)-[:USED]->(Technology)` instead of the required `(Project)-[:USED]->(Technology)` — i.e., it kept attributing a project's tech stack directly to the person, skipping the project node.

Rewriting the prompt to be more explicit about direction did not fix it — same ~65% failure rate. That ruled out "prompt wording" as the cause and pointed to a genuine reasoning-capacity limit of the lite model on this specific relational structure.

The fix was structural, not verbal: stop giving the model the option to get the direction wrong. In pass 3, the model is never asked to supply `source_id` — the code fixes it to the current project. This makes the wrong-direction bug **impossible by construction**, not just discouraged.

### Model note
`gemini-2.5-flash-lite` is the default everywhere (free tier). But the run that actually produced the 75-node/38-edge graph used `gemini-2.5-flash` (not lite) — quota happened to be available, and it produced zero schema violations vs. lite's ~35% edge loss. If you re-run ingestion, prefer passing `--model gemini-2.5-flash`.

## 5. `app/ingestion/graph/resolver.py` — the dedupe safety net

The entity pass already merges obvious duplicates, but this is a second, code-based (non-LLM) pass that catches what it missed.

- `_normalize()` lowercases a name, strips punctuation, and drops legal-suffix noise ("pvt", "ltd", "inc", ...).
- Entities of the *same type* whose normalized canonical name or any alias matches are unioned together via a small `_UnionFind` (union-find / disjoint-set) structure.
- `_merge_entities()` then folds each group into one surviving entity — the one with the most `source_spans` (most evidence), ties broken by longest name — and unions in the aliases/properties/spans/sources from the others.
- All relationship endpoints are rewritten onto the surviving ids, and `_dedupe_edges()` collapses any now-duplicate `(source, type, target)` edges (also dropping accidental self-loops created when a merge collapses two entities that had an edge between them).
- There's a hook for embedding-similarity dedupe (`embed_fn`) for later — not wired up yet since the vector-store path (Gemini Embedding 2) hasn't started.

## 6. `app/ingestion/graph/cypher.py` — rendering, not writing

This module turns a `ProposedGraph` into two artifacts, but does **not** touch Neo4j itself:

- `render_review_table()` — a Markdown file (`review.md`) with two tables (entities, relationships) for you to read and sanity-check before approving anything.
- `render_cypher_script()` / `build_statements()` — the actual Cypher, all `MERGE` (never `CREATE`) keyed on `id`, so reloading is always idempotent. Every node/edge gets provenance fields baked in (`source_docs`, `source_spans`/`source_span`, `extracted_at`), which is what will let a future guardrail cite "this fact came from the resume" when answering questions.
- Important design point: the `.cypher` file is exactly what gets executed at load time — nothing is re-derived or re-generated between "review" and "load," so what you read is what runs.

## 7. `app/ingestion/graph/loader.py` — the actual Neo4j write

- `get_driver()` builds a `neo4j+s://` driver from settings (Aura).
- `load_statements()` runs every statement from `cypher.build_statements()` in its own autocommit transaction (Neo4j requires this — schema statements like `CREATE CONSTRAINT` can't share a transaction with data statements), tallying counters (`nodes_created`, `relationships_created`, etc.) and collecting any per-statement errors without aborting the whole run.
- `graph_summary()` is a post-load sanity check — total nodes/edges plus a per-label breakdown, used to print the final "what's actually in Neo4j now" summary.
- `DEFAULT_DATABASE` reads from `settings.neo4j_database` rather than hardcoding `"neo4j"` — this mattered because on Aura, the database name equals the instance id (e.g. `3eed3487`), not the literal string `"neo4j"`. That was one of two Aura-specific config bugs found and fixed (the other: Aura's username is also the instance id).

## 8. `app/ingestion/graph/pipeline.py` — the CLI, tying it together

Two-step, manual-approval flow — nothing touches the database until you explicitly say so:

```
python -m app.ingestion.graph.pipeline          # extract only, writes review artifacts
python -m app.ingestion.graph.pipeline --load    # loads the reviewed JSON into Neo4j
```

- Default (no `--load`): `build_proposed_graph()` runs `load_sources → build_marked_corpus → extract_graph → resolve`, then `write_artifacts()` writes three files to `temp_data/graph_review/`:
  - `proposed_graph.json` — the full envelope (`extracted_at`, `model`, `graph`) — this is the **source of truth for loading**, not a re-extraction.
  - `review.md` — for you to read.
  - `load.cypher` — for reference/audit.
- `--load`: reads back `proposed_graph.json` (not a live re-extraction — so what loads is exactly what you reviewed, with no drift from re-running the LLM), verifies Neo4j connectivity, runs `load_graph()`, and prints a before/after summary via `graph_summary()`.
- This two-step gate is deliberate: extraction is non-deterministic (LLM output) and costs API quota, so you review the Markdown once and the load step never re-calls Gemini.

## Current state

75 nodes / 38 relationships loaded into Aura: 1 Person, 9 Company, 3 Role, 3 Project, 11 Skill, 41 Technology, 1 Publication, 6 Achievement — from the resume only (narrative not yet written).

**Known, accepted gap**: entity-pass recall is uneven across projects. One project got 12 rich USED/DEMONSTRATES edges; two others got only 1–2, because the (deliberately tightened, anti-noise) entity prompt didn't pull out some of their more specific domain skills on this run. This was a conscious call to load as-is on the free-tier model and revisit with a stronger model later, rather than keep iterating prompts against `flash-lite`'s limits.

## Not started yet

Vector-store ingestion (Qdrant + Gemini Embedding 2). `sources.py`'s `SourceDoc` abstraction is meant to feed that path too, and once `temp_data/narrative.md` exists, re-running the graph pipeline picks it up automatically with no code changes.
