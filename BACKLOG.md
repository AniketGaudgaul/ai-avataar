# Deferred Work / Backlog

Things we consciously postponed to keep momentum, so they don't get lost. Each
item notes *why* it's deferred and *how* we'd do it. Not bugs — planned
follow-ups and quality improvements.

_Last updated: 2026-07-09_

---

## Vector / Multimodal ingestion

### 1. ~~Link images to their section~~ — **DONE (2026-07-09)**
Implemented in `app/ingestion/vector/images.py`: override file → inline
`![caption](uri)` ref → opt-in vision caption-match, with an unlinked fallback.
Sidecar (`heading_path` + `Figure N: caption`) feeds BM25 and is fused into the
dense vector; 9 ECIR figures embedded + retrieved rank-1 on 4/4 cross-modal
queries. `parser.py` now persists `page_number`.

*bbox positional linking was not available* — LlamaParse v2 returns no bbox on
image metadata, only a filename encoding the page. Filename/positional **order is
not a valid signal** (`page_7_table_1` = Figure 2, `page_7_chart_1` = Figure 3).

**Residual gaps** (small):
- **Image → `Project` graph edge** is still not created, so text → graph → image
  traversal doesn't work yet. Do it with the `linked_entities` pass (item 3).
- **Caption-to-heading drift**: a caption line typeset after a section break gets
  that later section's `heading_path` (Figure 5 / Table 2 read as "6 Ethical
  Considerations"). The caption itself dominates the sidecar, so retrieval is
  unaffected; fix by attaching a caption to the section of the *paragraph that
  references it* ("see figure 5") rather than the section it physically sits in.
- One ECIR image (`page_11_image_1_v2.jpg`) has no caption and is intentionally
  `unlinked` in `temp_data/vector_review/image_links.json`.

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

---

## Agentic layer

### 8. "How I Built This" is just a normal RAG doc — keep it that way
**Status:** the meta lane ("how was this built") is served by the Career Q&A
agent with a `source_type=how_i_built_this` vector filter — the doc is chunked +
embedded like any other source, NOT special-cased or fed whole into the prompt.
**Decision:** deliberately keep it as a normal ingested doc (one consistent
pipeline, nothing bespoke to maintain). Do NOT turn it into a separate code
path.
**How to make it good:** just author `how_i_built_this.md` with the same
heading-per-concern structure as project docs (Ingestion → Retrieval → Agents →
Guardrails, mirroring the spec sections) so chunking yields clean, self-contained
sections. No code change.

### 9. ~~project_tag retrieval filter is inert~~ — **ACTIVE (2026-07-09)**
Both project write-ups are ingested with `project_tag` stamped
(`agentic-rag-presentation-generator`, `product-discovery-ai-assistant`), validated
against the Neo4j project catalog by `pipeline._project_tag_exists` so a typo
fails loudly instead of silently matching zero chunks. The router's filter and the
abstract-first flow now have real data behind them. The ECIR paper still has an
empty `project_tag` (it links to a `Publication`, not a `Project`).
The "maybe" below is still open: decide whether a `project_tag` filter yielding 0
vector hits should fall back to unfiltered retrieval.

<details><summary>Original entry</summary>
**Status:** the router now resolves a query to a `project_tag` (from the Neo4j
project catalog) and the retrieval layer filters Qdrant on it — verified live
(e.g. "tell me about the Agentic RAG Presentation Generator" → tag
`agentic-rag-presentation-generator`). BUT the only ingested chunks are the ECIR
paper, whose `project_tag` is empty, so the filter matches **zero** contexts and
a project deep-dive falls back to graph facts only (and can end in a refusal).
**Why:** infrastructure built ahead of the data. Activates automatically once
project docs are ingested with matching `project_tag`s (see item 3).
**How:** ingest the project write-ups (S2) with `project_tag` = the Neo4j
`Project` id. No agent-code change needed — the filter + router already work.
**Maybe:** consider whether a project_tag filter that yields 0 vector hits should
gracefully fall back to unfiltered retrieval, or the specialist should say "I
don't have that project's write-up yet" instead of the generic refusal.
</details>

