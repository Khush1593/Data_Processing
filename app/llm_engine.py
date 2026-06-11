"""Compatibility shim for the host pipeline's Stage 3 import path.

The pre-processing spec imports ``from app.llm_engine import _generate_structured``.
In this standalone build that function lives in :mod:`app.llm.engine`; re-export
it here so the spec's import path works unchanged when wired into the host app.
"""
from app.llm.engine import (  # noqa: F401
    LLMError,
    _generate_structured,
    register_provider,
)

__all__ = ["_generate_structured", "register_provider", "LLMError"]
