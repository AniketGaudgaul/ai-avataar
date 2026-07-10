# Agentic System — Walkthrough

This explains the Tier-2 agent layer (`app/agents/`) end-to-end: what happens
from the moment a user asks a question to the moment an answer comes back.
Read this alongside the code — file paths are given for every step so you can
open each one as you go.

The whole thing is a **LangGraph state machine**: a graph of "nodes" (plain
Python functions) that pass a shared dictionary (`state`) between them. Each
node reads some keys out of `state`, does its job, and returns the keys it
wants to add/update. LangGraph merges those updates in and moves to the next
node according to the edges you defined.

---

## 0. The shared state — `app/agents/state.py`

Before anything else, look at `AvatarState`. It's just a `TypedDict` — a
dictionary with known keys. Think of it as a clipboard that gets passed from
node to node, each one writing on it:

| Key | Set by | Meaning |
|---|---|---|
| `query`, `messages` | caller (API) | the question + prior chat turns |
| `route`, `retrieval_plan`, `router_entities` | router | which specialist + how to retrieve |
| `retrieved_context`, `graph_facts`, `citations`, `context_block` | retrieve | the evidence to answer from |
| `draft_answer` | a specialist | the generated answer |
| `guardrail_verdict`, `retry_count` | guardrail | pass/fail + how many regen attempts |

Nothing here is magic — it's the one object every file below reads from and
writes to.

---

## 1. Entry point — `app/api/routes/chat.py`

The FastAPI `/chat` endpoint is the outside-world entry point. It takes the
HTTP request, pulls out `query` + `history`, and calls:

```python
result = await run_agent(request.query, history=history)
```

That's it — the endpoint doesn't know anything about routing/retrieval/agents.
All of that is `run_agent`'s job.

## 2. The runner — `app/agents/runner.py`

`run_agent()` is the thing that actually drives the state machine:

1. Builds the **initial state**: `{"query": ..., "messages": history, "retry_count": 0}`.
2. Gets the compiled graph via `get_agent_graph()` (built once, cached).
3. `await graph.ainvoke(initial)` — runs the whole flow start to finish and
   returns the **final state** after it reaches `END`.
4. Picks `draft_answer` out as `answer`, filters `citations` down to only the
   ones actually referenced (via `[n]` markers) in the final text, and returns
   `{"answer", "route", "citations"}` — the shape the API responds with.

So this file is the "conductor" — read it first to see the shape of one full
run before diving into what each node does.

## 3. The graph wiring — `app/agents/graph.py`

This is the map of the state machine — **read this to see the whole flow at a
glance** before looking at individual node code:

```
START → router ─┬─(out_of_scope)→ refuse → END
                └─(else)→ retrieve → {career_qa | deep_dive | recruiter} → guardrail

guardrail ─┬─(pass)→ END
           ├─(fail, retry_count < 1)→ back to the SAME specialist (regenerate)
           └─(fail again)→ refuse → END
```

`build_graph()` just registers each node function under a name and draws these
edges. Two of the edges are **conditional** — they call a small routing
function to decide where to go next, based on the current state:

- `route_after_router` (in `router.py`) — out_of_scope → `refuse`, else → `retrieve`.
- `route_to_specialist` (in `retrieve.py`) — factual/meta → `career_qa`, deep_dive → `deep_dive`, recruiter → `recruiter`.
- `route_after_guard` (in `guardrail.py`) — pass → `END`, fail+first try → same specialist, fail again → `refuse`.

Everything below is what happens *inside* each box in that diagram.

---

## 4. Router node — `app/agents/router.py`

**Job:** this is not just a classifier — it's a **query planner**. In one Gemini
call, given the query *and the recent conversation*, it decides which specialist
answers, how to retrieve, **and rewrites the search query**.

```python
decision = generate_structured(
    prompt=f"Recent conversation:\n{history}\n\nClassify and plan ...:\n{query}",
    schema=RouterDecision,   # {route, retrieval_plan, search_query,
                             #  entities, project_tag, answer_depth, rationale}
    model=settings.agent_router_model,   # gemini-2.5-flash-lite
    system_instruction=ROUTER_SYSTEM + project_catalog_prompt(),
)
```

`RouterDecision` is a Pydantic model, so Gemini is forced to return valid JSON
matching that shape — no manual parsing. The fields:

