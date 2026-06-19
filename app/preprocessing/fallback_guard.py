"""Dev-mode switch to stop silent fallbacks from executing.

Several places in the pipeline (exception_capture.py, llm_resolver.py) catch
an LLM failure / unverifiable patch and quietly degrade to a naive
deterministic expression so the pipeline never gets stuck. That's the right
behavior in production, but it hides exactly which column/table the LLM is
failing on behind a wave of new NULLs.

``guard()`` is called right before each of those fallback branches returns
its degraded expression. When ``PREPROCESSING_DISABLE_FALLBACKS`` is set, it
raises instead — surfacing the failure per-column (as ``cold_start_error``)
instead of silently shipping it. The fallback code itself is never touched;
flipping the setting back to False restores normal behavior immediately.
"""
from __future__ import annotations

from app.config import settings


class FallbackBlockedError(RuntimeError):
    """Raised in place of a silent fallback when fallbacks are disabled."""


def guard(message: str) -> None:
    if settings.PREPROCESSING_DISABLE_FALLBACKS:
        raise FallbackBlockedError(message)