### 10. Graph retrieval is 1-hop-from-named-entity only (no NL→Cypher)
**Status:** `facts_for_entities` returns all 1-hop neighbours of matched
entities; no multi-hop traversal, no NL→Cypher. Accepted for now (user: revisit
only if results disappoint). "Common thread across projects" synthesis is the
weak spot (1-hop from the Person doesn't surface Project→Technology overlaps).
**How (if needed):** add a couple of fixed, parameterised traversal patterns the
router can select (e.g. `overlap_between(a, b)` = shared Technology/Skill nodes
between two Projects) rather than full LLM-generated Cypher — deterministic and
safe.

### 11. Surface retrieved images through the agents + `/chat` (Tier 3, second half)
**Status:** the retrieval layer is done and verified live — both
`images_for_retrieved()` (section anchoring, the default) and `retrieve_images()`
(cross-modal similarity, router-gated). What's missing is the agent/API half:
`AvatarState` has no `images` field, the specialists can't reference a figure,
and `ChatResponse` has no image list. So the Tier-3 exit criterion ("*show me the
WGU Copilot architecture*" **displays** the diagram) is met at the retrieval layer
only.

**Design (settled 2026-07-09):** images are attached by **section anchoring**, not
by ranking them against the query. Retrieve text → take the winning
`parent_section_id`s → attach figures whose `linked_section_id` matches. The
figure inherits the prose's relevance, so there is nothing to threshold and a
diagram only appears beside the context it illustrates. The similarity pass
(`retrieve_images`) stays, but only for explicitly visual queries the router asks
for — it is not run every turn.

**How:**
- `retrieve` node: after building `retrieved_context`, call
  `images_for_retrieved(contexts)` → `state["images"]`. **Zero** extra API calls.
  If the router flags the query as visual ("show me / diagram / screenshot"), also
  run `retrieve_images(search_query, project_tag=...)` and merge, de-duping on
  `chunk_id`. Add a `wants_images: bool` to the router's structured output.
- `context.py`: render as `[img N] (citation_label) caption` so a specialist
  (mainly Deep-Dive) can write "see the architecture diagram [img 1]".
- `schemas.py`: return `images: [{image_uri, caption, citation_label}]` from
  `/chat`; the UI renders them inline. Guardrail: an `[img N]` marker with no
  matching image should fail the same way a bad `[n]` citation does.
- **Serve the bytes.** `image_uri` is currently an absolute local path
  (`temp_data/parsed/..._images/x.jpg`), useless to a browser and leaking a
  filesystem path. Add a `/images/{doc_id}/{filename}` static route (or push to
  object storage) and store a *relative* uri in the payload before deploy.

**Do not add an image score threshold.** RRF scores are rank-derived, so *both*
paths return something for an off-topic query — `retrieve()` always returns its
top-k sections and anchoring faithfully inherits them (verified: "what is his
salary expectation" still anchors two figures). Out-of-scope is handled where it
already is: the router sets `retrieval_plan=none`, so neither path runs.

### 13. Unfiltered retrieval can anchor an image from a *different* project
**Status:** observed, low priority. `images_for_retrieved` attaches figures from
every retrieved section. With no `project_tag` filter, a query like "how does
MedSumm fuse image and text embeddings" tops out on the ECIR paper but also
retrieves an RRF section from the Agentic RAG doc — and correctly anchors that
doc's `reciprocal-rank-fusion.svg` alongside. Not wrong (the section *was*
retrieved), but an answer about one project can display another's diagram.
**How:** restrict anchoring to the top-N contexts rather than all of them, or
require the image's `doc_id` to match the top context's. The router's
`project_tag` filter already prevents this whenever a project is identified.

### 14. Figure/Image index sections are retrieval noise
**Status:** both project docs end with an "Image Index" / "Figure index" section
listing every figure. These chunk into a section that ranks on any figure-ish
query (seen at rank 3 for "two-phase agent pipeline").
**How:** add `(image|figure)\s+index` to `_DROP_SECTION_RE` in `chunker.py`,
alongside References/Bibliography/Contents.

### 12b. Why the image vector is kept (don't "simplify" it away)
A text sidecar alone retrieves captioned figures just as well as the fused
image+sidecar vector (4/4 vs 4/4 on caption-vocabulary queries). The pixels earn
their keep only on content **depicted but never written**: "thumbnail photos of
lip swelling, mouth ulcer, cyanosis" → fused ranks Figure 1 **1st**, sidecar-only
ranks it **5th of 9** (3/4 vs 2/4 top-1 overall). Project write-ups are exactly
this case — a diagram's boxes name components the caption never mentions. Cost is
one embed call per image at ingest, zero at query time under section anchoring.

### 12. Embedding quota is separate from generation quota
**Status:** observed, not a bug — worth remembering. `gemini-embedding-2` kept
serving after `gemini-2.5-flash-lite` hit its daily free-tier `RESOURCE_EXHAUSTED`
wall, so the whole image ingest + retrieval verification ran with generation
quota exhausted.
**Implication:** a daily 429 looks identical to the per-minute throttle that
`_is_retryable` legitimately retries, so an exhausted quota burns 5 slow backoffs
(15→60s) per call. `images.link_images` now aborts caption-matching after 2
consecutive API failures; the same guard is worth adding to the graph extractor
and any other per-item LLM loop.