- `route` ∈ `factual | deep_dive | recruiter | meta | out_of_scope` — the lane.
- `retrieval_plan` ∈ `vector | graph | hybrid | none` — where to look.
- **`search_query`** — the user's question **rewritten** into a clean,
  self-contained retrieval query. This is the important upgrade: retrieval does
  NOT use the raw user text. The router resolves pronouns/follow-ups from the
  conversation ("go deeper on the architecture" → "WGU Copilot architecture
  design and components"), drops filler, and expands with useful keywords.
- `entities` — proper nouns to look up in the graph; "he/his" resolved to the
  person's name.
- **`project_tag`** — if the query targets one specific known project, the exact
  project id to filter retrieval by. The router knows the valid project ids
  because `project_catalog_prompt()` (from `app/agents/catalog.py`) injects the
  list of projects — fetched once from Neo4j at startup. A tag not in that list
  is discarded (`valid_project_tags()`), so it can't hallucinate a filter.
- **`answer_depth`** ∈ `overview | detail` — for the abstract-first behavior
  (see the deep-dive note in §6). "Tell me about project X" → `overview`;
  "explain X's architecture" or "go deeper" → `detail`.

Because the router now sees history, **follow-ups work**: turn 1 "tell me about
MedSumm" gets an overview; turn 2 "yes, the fusion part" is rewritten (using the
history) into a detailed, MedSumm-fusion-specific `search_query`.

After the call, small safety nets: out-of-scope is forced to `plan="none"`; an
empty `search_query` falls back to the raw question; an unknown `project_tag` is
dropped. The prompt itself lives in `app/agents/prompts.py` under
`ROUTER_SYSTEM`.

**Where it goes next:** `route_after_router` sends `out_of_scope` → `refuse`,
everything else → `retrieve`.

---

## 5. Retrieve node — `app/agents/retrieve.py`

