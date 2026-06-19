"""Provider-agnostic structured LLM generation.

The rest of the codebase calls :func:`_generate_structured` and gets back a
validated Pydantic object — it never knows or cares which vendor answered.
The active provider/model/key come from :mod:`app.config` (driven by ``.env``)
unless explicitly overridden per call.

Supported providers:
  * ``groq``   — OpenAI-compatible chat completions, JSON object mode.
  * ``gemini`` — Google GenAI SDK, JSON mime type + response schema.

Network access for each provider is isolated behind a single callable in
``_PROVIDER_CALLS`` so tests can monkeypatch or register a fake provider
without any API key.
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Callable, Type, TypeVar

from pydantic import BaseModel, ValidationError

from app.config import get_settings
from app.debug_logger import DebugLogger

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Raised when the LLM call fails or returns unparseable output."""


# --------------------------------------------------------------------------
# Global call throttle — see config.LLM_MAX_CONCURRENT_CALLS /
# LLM_MIN_INTERVAL_SECONDS. Every LLM call in the process passes through
# this gate, so columns/tables are patched one at a time (or N at a time)
# with a minimum gap between calls, instead of every concurrently-analyzed
# table firing its own LLM call at once and blowing a per-minute token cap.
# --------------------------------------------------------------------------
_call_semaphore = threading.Semaphore(get_settings().LLM_MAX_CONCURRENT_CALLS)
_timing_lock = threading.Lock()
_last_call_at = 0.0


def _effective_min_interval(s) -> float:
    """The real gap to enforce between calls: whichever is larger of the
    flat floor and the spacing implied by the provider's RPM budget.

    A flat ``LLM_MIN_INTERVAL_SECONDS`` alone is wrong whenever it's looser
    than the provider's actual per-minute request cap (e.g. 1.5s implies
    ~40 calls/min, but a free-tier model capped at 5 RPM needs 12s between
    calls) — that mismatch is what produces a wave of 429 RESOURCE_EXHAUSTED
    even though calls are already serialized one-at-a-time.
    """
    rpm = max(1, s.LLM_REQUESTS_PER_MINUTE)
    return max(s.LLM_MIN_INTERVAL_SECONDS, 60.0 / rpm)


def _throttle(min_interval: float) -> None:
    global _last_call_at
    with _timing_lock:
        wait = _last_call_at + min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call_at = time.monotonic()


# --------------------------------------------------------------------------
# Raw provider calls: (prompt, model, api_key, temperature, timeout) -> str
# Each returns the raw text the model produced (expected to contain JSON).
# --------------------------------------------------------------------------
def _call_groq(prompt: str, model: str, api_key: str, temperature: float, timeout: int) -> str:
    from groq import Groq

    client = Groq(api_key=api_key, timeout=float(timeout))
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": "You are a senior data engineer. Respond ONLY with a single JSON object. No prose, no markdown fences.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=temperature,
        response_format={"type": "json_object"},
    )
    return resp.choices[0].message.content or ""


def _call_gemini(prompt: str, model: str, api_key: str, temperature: float, timeout: int) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=temperature,
            response_mime_type="application/json",
            http_options=types.HttpOptions(timeout=timeout * 1000),  # ms
        ),
    )
    return resp.text or ""


_PROVIDER_CALLS: dict[str, Callable[[str, str, str, float, int], str]] = {
    "groq": _call_groq,
    "gemini": _call_gemini,
}


def register_provider(name: str, fn: Callable[[str, str, str, float, int], str]) -> None:
    """Register/override a provider call. Used by tests to inject a fake."""
    _PROVIDER_CALLS[name.lower()] = fn


# --------------------------------------------------------------------------
# JSON extraction — models occasionally wrap JSON in prose or ``` fences.
# --------------------------------------------------------------------------
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(raw: str) -> dict:
    if not raw or not raw.strip():
        raise LLMError("LLM returned empty response.")
    text = raw.strip()

    # 1. Direct parse.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Fenced ```json ... ``` block.
    m = _FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 3. First balanced-looking {...} span.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    raise LLMError(f"Could not extract JSON from LLM response: {raw[:200]!r}")


# Substrings that mark an error as pointless to retry: a quota/daily-limit or
# auth/bad-model failure will NOT clear within a few seconds of backoff, and
# retrying it burns yet another request against the same exhausted daily
# quota. Detect these and stop immediately.
_NON_RETRYABLE_MARKERS = (
    "resource_exhausted",
    "quota",
    "insufficient_quota",
    "invalid api key",
    "api_key_invalid",
    "permission_denied",
    "unauthorized",
    "401",
    "403",
    "not found",
    "404",
)

