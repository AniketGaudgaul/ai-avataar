# Eval findings → issue register & fix plan

Source: manual review of `review-full-baseline-2026-07-15.md` (65 cases, annotated).
Status: **planning only — nothing fixed yet.** Each issue: root cause (verified
against code), evidence (case ids), severity, and proposed approach.

Legend: **P0** correctness/safety · **P1** retrieval quality · **P2** routing ·
**P3** framing/tone/data · **P4** eval-harness/dataset.

---

## Confirmed assumptions (from your notes)
- **AI-Avatar KB is NOT a project.** `source_type=how_i_built_this`, `project_tag=None`
  (60 chunks, doc_id `ai-avatar-kb`); not in the project catalog. Meta flow
  (`meta → vector → filter how_i_built_this`) is intact — no regression. (meta-03)
- **include_profile** on education/publications/years is `false` *by design*
  (narrow single facts). Not a bug. (fact-05, fact-06, fact-09)
- **fact-08** (location) and **adv-01** (Google) already fixed by the guardrail
  persona-coverage change.
- **deep-03 "QLoRA" miss** is a substring artifact — answer says "Q-LoRA".

---

## P0 — Correctness / safety   ✅ DONE (2026-07-16)
Implemented: grounding gate (I1) + guardrail false-refusal fixes (I2a, I2b).
I2c (greeting) deferred to I7 (needs the routing change). Verified on affected
items via targeted runs; **no full eval re-run yet** (do that after P1/P2 batch).

### I1. ✅ Hallucination on absent entities (retrieval always returns *something*)
**Fix shipped:** `_subject_unsupported()` in [retrieve.py](../app/agents/retrieve.py)
— a deep-dive whose named subject is neither a known catalog project nor mentioned
in *real* (non-meta) evidence short-circuits via `route_to_specialist → refuse` to
a confident `PROJECT_UNKNOWN_MESSAGE` ("No — that isn't one of the projects he's
worked on…"). Meta doc excluded from the mention check (it name-drops WGU as an
example query). Verified: WGU + a fabricated "NASA Mars Rover chatbot" decline;
all 5 real deep-dives (Agentic-RAG, Dreambrush, AskYarnit, Humanizer, MedSumm)
still answer.
**The most serious issue.** `retrieve()` returns top-k unconditionally with no
relevance gate — its own comment admits "an off-topic query still yields
sections" ([vector.py:141-145](../app/retrieval/vector.py#L141)). For a project
that isn't in the corpus, the top-k are generic chunks from *other* projects, and
the specialist writes a confident answer as if they were about the asked entity.
- **Evidence:** adv-02 (WGU Copilot — fully fabricated architecture from Agentic-RAG
  / AI-Avatar chunks; guardrail passed it because citations were present).
