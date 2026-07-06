# AI Avatar — Personal RAG + Agent System

A hybrid **GraphRAG** career assistant: a conversational system that answers
grounded, cited questions about Aniket Gaudgaul's career, projects, and
expertise. It combines a knowledge graph (Neo4j), a multimodal vector store
(Qdrant + Gemini Embedding 2), hybrid retrieval (dense + BM25 + RRF), and a
light LangGraph multi-agent layer (router + specialists + guardrail).

See [spec.md](spec.md) for the full specification and build roadmap.

## Tech stack

| Layer | Choice |
|---|---|
| Backend | FastAPI + Uvicorn |
| LLM / embeddings | Gemini (via `google-genai`) |
| Agent orchestration | LangGraph |
| Vector store | Qdrant |
| Graph DB | Neo4j |
| Sparse retrieval | `rank_bm25` |
| Evals | Ragas + DeepEval (optional extra) |

## Project layout

```
app/
├── main.py            # FastAPI app + lifespan
├── config.py          # pydantic-settings (reads .env)
├── api/               # routes (/health, /chat) + wire schemas
├── core/              # logging, shared utilities
├── ingestion/         # chunk, embed, extract entities  (spec 5)
├── retrieval/         # hybrid search, graph query, RRF  (spec 6)
└── agents/            # router, specialists, guardrail    (spec 7)
docker/                # Dockerfile + local compose (Qdrant + Neo4j)
evals/                 # Ragas / DeepEval / Promptfoo       (spec 10 Phase B)
docs/                  # source content
tests/                 # pytest smoke + eval seed
```

## Getting started

### 1. Environment

```powershell
# Create and activate the virtual environment (Windows / PowerShell)
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install the package (editable) with dev tooling
pip install -e ".[dev]"

# Copy env template and fill in secrets (Gemini key, DB passwords)
Copy-Item .env.example .env
```

### 2. Local services (optional until ingestion/retrieval land)

```powershell
docker compose -f docker/docker-compose.local.yml up -d
# Qdrant  -> http://localhost:6333
# Neo4j   -> http://localhost:7474  (bolt on 7687)
```

### 3. Run the API

```powershell
uvicorn app.main:app --reload
# Docs   -> http://localhost:8000/docs
# Health -> http://localhost:8000/health
```

### 4. Test & lint

```powershell
pytest -q
ruff check .
```

## Status

Tier 0 — scaffolding complete: FastAPI app with `/health` and a `/chat` stub,
config, structured logging, Docker, and CI. The retrieval + agent pipeline
(Tiers 1–3) is not wired up yet. See [spec.md](spec.md) §10 for the roadmap.
