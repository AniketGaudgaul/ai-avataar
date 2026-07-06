"""Liveness / readiness endpoint (spec 4: FastAPI `/health`)."""

from fastapi import APIRouter

from app import __version__
from app.api.schemas import HealthResponse
from app.config import settings

router = APIRouter(tags=["ops"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(version=__version__, env=settings.app_env)
