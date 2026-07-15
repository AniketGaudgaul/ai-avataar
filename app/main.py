"""FastAPI application entrypoint.

Run locally with:
    uvicorn app.main:app --reload
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import chat, health, images
from app.config import settings
from app.core.logging import get_logger, setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger = get_logger(__name__)
    logger.info("startup", extra={"env": settings.app_env, "version": __version__})
    # Warm the compiled LangGraph and the project catalog (the router's
    # project-filter vocabulary, read once from Neo4j) so the first request is
    # fast and a graph/DB misconfig surfaces at startup, not mid-conversation.
    from app.agents.catalog import get_project_catalog
    from app.agents.graph import get_agent_graph
    from app.core.tracing import get_langfuse, tracing_enabled
    from app.core.tracing import shutdown as shutdown_tracing

    # Initialise the Langfuse client at startup so the @observe decorators resolve
    # the configured project (no-op when keys are absent).
    get_langfuse()
    get_agent_graph()
    catalog = get_project_catalog()
    logger.info(
        "agent ready",
        extra={"projects_in_catalog": len(catalog), "tracing": tracing_enabled()},
    )
    yield
    shutdown_tracing()  # flush queued Langfuse events before exit
    logger.info("shutdown")


app = FastAPI(
    title="AI Avatar",
    description="Personal RAG + Agent System — grounded, cited career Q&A.",
    version=__version__,
    lifespan=lifespan,
)

# The chat widget is a public, unauthenticated read-only API served from many
# contexts (GitHub Pages, in-app browsers, file:// with Origin "null"). It sends
# no cookies, so we don't need credentialed CORS. Setting CORS_ORIGINS="*" lets
# any origin call it; the CORS spec forbids wildcard + credentials, so we drop
# credentials whenever a wildcard is configured.
_cors_origins = settings.cors_origins_list
_cors_allow_all = "*" in _cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=not _cors_allow_all,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(chat.router)
app.include_router(images.router)


@app.get("/", tags=["ops"])
async def root() -> dict[str, str]:
    return {"service": "ai-avatar", "version": __version__, "docs": "/docs"}
