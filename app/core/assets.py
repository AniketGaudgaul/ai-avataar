"""Where figure bytes live, and how a stored `image_uri` resolves to a file.

An image chunk's `image_uri` is stored in Qdrant as a **root-relative POSIX
path** (`<doc_id>/<filename>`), never as the absolute path of the machine that
ingested it. That machine is a Windows dev box; the machine that serves the
answer is a Linux container. An absolute `D:\\...` path in the index means every
figure silently vanishes at serve time — `Path.is_file()` is False, the specialist
is shown nothing, and the answer degrades to text with no error anywhere.

So the index stores *identity* (which figure) and this module owns *location*
(where its bytes are on this host), keyed off `settings.assets_dir`. Moving the
corpus between hosts is then a matter of shipping `assets/` and pointing the
setting at it — no re-embedding, no payload rewrite.

Legacy absolute URIs written before this split still resolve, so an index does not
have to be migrated to keep working locally.
"""

from __future__ import annotations

import shutil
from functools import lru_cache
from pathlib import Path

from app.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def mime_for(path: str | Path) -> str:
    """Best-effort MIME type from a file extension (defaults to PNG)."""
    return _MIME_BY_SUFFIX.get(Path(path).suffix.lower(), "image/png")


@lru_cache
def assets_root() -> Path:
    """The directory holding figure bytes on this host."""
    return Path(settings.assets_dir).resolve()


def relative_uri(doc_id: str, filename: str) -> str:
    """The portable `image_uri` for a figure: `<doc_id>/<filename>`, POSIX-style."""
    return f"{doc_id}/{Path(filename).name}"


def resolve(uri: str) -> Path | None:
    """Return the on-disk path for a stored `image_uri`, or None if it is absent.

    Relative URIs join `assets_root()`. An absolute URI is honoured only if it
    exists — that keeps a pre-migration local index working while guaranteeing a
    stale `D:\\...` path resolves to None on a container rather than to some
    unrelated file.

    Guards against traversal: a URI cannot escape the assets root, so a payload is
    never a lever to read arbitrary files even if the index were tampered with.
    """
    if not uri:
        return None

    candidate = Path(uri)
    if candidate.is_absolute():
        return candidate if candidate.is_file() else None

    root = assets_root()
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        logger.warning("image_uri escapes assets root", extra={"uri": uri})
        return None
    return resolved if resolved.is_file() else None


def store_image(doc_id: str, source: str | Path) -> str:
    """Copy a figure into the assets tree and return its portable `image_uri`.

    Called at ingest so the bytes a chunk refers to are always inside `assets/`,
    which is what gets shipped to the serving host.
    """
    src = Path(source)
    uri = relative_uri(doc_id, src.name)
    dest = assets_root() / uri
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists() or dest.stat().st_size != src.stat().st_size:
        shutil.copy2(src, dest)
    return uri
