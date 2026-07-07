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

    # --- Gemini ---
    google_api_key: str = ""
    gemini_router_model: str = "gemini-2.5-flash-lite"
    gemini_flash_model: str = "gemini-2.5-flash"
    gemini_pro_model: str = "gemini-2.5-pro"
    gemini_embedding_model: str = "gemini-embedding-2"
    embedding_dim: int = 1536

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

    # --- Qdrant ---
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str = ""
    qdrant_collection: str = "ai_avatar_chunks"
    # Named vectors in the collection: one dense (Gemini Embedding 2) + one sparse
    # (BM25). Hybrid retrieval fuses them with Qdrant's native RRF (spec 6.1).
    qdrant_dense_vector: str = "dense"
    qdrant_sparse_vector: str = "bm25"
    qdrant_bm25_model: str = "Qdrant/bm25"

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
