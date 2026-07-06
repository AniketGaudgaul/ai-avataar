"""Chat endpoint (spec 4: FastAPI `/chat`).

Currently a stub that returns a placeholder response. It will be wired to the
LangGraph state machine (router → retrieve → specialist → guardrail) in Tier 1/2.
The wire contract is stable so the frontend widget can be built against it now.
"""

from fastapi import APIRouter

from app.api.schemas import ChatRequest, ChatResponse
from app.core.logging import get_logger

router = APIRouter(tags=["chat"])
logger = get_logger(__name__)


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    logger.info("chat_request", extra={"query": request.query})

    # TODO(Tier 1): invoke the LangGraph pipeline here.
    return ChatResponse(
        answer=(
            "The AI Avatar backend is running, but the retrieval + agent pipeline "
            "is not wired up yet. This is a placeholder response."
        ),
        route=None,
        citations=[],
    )
