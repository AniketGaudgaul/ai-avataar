"""FastAPI application entrypoint.

Run locally with:
    uvicorn app.main:app --reload
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.routes import chat, health
from app.config import settings
from app.core.logging import get_logger, setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger = get_logger(__name__)
    logger.info("startup", extra={"env": settings.app_env, "version": __version__})
    # TODO: initialize store clients (Qdrant, Neo4j) and the compiled LangGraph
    # here, attach to app.state, and close them on shutdown.
    yield
    logger.info("shutdown")


app = FastAPI(
    title="AI Avatar",
    description="Personal RAG + Agent System — grounded, cited career Q&A.",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(chat.router)


@app.get("/", tags=["ops"])
async def root() -> dict[str, str]:
    return {"service": "ai-avatar", "version": __version__, "docs": "/docs"}
