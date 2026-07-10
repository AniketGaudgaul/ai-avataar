"""Figure bytes endpoint (spec 6.3).

`/chat` returns each chosen figure as a `chunk_id` + a URL rather than base64 in
the JSON: a diagram runs to ~1 MB, answers are cached and logged, and a client
that renders text only should not pay to transfer pixels it will discard.

The path served is never supplied by the caller. `chunk_id` is resolved through
Qdrant, `image_uri` is read back from the payload our own ingest wrote, and
`assets.resolve` confines it to the assets root — so this cannot be walked into
an arbitrary file read.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import settings
from app.core.assets import mime_for, resolve
from app.core.logging import get_logger
from app.ingestion.vector.store import get_client, image_record

router = APIRouter(tags=["images"])
logger = get_logger(__name__)


def image_url(chunk_id: str) -> str:
    """The URL `/chat` advertises for a figure's bytes.

    Absolute when `public_base_url` is configured: the widget is served from
    GitHub Pages, where a relative path would resolve against the portfolio site
    rather than this API."""
    path = f"/images/{chunk_id}"
    base = settings.public_base_url.rstrip("/")
    return f"{base}{path}" if base else path


@router.get("/images/{chunk_id}")
async def get_image(chunk_id: str) -> FileResponse:
    record = image_record(get_client(), chunk_id)
    if record is None:
        raise HTTPException(status_code=404, detail="No such figure.")

    uri = (record.payload or {})["image_uri"]
    path = resolve(uri)
    if path is None:
        # Indexed but the bytes are absent — a bad deploy (assets not shipped) or a
        # stale absolute uri, not a bad request. Loud, because the alternative is a
        # portfolio that quietly shows no diagrams.
        logger.error("figure indexed but missing", extra={"chunk_id": chunk_id, "uri": uri})
        raise HTTPException(status_code=410, detail="Figure is no longer available.")

    return FileResponse(path, media_type=mime_for(path))
