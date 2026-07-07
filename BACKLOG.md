# Deferred Work / Backlog

Things we consciously postponed to keep momentum, so they don't get lost. Each
item notes *why* it's deferred and *how* we'd do it. Not bugs — planned
follow-ups and quality improvements.

_Last updated: 2026-07-07_

---

## Vector / Multimodal ingestion

### 1. Link images to their section (multimodal, Tier 3)
**Status:** images are extracted and saved to disk, but stored as a *flat list*
on the document — no link to the section/heading they appear in.
**Why deferred:** the current stage is Tier-1 text chunking; image→section
linkage is Tier-3 multimodal work (spec 5.7).
**How:** enable one of —
- *Positional* (preferred, accurate): persist each image's `page` + `bbox` from
  the LlamaParse result, then link an image to the section whose text span
  contains its bbox. Requires a small `parser.py` change to keep bbox/page
  (currently dropped in `ParsedImage`).
- *Caption-matching* (simpler, partial): match the "Figure N:" caption lines
  already in the parsed text to images by number.
Then build the **text sidecar** per image (caption + parent heading) for BM25,
embed the image via Gemini Embedding 2, and link it to the parent `Project`
node in the graph (spec 5.7).

### 2. Contextual-retrieval prefix per chunk (spec 5.4 step 6)
**Status:** not implemented. We embed raw chunks first.
**Why deferred:** it's a Gemini 2.5 Flash call per chunk; embedding raw chunks
first gives a clean **before/after** recall comparison (which the eval phase
wants — spec 10 Phase B), and keeps the chunk-review stage LLM-free.
**How:** for each chunk, Gemini 2.5 Flash generates a 1–2 sentence situating
blurb ("From the MedSumm paper, explaining the multimodal encoder…"); prepend it
to the chunk text *before embedding only*. Keep the raw chunk stored separately
for display/citation. Measure recall before/after.

### 3. Populate `linked_entities` + `project_tag` (the chunk↔graph bridge)
**Status:** both empty/None on every chunk.
**Why deferred:** needs the graph loaded and an entity-linking pass; not required
to get vector retrieval working.
**How:** match chunk text against canonical graph entity ids (names + aliases)
to fill `linked_entities`; set `project_tag` to the matching Neo4j `Project` id.
For the ECIR paper, link to the `Publication` node (`medsumm`), not a Project.
This coupling is what makes it GraphRAG rather than a bolted-on graph (spec 5.2).

### 4. Reranker (Tier 2 quality lever, spec 6.1)
**Status:** not added. Base hybrid (dense + sparse + fusion) first.
**How:** add a cross-encoder reranker (Cohere Rerank or `bge-reranker`) after
fusion, once the base pipeline is measurable.

### 5. Real token counting
**Status:** `tokens.py` uses a chars/words heuristic.
**How:** swap for Gemini's `count_tokens` or a bundled BPE if exactness ever
matters for context budgeting. Fine as-is for chunk sizing.

---

## Graph ingestion

### 6. Weak entity recall for 2 projects
**Status:** known gap from the graph load. `Concept-to-Catwalk` got 1
USED/DEMONSTRATES edge and `Product Discovery AI Assistant` got 2, because the
(deliberately strict, anti-noise) entity pass didn't extract their
domain-specific skills (e.g. "co-occurrence clustering", "preference-aware
memory system") on a free-tier model.
**Why deferred:** chose to load as-is rather than keep iterating prompts against
`flash-lite`'s limits.
**How:** re-run the entity pass with a stronger model (`gemini-2.5-flash` or Pro
once billing is on), or add a project-scoped entity pass mirroring the
per-project edge pass, so each project's skills are pulled out explicitly.

### 7. Personal narrative doc not yet written
**Status:** graph built from resume only; `temp_data/narrative.md` absent.
**Impact:** most relationship richness (`LED`/`USED`/`DEMONSTRATES`,
project→outcome, "common thread" synthesis) lives in the narrative, not the
resume bullets (spec 5.3).
**How:** write it with the spec 5.3 per-project template, then re-run
`python -m app.ingestion.graph.pipeline --model gemini-2.5-flash` (picks it up
automatically). Also feeds the vector path.