- **Root cause:** no check that retrieved evidence is actually *about* the asked
  entity/project; RRF score is available on each context but never thresholded;
  the `project_tag`-miss fallback ([retrieve.py:65](../app/agents/retrieve.py#L65))
  *removes* filtering, making off-topic retrieval more likely.
- **Proposed approach (discuss):**
  1. Grounding-relevance gate before generation: if the query names a specific
     project/entity and no retrieved chunk matches it (by `project_tag`, entity
     mention, or a score floor), route to a graceful "I don't have that on record"
     decline instead of generating. Ties into the guardrail's existing "nothing
     retrieved → must decline" logic, but for "nothing *relevant* retrieved."
  2. Consider an entity-match signal: does any context/graph-fact mention the
     asked project name? If not → decline.
- **Interaction:** overlaps I5 (project filtering). Fixing I5 reduces but does not
  eliminate this — WGU has no tag at all, so we still need the relevance gate.

### I2. Guardrail false-refusals on valid answers
- **I2a — ✅ banned-term substring fired on a *meta* answer describing the guardrails.**
  Check #3 now skips the `meta` route ([guardrail.py](../app/agents/guardrail.py))
  — a "what guardrails does it have?" answer that *names* compensation is a
  description, not a disclosure. Verified: meta-02 now answers + cites.
- **I2b — ✅ status/persona answer flagged via the recruiter-hedge check.** Check #4
  now exempts `persona_covered` answers — a status-brief availability answer that
  lands in the recruiter lane isn't a fit-judgement. Verified: status-01 now answers.
- **I2c — greeting flagged (DEFERRED → I7).** The real fix is not retrieving for
  greetings; handled with the routing batch. (edge-01)

---

## P1 — Retrieval quality (the dominant theme)   ✅ I5 + I3 DONE (2026-07-16)
Shipped I5 (project filtering) and I3 (multi-query). I4 (reranker) dropped per
decision. Verified on affected items; full eval re-run deferred to after P2.

### I3. ✅ Single fused multi-topic `search_query` hurts vector retrieval
**Fix shipped:** router now emits `sub_queries` (2-4, comparison/multi-part only);
`_multi_retrieve` in [retrieve.py](../app/agents/retrieve.py) retrieves each,
**auto-scopes each sub-query to its own project** (`resolve_single_project`), and
RRF-merges. Verified: compare-02 now answers the real overlap (Python, multi-agent,
OpenAI-family — cited to both projects' KB) instead of "no shared tech named"; a
plain deep-dive stays a single query.
The router emits ONE `search_query`, often a long sentence stuffing 4-5 sub-topics
("purpose AND workflow AND features AND latency AND throughput…"). One embedding of
a multi-topic sentence retrieves diffuse, mediocre matches.
- **Evidence:** compare-02 (tech overlap of A vs B — needs per-project queries),
  compare-04, expl-02, expl-04, deep-01. Your proposal: decompose into 2-4
  sub-queries, take top-k each, merge.
- **Root cause:** single-query design in [router.py](../app/agents/router.py) +
  single `vector_retrieve` call in [retrieve.py:54](../app/agents/retrieve.py#L54).
- **Proposed approach (discuss):** query decomposition for comparison/multi-aspect
  questions — router emits a `sub_queries: list[str]`; retrieve runs each, fuses
  (RRF across sub-results), dedupes, keeps top-N. Start narrow: only when the
  question is comparative or explicitly multi-part, to limit added latency/cost.

### I4. Correct section under-ranked; Abstract/Glossary/Fact-Sheet dominate
Architecture / tech-stack / retrieval-design questions retrieve the broad "Abstract",
"Project Fact Sheet", "Glossary" sections instead of the actual target section.
- **Evidence:** deep-01 (no architecture chunks retrieved), followup-01b (retrieval
  design → Abstract ranked #1), followup-02b (tech stack ranked #3 behind Abstract),
  visual-01, expl-03 (correct chunk at rank 5), deep-04 (correct chunk at rank 3).
- **Root cause:** retrieval = hybrid dense+BM25 fused by RRF, then sorted by fused
  score ([vector.py:120](../app/retrieval/vector.py#L120)). **There is no
  second-stage cross-encoder reranker.** Broad sections match many query terms and
  win on fusion; the specific section loses.
- **Proposed approach (discuss):** add a cross-encoder / LLM reranker over the
  top-N candidates before parent expansion — rerank by true query-passage relevance,
  not lexical overlap. Complements I3. Alternative/cheaper: section-type boosting
  (downweight Abstract/Glossary/Fact-Sheet for aspect-specific queries).

### I5. ✅ Cross-project pollution & coverage-gap projects
**Fix shipped (two parts):** (a) **data** — `python -m app.ingestion.vector.retag_narrative`
backfilled `project_tag` on the 8 project-specific narrative sections (Qdrant
`set_payload`, no re-embed; Yarnit *employer* section correctly skipped); (b)
**router** — `resolve_single_project` deterministically infers `project_tag` when
exactly one known project is named (the nano router was leaving it null), scoped to
deep_dive/factual lanes. Verified: Dreambrush/AskYarnit/Humanizer deep-dives now
retrieve only their own narrative chunk (no other-project pollution); comparisons
and recruiter/inventory stay unscoped. Matcher uses catalog **name-only distinctive
tokens** so "product"/"yarnit" don't mis-match.
Original analysis below.

#### I5 (original notes)
For a single-project question, chunks from *other* projects appear in the context.
Worse for the 5 "coverage-gap" projects that have **no dedicated chunks**.
- **Data:** only 3 projects have tagged chunks (agentic-rag 146, product-discovery
  141, medsumm 40). Ask-Yarnit, Concept-to-Catwalk, Dreambrush, Humanizer,
  LLM-Cost-Opt have **0** tagged chunks — they exist only as 1 narrative chunk +
  graph facts. So `project_tag=dreambrush` matches nothing → fallback un-filters →
  other projects' chunks dominate; the one correct narrative chunk ranks low.
- **Evidence:** deep-04 (Dreambrush correct chunk rank 3, rest from other projects),
  deep-05 (AskYarnit rank 1 but other-project chunks alongside), deep-06 (Humanizer
  same).
- **Proposed approach (discuss):** (a) when a project is named, prefer its narrative
  chunk and drop unrelated-project chunks even if the dedicated tag is empty — e.g.
  tag narrative sub-sections per project, or filter by entity mention; (b) rethink
  the `project_tag`-miss fallback so it narrows to narrative rather than going fully
  unfiltered. Feeds I1.

---

## P2 — Routing   ✅ DONE (2026-07-16)
Shipped I6 (status in-scope), I7 (greeting/gibberish direct replies — includes the
deferred I2c), I8 (meta cues + clarify-don't-assume). All via one terminal-reply
mechanism: router sets `decline_reason` (greeting/clarify) or `clarification`, and
the reply node picks the message. Verified on affected items.

- **I6 ✅** router prompt now classifies availability/contact/location/roles as
  `factual` (answered from the status brief), never out_of_scope/recruiter.
  status-01/02 now answer.
- **I7 ✅** out_of_scope carries `oos_kind` (refuse/greeting/clarify); "hi" →
  GREETING_MESSAGE (no retrieval, no guardrail), gibberish → CLARIFY_MESSAGE.
- **I8 ✅** sharpened meta-vs-deep_dive cues (meta-04 → meta); router may set a
  `clarification` for ambiguous project/subject questions → visual-03 now asks
  "this avatar's pipeline or one of his projects?" instead of assuming.

#### I6 (original notes)
- **status-01** "available for new roles?" → `recruiter` (then guardrail-flagged).
- **status-02** "how can I get in touch?" → `out_of_scope` (hard refusal).
- **Root cause:** router prompt has no explicit rule that availability / contact /
  location / how-to-reach are in-scope factual questions answered from the status
  brief — so they drift to recruiter or out_of_scope.
- **Proposed approach:** add a router rule: availability/contact/location/"is he
  looking" → `route=factual`, answered from the persona brief (no citation needed).
  Keep status-03 ("what roles") flexible (recruiter OR factual both fine).

### I7. Greetings / gibberish / trivial → over-retrieve + wrong refusal
- **edge-01** "hi" → `meta`+`vector`, retrieved chunks unnecessarily, greeting
  answer then guardrail-refused. **edge-02** "asdfghjkl" → correct out_of_scope but
  the generic refusal template says "I'll leave compensation/personal to him" — for
  gibberish that's a non-sequitur.
- **Root cause:** no lightweight "answer directly, no retrieval" path; the single
  refusal template is used for every out_of_scope case.
- **Proposed approach (discuss):** a `route` (or router-provided short answer) for
  greetings/smalltalk that replies briefly with no retrieval and no guardrail
  citation requirement; make the refusal wording context-appropriate (greeting vs
  gibberish vs genuinely-out-of-scope). Overlaps I2c and I11.

### I8. meta ↔ deep_dive confusion + no clarification on ambiguous queries
- **meta-04** "hardest engineering problem you solved building this" → `deep_dive`
  (should be `meta`; missed the modality-gap answer). **visual-03** "what does the
  ingestion pipeline look like?" → `deep_dive`/`hybrid` (should be `meta`), and — your
  key point — it **assumed** the AI-Avatar pipeline instead of asking which project.
- **Proposed approach:** sharpen meta-vs-deep_dive cues in the router prompt; add a
  clarify path for genuinely ambiguous, project-unspecified questions rather than
  assuming.

---

## P3 — Framing / tone / data accuracy

### I9. False-premise corrections are wishy-washy
"That isn't something I can point to" / "in the material I have" reads evasive. A
confident twin should say plainly: "No — he doesn't have a PhD," without narrating
whether it's "in the data."
- **Evidence:** adv-03 (PhD/Stanford), adv-01 (Google).
- **Proposed approach:** specialist prompt guidance for correcting false premises —
  correct directly and confidently; don't reference "sources"/"the data" as the
  reason.

### I10. Role/ownership overclaim
- **fact-03** "he led the MedSumm project" — he was a research intern/contributor,
  not necessarily lead. **fact-07** frames Concept-to-Catwalk as his solo win; it was
  a Yarnit team effort.
- **Root cause:** graph `LED` edges (and prose) overstate ownership for some items.
- **Proposed approach:** audit `LED` vs "contributed to / part of" edges in the graph
  for MedSumm and Concept-to-Catwalk; and/or prompt guidance to hedge ownership when
  the graph doesn't clearly assert sole leadership. **Data change — needs your input
  on the true role for each.**

### I11. One-size-fits-all refusal template
The refusal always mentions "compensation or personal matters," even when the user
asked neither (gibberish, greeting). (edge-01, edge-02) **Proposed approach:** make
the refusal wording match the trigger, or keep a neutral short version. Overlaps I7.

### I12. Image over-inclusion
A profile-card image was attached to a broad "tell me everything" overview where it
added nothing. (edge-03) **Proposed approach:** tighten figure-selection so images
ride only genuinely visual/architectural answers (prompt already says this — verify
why it fired here).

---

## P4 — Eval-harness / dataset reconciliation (not system bugs)
### I13. Stale `expected_plan` (graph → hybrid)
Router now sends person-level & false-premise factual questions to `hybrid` by
design. Reconcile ground truth: fact-05, fact-06, fact-07, fact-09, adv-01, adv-03.
Also accept adv-07 (refusing a fabrication bait is correct even though expected
`factual`), and update status-01/02 expectations once I6 lands.
### I14. `must_include` substring artifacts
Normalize obvious variants (QLoRA/Q-LoRA) or lean on the LLM-judge for recall so
correct answers aren't dinged. (deep-03; and the 60%/40%, SKU/30s misses are the
real *coverage-gap* signal from I5, not wording.)

---

## Suggested sequencing (for discussion)
1. **P0 quick wins first** — I2 guardrail false-refusals (meta-02, status-01,
   greeting) are small, high-value prompt/logic edits with immediate UX impact.
2. **I1 relevance gate** — the WGU hallucination is the highest-stakes correctness
   bug; do right after I2 since it also touches the guardrail/decline path.
3. **P1 retrieval** — the biggest lever on output quality, but the largest change.
   Sequence: I5 (filtering, cheap) → I4 (reranker) → I3 (query decomposition).
   Re-run the eval after each to measure the delta.
4. **P2 routing** (I6, I7, I8) — mostly router-prompt edits; batch them.
5. **P3 framing/data** (I9-I12) — prompt tweaks + a small graph audit (I10).
6. **P4** — reconcile the dataset so future runs measure cleanly.

Decisions (from user, 2026-07-16):
- **I3/I4:** multi-query **yes**, reranker **no** (for now). I3 stays P1; I4 dropped.
- **I10:** MedSumm — he was a **contributor/author, NOT the lead**. Concept-to-Catwalk
  — a **Yarnit team** effort, not solo. Fix graph/prose to reflect this.
- **I9 / I1 decline style:** state it **plainly and confidently** — "No, he didn't
  work on that", "No, he doesn't have a PhD from Stanford", "No, he isn't at Google."
  **Never** say "the data/sources don't show…". So the I1 unknown-project decline is
  a confident "that isn't one of his projects", not a hedge.
