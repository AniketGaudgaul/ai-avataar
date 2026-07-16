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

import re
from collections import defaultdict
from functools import lru_cache

from app.core.logging import get_logger
from app.retrieval.graph import list_projects

logger = get_logger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Filler in project names/ids that carries no identifying signal.
_NAME_STOPWORDS = frozenset({"the", "and", "for", "of", "an", "ai"})


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


@lru_cache
def _distinctive_token_index() -> dict[str, str]:
    """Map each token that is UNIQUE to one project → that project's tag.

    Built from every project's name + id. A token shared by two projects (e.g.
    "product", "ai", "assistant") is dropped, so only identifying tokens remain
    ("dreambrush", "discovery", "catwalk", "medsumm"). This is what lets us match a
    named project without generic words ("Product App: …") causing false hits."""
    # Index the display NAME only, not the id: the id "ask-yarnit" would split into
    # "ask"/"yarnit", and "yarnit" also names the *employer* Yarnit — so an id-based
    # token would mis-tag the employer section as the AskYarnit product. The name
    # "AskYarnit" tokenizes to a single unambiguous "askyarnit".
    token_projects: dict[str, set[str]] = defaultdict(set)
    for p in get_project_catalog():
        tokens = {
            t for t in _TOKEN_RE.findall(p["name"].lower())
            if len(t) >= 3 and t not in _NAME_STOPWORDS
        }
        for t in tokens:
            token_projects[t].add(p["id"])
    return {t: next(iter(ids)) for t, ids in token_projects.items() if len(ids) == 1}


def match_projects(text: str) -> set[str]:
    """Project tags whose distinctive tokens appear in `text` (name, heading, …)."""
    idx = _distinctive_token_index()
    return {idx[t] for t in _TOKEN_RE.findall(text.lower()) if t in idx}


def resolve_single_project(texts: list[str]) -> str:
    """The single project tag named across `texts` (router entities), else "".

    Returns a tag only when EXACTLY ONE project is named — so a comparison across
    two projects ("Agentic RAG vs Product Discovery") stays unscoped, while a
    single-project deep-dive ("Dreambrush architecture") gets its filter even when
    the router itself left `project_tag` empty."""
    matched: set[str] = set()
    for t in texts:
        matched |= match_projects(t)
    return next(iter(matched)) if len(matched) == 1 else ""