# A plain 429/"rate limit" (as opposed to a daily quota exhaustion above) is
# scoped to a rolling per-minute/per-token window — it clears on its own, so
# it's worth a longer backoff + retry rather than giving up immediately.
_RATE_LIMIT_MARKERS = (
    "429",
    "rate_limit",
    "rate limit",
    "too many requests",
)

# Gemini's free-tier 429 RESOURCE_EXHAUSTED response contains "quota" for
# BOTH a rolling per-minute window (quotaId "...PerMinute...", clears in
# seconds) AND a hard daily cap (quotaId "...PerDay...", clears tomorrow).
# Both also include a RetryInfo.retryDelay / "Please retry in Ns" — that
# phrasing is NOT a reliable signal on its own (a first version of this
# check used it and would have retried a daily exhaustion for no benefit).
# Only the literal "PerMinute" quotaId name distinguishes the case that's
# actually worth backing off and retrying for.
_ROLLING_WINDOW_MARKERS = (
    "per minute",
    "perminute",
    "per_minute",
)

# The daily-cap counterpart — kept explicit (not just "absence of the
# per-minute marker") so a future provider wording change fails closed
# (stays non-retryable) instead of silently starting to retry it.
_DAILY_QUOTA_MARKERS = (
    "per day",
    "perday",
    "per_day",
)


def _is_non_retryable(err: Exception) -> bool:
    msg = str(err).lower()
    if any(marker in msg for marker in _ROLLING_WINDOW_MARKERS):
        return False
    return any(marker in msg for marker in _NON_RETRYABLE_MARKERS)


def _is_rate_limited(err: Exception) -> bool:
    msg = str(err).lower()
    return any(marker in msg for marker in _RATE_LIMIT_MARKERS)


def _generate_structured(
    prompt: str,
    response_schema: Type[T],
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    temperature: float | None = None,
    max_retries: int | None = None,
    debug: DebugLogger | None = None,
) -> T:
    """Call the active LLM provider and parse the response into ``response_schema``.

    Falls back to :mod:`app.config` for any unspecified provider/model/key.
    Retries on transient failures and JSON parse errors.
    """
    s = get_settings()
    provider = (provider or s.LLM_PROVIDER).lower()
    model = model or s.active_model
    api_key = api_key or s.active_api_key
    temperature = s.LLM_TEMPERATURE if temperature is None else temperature
    retries = s.LLM_MAX_RETRIES if max_retries is None else max_retries
    timeout = s.LLM_TIMEOUT_SECONDS

    call = _PROVIDER_CALLS.get(provider)
    if call is None:
        raise LLMError(f"Unknown LLM provider {provider!r}. Known: {list(_PROVIDER_CALLS)}")
    if not api_key:
        raise LLMError(
            f"No API key configured for provider {provider!r}. "
            f"Set {'GROQ_API_KEY' if provider == 'groq' else 'GEMINI_API_KEY'} in .env."
        )

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        raw: str | None = None
        try:
            with _call_semaphore:
                _throttle(_effective_min_interval(s))
                raw = call(prompt, model, api_key, temperature, timeout)
            data = _extract_json(raw)
            result = response_schema.model_validate(data)
            if debug:
                debug.llm_call(
                    f"LLM call ({provider}/{model}, attempt {attempt + 1}) — OK",
                    prompt, raw_response=raw,
                )
            return result
        except (LLMError, ValidationError) as e:
            last_err = e
        except Exception as e:  # transient network/provider errors
            last_err = e
        if debug:
            debug.llm_call(
                f"LLM call ({provider}/{model}, attempt {attempt + 1}) — FAILED",
                prompt, raw_response=raw, error=str(last_err),
            )
        # Quota/auth/not-found errors won't clear on retry — stop now rather
        # than waste more requests against an already-exhausted daily quota.
        if _is_non_retryable(last_err):
            raise LLMError(f"LLM generation failed (non-retryable): {last_err}")
        if attempt < retries:
            if _is_rate_limited(last_err):
                time.sleep(s.LLM_RATE_LIMIT_BACKOFF_SECONDS * (attempt + 1))
            else:
                time.sleep(0.5 * (attempt + 1))

    raise LLMError(f"LLM generation failed after {retries + 1} attempts: {last_err}")
