"""Project catalog — the router's project-filter vocabulary.

The router can only emit a *valid* `project_tag` filter if it knows which
projects exist and their canonical ids. We fetch the `Project` nodes from Neo4j
once (cached for the process) and render them as a compact list to inject into
the router prompt. Each `id` is exactly the chunk `project_tag` (spec 5.5), so a
project the router names maps straight to a retrieval filter.

Note: this is *only* for resolving a query to a project filter. The "list all
his projects" question still answers via graph retrieval (Person→LED→Project),
not from this catalog — so that path can be evaluated on its own.
"""

from __future__ import annotations

from functools import lru_cache

from app.core.logging import get_logger
from app.retrieval.graph import list_projects

logger = get_logger(__name__)


@lru_cache
def get_project_catalog() -> list[dict[str, str]]:
    """Cached `[{"id", "name"}]` of Project nodes. Never raises — a graph outage
    degrades to an empty catalog (router simply won't emit project filters)."""
    try:
        return list_projects()
    except Exception as exc:  # noqa: BLE001 — catalog is best-effort
        logger.warning("project catalog unavailable", extra={"error": str(exc)})
        return []


def project_catalog_prompt() -> str:
    """Render the catalog for the router system prompt. Empty string if unknown
    (the router prompt then simply omits the project-filter guidance)."""
    catalog = get_project_catalog()
    if not catalog:
        return ""
    lines = "\n".join(f'  - "{p["name"]}" -> project_tag "{p["id"]}"' for p in catalog)
    return (
        "Known projects (use the exact project_tag when a query targets one of "
        f"these; otherwise leave project_tag empty):\n{lines}"
    )


def valid_project_tags() -> set[str]:
    """The set of legal `project_tag` values, to reject router hallucinations."""
    return {p["id"] for p in get_project_catalog()}
