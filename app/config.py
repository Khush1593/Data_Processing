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

    # ----- LLM call throttling -----
    # Stage 0 fires one LLM call per ambiguous/exception column, and up to
    # _ANALYZE_CONCURRENCY tables are analyzed concurrently — without a
    # global cap that's several calls in flight at once, which blows past a
    # provider's per-minute token budget (observed: Groq 8000 TPM 429s).
    # These settings force every LLM call in the process through one gate,
    # so columns are patched one at a time, paced to the active provider's
    # real per-minute request budget, regardless of how many tables are
    # "concurrent" at the profiling level.
    LLM_MAX_CONCURRENT_CALLS: int = 1
    # Floor on the gap between any two calls, independent of RPM (catches
    # token-per-minute caps that aren't expressed as a request count).
    LLM_MIN_INTERVAL_SECONDS: float = 1.5
    # The active provider/tier's real requests-per-minute budget. The actual
    # gap enforced between calls is max(LLM_MIN_INTERVAL_SECONDS, 60 / this).
    # Set this to match whatever you're actually paying for — e.g. 5 for
    # Gemini's free tier, much higher for a paid tier or Groq. Getting this
    # wrong (too high) is what causes a wave of 429 RESOURCE_EXHAUSTED: the
    # calls are already serialized one-at-a-time, just not paced slowly
    # enough to stay under the real quota.
    LLM_REQUESTS_PER_MINUTE: int = 5
    # Extra backoff specifically for 429/rate-limit responses (separate from
    # the generic retry backoff) — a TPM cap clears within its rolling
    # window, so it's worth waiting longer and retrying rather than failing
    # immediately like a real quota exhaustion would.
    LLM_RATE_LIMIT_BACKOFF_SECONDS: float = 8.0

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

    # ----- Dev-mode fallback guard -----
    # When True, every "silently degrade to a naive/deterministic expression
    # after an LLM failure" code path raises FallbackBlockedError instead of
    # returning the degraded expression. OFF by default — fallbacks are the
    # production safety net and stay untouched; this only stops them from
    # executing so failures surface loudly (per-column, in cold_start_error)
    # instead of as a silent wave of new NULLs. Flip back to False to restore
    # normal behaviour, no code changes needed.
    PREPROCESSING_DISABLE_FALLBACKS: bool = False

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
