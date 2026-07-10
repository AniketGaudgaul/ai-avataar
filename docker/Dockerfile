# Multi-stage build (spec 9: Docker multi-stage). Stage 1 builds a wheel-based
# virtualenv and warms the BM25 model cache; stage 2 is a slim runtime image.
#
# Build context is the repo root, not this directory:
#     docker build -f docker/Dockerfile .
#
# Only the runtime dependency set is installed — not the `[ingest]` extra. The
# index is built on the dev box (LlamaParse, SVG rasterisation, embedding); this
# container only *serves* an index that already exists.

# ---- builder ----
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FASTEMBED_CACHE_PATH=/opt/fastembed

WORKDIR /app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --upgrade pip && pip install .

# Pre-download the BM25 model (18 files from HuggingFace). qdrant-client builds
# the sparse half of each hybrid query client-side via fastembed, so without this
# the *first user request* blocks on a HuggingFace download — a slow cold start
# and a runtime dependency on a third-party host staying reachable.
RUN python -c "from fastembed import SparseTextEmbedding; SparseTextEmbedding(model_name='Qdrant/bm25')"

# ---- runtime ----
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    FASTEMBED_CACHE_PATH=/opt/fastembed \
    ASSETS_DIR=/app/assets

# Run as a non-root user.
RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/fastembed /opt/fastembed
COPY app ./app
# Figure bytes. Chunks store a uri relative to ASSETS_DIR, so this directory is
# what makes diagrams resolvable at serve time (app/core/assets.py). Without it
# the agent is shown no figures and /images/{chunk_id} returns 410 — silently, as
# far as the answer text is concerned.
COPY assets ./assets

USER appuser
EXPOSE 8000

# Railway and most PaaS inject $PORT and expect the process to bind it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,sys; p=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://localhost:{p}/health').status==200 else 1)"

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
