"""Chat endpoint (spec 4: FastAPI `/chat`).

Drives the LangGraph state machine (router → retrieve → specialist → guardrail)
for one turn and returns the grounded answer with its cited sources, plus any
figures the specialist chose to show — each carrying the `[imgN]` marker that
locates it in `answer`.
"""

from fastapi import APIRouter, HTTPException

from app.agents.context import clean_label, short_caption
from app.agents.runner import run_agent
from app.api.routes.images import image_url
from app.api.schemas import AnswerImage, ChatRequest, ChatResponse, Citation
from app.core.logging import get_logger

router = APIRouter(tags=["chat"])
logger = get_logger(__name__)


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    logger.info("chat_request", extra={"query": request.query})

    history = [m.model_dump() for m in request.history]
    try:
        result = await run_agent(
            request.query, history=history, session_id=request.session_id
        )
    except Exception as exc:  # surface as 502 rather than leaking a stack trace
        logger.exception("agent_failed")
        raise HTTPException(status_code=502, detail="The assistant failed to answer.") from exc

    return ChatResponse(
        answer=result["answer"],
        route=result["route"],
        citations=[Citation(**c) for c in result["citations"]],
        images=[
            AnswerImage(
                marker=f.marker,
                chunk_id=f.image.chunk_id,
                url=image_url(f.image.chunk_id),
                caption=short_caption(f.image.caption, f.image.heading_path),
                label=clean_label(f.image.citation_label),
                source_type=f.image.source_type,
            )
            for f in result["images"]
        ],
    )
