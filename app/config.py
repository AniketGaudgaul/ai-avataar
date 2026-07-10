"""Application configuration, loaded from environment / `.env`.

Uses pydantic-settings so every setting is typed, validated, and documented in
one place. Import the singleton `settings` (or call `get_settings()`).
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---
    app_env: str = "local"
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:3000,http://localhost:5173"
    # Figure bytes live here (see app/core/assets.py). Chunks store a *relative*
    # image_uri, so this is the only thing that changes between the dev box and
    # the container (`/app/assets`).
    assets_dir: str = "assets"
    # The origin this API is reachable at, e.g. "https://ai-avatar.up.railway.app".
    # Figure URLs must be absolute: the chat widget is served from GitHub Pages, so
    # a relative "/images/..." would resolve against the *portfolio*, not the API.
    # Empty = emit relative URLs (fine for local dev on one origin).
    public_base_url: str = ""

    # --- Gemini ---
    google_api_key: str = ""
    gemini_router_model: str = "gemini-2.5-flash-lite"
    gemini_flash_model: str = "gemini-2.5-flash"
    gemini_pro_model: str = "gemini-2.5-pro"
    gemini_embedding_model: str = "gemini-embedding-2"
    embedding_dim: int = 1536

    # --- Agents (spec 7) ---
    # Free-tier note (see project memory): every agent call uses flash-lite for
    # now. The spec's per-agent model choices (Flash for Q&A, Pro for Deep-Dive)
    # are deferred until billing is enabled — these are parameters so they can be
    # bumped without code changes.
    agent_router_model: str = "gemini-2.5-flash-lite"
    agent_career_model: str = "gemini-2.5-flash-lite"
    agent_deep_dive_model: str = "gemini-2.5-flash-lite"
    agent_recruiter_model: str = "gemini-2.5-flash-lite"
    # The single Person the avatar represents — used to seed graph-fact lookups
    # for relational career queries where the query says "he"/"his".
    avatar_person_name: str = "Aniket Gaudgaul"
    # Context-assembly budgets (retrieval can return ~8k-char parent sections;
    # cap so the generation prompt stays bounded — see retrieval checkpoint).
    agent_max_contexts: int = 6
    agent_max_context_chars: int = 4000
    # Recent conversation turns included for follow-up questions.
    agent_history_turns: int = 4
    # Figures shown to the specialist, which may then include them in the answer
    # (spec 6.3). Small on purpose: every figure is inlined into the prompt as
    # pixels, so this is the main lever on multimodal token cost.
    agent_max_images: int = 3
    # Figures larger than this are described by their caption but not shown — a
    # single oversized raster can blow the request payload.
    agent_image_max_bytes: int = 4_000_000

    # --- LlamaParse (vector-path document parsing, LlamaParse v2 SDK) ---
    llama_cloud_api_key: str = ""
    # Parsing tier: fast | cost_effective | agentic | agentic_plus.
    # agentic_plus = premium AI, highest accuracy — used for the ECIR paper
    # (dense two-column academic PDF with figures/tables/equations).
    llama_parse_tier: str = "agentic_plus"
    # Specialized chart/plot parsing for result figures: agentic | agentic_plus | efficient.
    llama_parse_chart_parsing: str = "agentic"

    # --- Chunking targets (spec 5.4), in approximate tokens ---
    chunk_child_max_tokens: int = 400     # child chunks 200-400 tokens
    chunk_parent_max_tokens: int = 1500   # parent sections up to ~1000-1500
    chunk_tiny_merge_tokens: int = 80     # merge sections smaller than this upward

    # --- Multimodal images (spec 5.7) ---
    # LlamaParse classes each saved image: 'layout' (a cropped figure/table region)
    # or 'embedded' (a raster lifted out of the PDF). Embedded rasters are usually
    # sub-images *inside* a figure (e.g. the symptom thumbnails within Figure 1),
    # so they are retrieval noise — ingest layout crops only, by default.
    ingest_image_categories: str = "layout"
    ingest_image_min_bytes: int = 20_000  # skip thumbnail-sized crops
    # Inline image bytes per embed request; the API rejects oversized payloads.
    image_max_bytes: int = 7_000_000
    # Model used only to match a parsed image to its caption when the source
    # markdown has no inline ![](...) reference (see ingestion/vector/images.py).
    image_caption_match_model: str = "gemini-2.5-flash-lite"
    # Below this, an inferred caption is discarded and the image is left unlinked.
    # A confidently-wrong caption is worse than none: it makes retrieval surface
    # the wrong diagram, whereas an unlinked image just retrieves less well.
    image_caption_min_confidence: float = 0.5

    # --- Qdrant ---
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "ai_avatar_chunks"
    # Named vectors in the collection: one dense (Gemini Embedding 2) + one sparse
    # (BM25). Hybrid retrieval fuses them with Qdrant's native RRF (spec 6.1).
    qdrant_dense_vector: str = "dense"
    qdrant_sparse_vector: str = "bm25"
    qdrant_bm25_model: str = "Qdrant/bm25"

    # --- Retrieval ---
    # Images are retrieved in their own modality-filtered query rather than
    # competing with text chunks in one ranked list (see retrieval/vector.py).
    retrieval_image_limit: int = 2

    @property
    def ingest_image_categories_list(self) -> list[str]:
        return [c.strip() for c in self.ingest_image_categories.split(",") if c.strip()]

    # --- Neo4j ---
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "please_change_me"
    neo4j_database: str = "neo4j"

    # --- Langfuse (optional) ---
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (read env once per process)."""
    return Settings()


settings = get_settings()