**Job:** actually fetch the evidence, once, shared by all specialists (this is
why it's its own node instead of being repeated inside each specialist).

It looks at `retrieval_plan` and does zero, one, or two of these:

```python
if plan in ("vector", "hybrid"):
    contexts = vector_retrieve(search_query, ..., source_type=..., project_tag=...)
if plan in ("graph", "hybrid"):
    graph_facts = facts_for_entities(names)                    # app/retrieval/graph.py
```

- `vector_retrieve` — your existing hybrid dense+BM25+RRF search over Qdrant,
  with small-to-big parent expansion (see `app/retrieval/vector.py`). Note it
  runs on the router's **rewritten `search_query`**, not the raw user text. Two
  filters may be applied: `source_type` (meta lane → only `how_i_built_this`),
  and **`project_tag`** (scopes to one project's chunks when the router
  resolved a project). *Caveat:* the project filter is inert until project docs
  are ingested with `project_tag`s — today only the ECIR paper is loaded, so a
  project-scoped search currently returns nothing (BACKLOG #9).
- `facts_for_entities` — looks up each named entity in Neo4j and returns
  citable relationship facts (already built — `app/retrieval/graph.py`). If the
  router found no explicit entity names but the plan needs graph facts, it
  defaults to looking up the person themself.

Then both results get merged into one prompt-ready block:

```python
context_block, citations = assemble_context(contexts, graph_facts)
```

That call goes to `app/agents/context.py` — `assemble_context()` numbers each
vector context `[1]`, `[2]`, ... and appends all graph facts together as one
final `[n]` block, e.g.:

```
[1] (WGU Copilot — Architecture)
...chunk text...

[2] (Knowledge graph facts)
- Aniket Gaudgaul —WORKED_AT→ Yarnit Innovations (start: 2024-08, end: 2026-05)
```

This is capped (`agent_max_contexts`, `agent_max_context_chars` in
`app/config.py`) because parent sections can be big (~8k chars) — otherwise the
prompt would blow up.

**Where it goes next:** `route_to_specialist` sends `factual`/`meta` →
`career_qa`, `deep_dive` → `deep_dive`, `recruiter` → `recruiter`.

---

## 6. The specialists

All three specialists are thin — they just pick a system prompt + model and
call one shared function. Look at the shared function first.

### Shared runner — `app/agents/generate.py`

`run_specialist(state, system_prompt, model, temperature)`:

1. Grabs `context_block` from state (built in step 5) and the last few
   `messages` (for follow-up questions).
2. Checks if there's a *failed* `guardrail_verdict` already in state (meaning
   this is a regeneration attempt) — if so, appends the guardrail's failure
   reasons to the prompt as feedback, and bumps `retry_count`.
3. Builds the final prompt:
   ```
   [recent conversation, if any]
   CONTEXT (numbered sources — cite by their [n] markers):
   <context_block>

   QUESTION: <query>
   [feedback from a rejected previous draft, if regenerating]
   ```
4. Calls `generate_text(prompt, model, system_instruction=system_prompt)` —
   this is a plain-text Gemini call (added to `app/core/gemini.py` for this
   feature; the existing `generate_structured` was JSON-only).
5. Returns `{"draft_answer": answer, "retry_count": retry_count}`.

### The three thin wrappers

- `app/agents/career_qa.py` — uses `CAREER_QA_SYSTEM` prompt. If `route ==
  "meta"`, appends `META_NOTE` (tells it this is a "how was this chatbot built"
  question). Handles both the factual/explanatory lane *and* meta lane, per the
  spec's decision to fold meta into Career Q&A.
- `app/agents/deep_dive.py` — uses `DEEP_DIVE_SYSTEM`, slightly higher
  temperature (more elaborative), meant for architecture walkthroughs.
- `app/agents/recruiter_mode.py` — uses `RECRUITER_SYSTEM`, which *requires*
  the answer to open with a projection disclaimer (checked later by the
  guardrail).

**The abstract-first / "explain a project" flow.** Both `career_qa` and
`deep_dive` check `answer_depth`: when it's `"overview"` (a general "tell me
about project X"), they append `OVERVIEW_NOTE`, which tells the model to give a
short gist and then *offer* to go deeper on a named aspect ("architecture", "key
decisions", "results"...). When the user replies "yes, the architecture", that's
a new turn — the router (which sees the history) rewrites it to a detailed,
aspect-specific `search_query` with `answer_depth="detail"`, and this same
`deep_dive` node now gives the full walkthrough. So the two-step "gist → offer →
deep dive" behavior needs **no extra state-machine nodes** — it emerges from
history-aware query rewriting + the depth flag + this prompt switch.

All three prompts live in `app/agents/prompts.py`. They all share
`_CITATION_RULES` (also in that file) — the paragraph that tells the model to
answer only from the numbered context, cite every claim with `[n]`, and refuse
out-of-scope topics (salary, personal life, etc). This is the main thing
enforcing "grounded, cited answers" from the model side; the guardrail (next
section) enforces it mechanically afterward.

**Where it goes next:** every specialist flows straight into `guardrail`
(see `graph.py`'s `for specialist in (...): g.add_edge(specialist, "guardrail")`).

---

## 7. Guardrail node — `app/agents/guardrail.py`

**Job:** validate the draft answer *without* an LLM call — pure rule-based
checks (this was an explicit decision to keep it simple/fast/free for now).

`guardrail_node` runs four checks against `draft_answer`:

1. **Grounded but uncited** — if we retrieved context/graph facts but the
   answer has no `[n]` marker anywhere, and it's not a graceful "I don't know"
   → fail.
2. **Unsupported assertion** — if we retrieved *nothing* but the answer isn't a
   graceful decline (i.e. it's asserting facts with no evidence) → fail.
3. **Banned-topic leakage** — scans for terms like "salary", "compensation",
   "his wife", etc. → fail if found.
4. **Recruiter projection label** — if `route == "recruiter"` and the word
   "projection" doesn't appear anywhere → fail.

It returns `{"guardrail_verdict": {"pass": bool, "reasons": [...]}}`.

Then `route_after_guard` decides where to go:

```python
if verdict["pass"]:
    return "end"
if state["retry_count"] < 1:
    return <same specialist>     # regenerate, now with feedback in the prompt
return "refuse"                  # already retried once, give up safely
```

This is the "loop back once, then fail safe" behavior from the spec — you can
see it directly in this one function.

---

## 8. Refuse node — `app/agents/refuse.py`

The simplest node — no LLM call at all, just returns a fixed templated
message (`REFUSAL_MESSAGE` in `prompts.py`). Reached either directly from the
router (out-of-scope query) or from the guardrail (answer failed validation
twice). Deliberately deterministic so this final safety net can never itself
hallucinate.

---

## 9. Back to the runner

Once the graph hits `END`, `graph.ainvoke(...)` returns, and we're back in
`run_agent()` (step 2). It takes whatever `draft_answer` and `citations` ended
up in the final state, filters citations to the ones actually cited, and hands
`{"answer", "route", "citations"}` back to the FastAPI endpoint, which wraps it
in a `ChatResponse` and sends it to the client.

---

## Full call stack for one request (cheat sheet)

```
POST /chat                                app/api/routes/chat.py
 └─ run_agent(query, history)              app/agents/runner.py
     └─ graph.ainvoke(initial_state)       app/agents/graph.py (defines the flow)
         ├─ router_node                    app/agents/router.py
         │    └─ generate_structured(...)  app/core/gemini.py
         ├─ [conditional] → retrieve_node  app/agents/retrieve.py
         │    ├─ vector_retrieve(...)      app/retrieval/vector.py
         │    ├─ facts_for_entities(...)   app/retrieval/graph.py
         │    └─ assemble_context(...)     app/agents/context.py
         ├─ [conditional] → specialist     career_qa.py / deep_dive.py / recruiter_mode.py
         │    └─ run_specialist(...)       app/agents/generate.py
         │         └─ generate_text(...)   app/core/gemini.py
         ├─ guardrail_node                 app/agents/guardrail.py
         ├─ [conditional] → regenerate OR refuse OR end
         └─ refuse_node (if needed)        app/agents/refuse.py
     └─ used_citations(...)                app/agents/context.py
 └─ ChatResponse(...)                      app/api/schemas.py
```

## Handy way to see it live

Run one query through the graph directly and print the internal trace
(route, plan, entities, retries, guardrail verdict) without going through the
API:

```bash
python -m app.agents.cli --trace "walk me through the WGU Copilot architecture"
```

That's `app/agents/cli.py` — it calls the exact same `get_agent_graph()` used
in production, just prints extra internals along the way.
