"""Centralised configuration for the Stage 0 pre-processing layer.

All tunables are read from the environment (`.env`). The LLM provider is
fully swappable at runtime via ``LLM_PROVIDER`` — the rest of the codebase
never hardcodes a vendor; it asks :data:`settings` which provider/model/key
to use.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ----- LLM provider selection -----
    LLM_PROVIDER: Literal["groq", "gemini"] = "groq"

    GROQ_MODEL: str = "openai/gpt-oss-120b"
    GROQ_API_KEY: Optional[str] = None

    GEMINI_MODEL: str = "gemini-2.0-flash"
    GEMINI_API_KEY: Optional[str] = None

    LLM_TEMPERATURE: float = 0.1
    LLM_MAX_RETRIES: int = 2
    LLM_TIMEOUT_SECONDS: int = 60

    # ----- Control-plane DB (PostgreSQL) -----
    CONTROL_DB_URI: str = (
        "postgresql+psycopg2://clarum:clarum@localhost:5433/clarum_control"
    )

    # ----- Source DB (client DB) — only used by tests/examples -----
    SOURCE_DB_URI: Optional[str] = None

    # ----- Stage 0 tuning -----
    PREPROCESSING_SAMPLE_SIZE: int = 1000
    PREPROCESSING_NAIVE_CHUNK_SIZE: int = 10000
    PREPROCESSING_CHUNK_SIZE: int = 100000
    PREPROCESSING_NULL_SPIKE_THRESHOLD: float = 0.10
    PREPROCESSING_RECONCILIATION_THRESHOLD: float = 0.005
    PREPROCESSING_ENABLED: bool = True

    # ----- v3.0 Column Intelligence Gate -----
    # distinct_sample_ratio (distinct_count / naive-chunk row count) above
    # which a STRING column with no detected issues is classified FREE_TEXT
    # (excluded from cleaning/LLM as free-form prose). A ratio, not an
    # absolute count, because distinct_count is capped at the naive chunk
    # size and so could never exceed a fixed absolute threshold.
    PREPROCESSING_FREE_TEXT_CARDINALITY_RATIO: float = 0.95
    # Minimum distinct_count required alongside the ratio above, so a tiny
    # table (e.g. 5 all-unique rows) isn't misclassified as free-form prose.
    PREPROCESSING_FREE_TEXT_MIN_DISTINCT: int = 20

    DUCKDB_CACHE_DIR: str = "projects"

    # ----- Debug logging -----
    # When enabled, every step of analyze/approve (profiling, issue
    # detection, LLM prompt + raw response, AST validation, dry-run diff) is
    # written to a per-table markdown file under DEBUG_LOG_DIR for review.
    PREPROCESSING_DEBUG_LOG: bool = False
    DEBUG_LOG_DIR: str = "debug_logs"

    @field_validator("LLM_PROVIDER", mode="before")
    @classmethod
    def _normalise_provider(cls, v: str) -> str:
        return str(v).strip().lower()

    # --- Convenience accessors used by the LLM layer ---
    @property
    def active_model(self) -> str:
        return self.GROQ_MODEL if self.LLM_PROVIDER == "groq" else self.GEMINI_MODEL

    @property
    def active_api_key(self) -> Optional[str]:
        return self.GROQ_API_KEY if self.LLM_PROVIDER == "groq" else self.GEMINI_API_KEY


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton. Call ``get_settings.cache_clear()`` in tests
    that need to re-read the environment."""
    return Settings()


# Module-level convenience handle (mirrors the `settings` import the spec uses).
settings = get_settings()
