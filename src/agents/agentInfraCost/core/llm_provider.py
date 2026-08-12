"""Phase B (post-mission extension): provider-agnostic LLM call abstraction.

Wraps OpenRouter's OpenAI-compatible chat-completions endpoint. "Provider-
agnostic" means the caller never hardcodes which model answers a prompt —
the model is always a parameter, defaulting to ``_DEFAULT_MODEL`` but
overridable per call or via the ``OPENROUTER_MODEL`` environment variable.
Swapping models never requires touching a caller like
``llm_architecture_advisor.py``.

Same failure contract as ``core.llm_enrichment``'s ``_call_gemini``: every
failure mode (missing key, network error, timeout, non-2xx status,
unparsable body) collapses to the same signal — return ``None`` — so callers
always write exactly one fallback branch, never one per error type.

Retry policy: transient failures are retried with exponential backoff before
giving up, so a single provider hiccup doesn't silently drop an LLM stage:

  - 5xx / 429 / 408, and OpenRouter's *provider-side* 404s ("Provider returned
    error" — the upstream Nvidia/etc. endpoint flaked; observed intermittently
    on free-tier models) are retried.
  - Dropped connections / truncated reads (``httpx.TransportError``) are
    retried.
  - A 200 whose body lacks the expected ``choices`` shape is retried.
  - Permanent failures never waste an attempt: a plain 404 (unknown model
    slug, reported by OpenRouter itself), other 4xx, and timeouts (the caller
    already waited ``timeout`` seconds for the response) fail immediately.
"""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any, Final

import httpx

logger = logging.getLogger(__name__)

_OPENROUTER_URL: Final[str] = "https://openrouter.ai/api/v1/chat/completions"
_DEFAULT_MODEL: Final[str] = "nvidia/nemotron-3-ultra-550b-a55b:free"
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 20.0

_MAX_LLM_RETRIES: Final[int] = 3
_RETRY_BASE_DELAY_SECONDS: Final[float] = 1.0
_RETRY_FACTOR: Final[float] = 2.0
_RETRY_JITTER_SECONDS: Final[float] = 0.1

# Status codes worth another attempt. 404 is only retryable when OpenRouter
# attributes it to the upstream provider (see _is_transient_status) — an
# unknown-model 404 comes from OpenRouter itself and would replay forever.
_RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({408, 429, 500, 502, 503, 504})
_PROVIDER_404_MARKER: Final[str] = "Provider returned error"


def _retry_delay(attempt: int) -> float:
    """Exponential backoff (plus jitter) before the retry following `attempt`."""
    wait = _RETRY_BASE_DELAY_SECONDS * (_RETRY_FACTOR ** (attempt - 1))
    return wait + random.uniform(0, _RETRY_JITTER_SECONDS)


def _is_transient_status(status_code: int, body: str) -> bool:
    if status_code in _RETRYABLE_STATUS_CODES:
        return True
    return status_code == 404 and _PROVIDER_404_MARKER in body


def _is_transient_connection_error(exc: Exception) -> bool:
    """Dropped connections / truncated reads are retryable. An expired
    timeout is not: the caller already waited `timeout` for it, and a repeat
    attempt would compound that latency without improving the odds."""
    return isinstance(exc, httpx.TransportError) and not isinstance(
        exc, httpx.TimeoutException
    )


def _is_transient_parse_error(exc: Exception) -> bool:
    """A 200 whose body doesn't match the expected shape (no `choices` array,
    unparsable JSON). Retrying is cheap and catches intermittent response
    glitches from OpenRouter's upstreams."""
    return isinstance(exc, (KeyError, IndexError, ValueError, TypeError))


def call_llm(
    prompt: str,
    system_instruction: str,
    *,
    model: str | None = None,
    provider_order: list[str] | None = None,
    temperature: float = 0.2,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> str | None:
    """Ask an LLM for one completion; return its text, or None on any failure.

    Args:
        prompt: The user-role message.
        system_instruction: The system-role message.
        model: OpenRouter model slug (e.g. ``"openai/gpt-4o-mini"``,
            ``"anthropic/claude-3.5-sonnet"``). Defaults to
            ``_DEFAULT_MODEL``, or to the ``OPENROUTER_MODEL`` environment
            variable if set — either way, never hardcoded in a caller.
        provider_order: Which OpenRouter-side provider(s) may serve the
            request (e.g. ``["nvidia"]``), in priority order. Defaults to
            the ``OPENROUTER_PROVIDER`` environment variable if set (comma-
            separated for more than one), otherwise omitted entirely and
            OpenRouter's own routing decides. Some accounts have a default
            provider preference that doesn't match every model — this is
            how a caller overrides that per-request without needing to
            change the account's own settings.
        temperature: Passed straight through to the API.
        timeout: Hard ceiling in seconds; a slow LLM must never hang the
            pipeline.

    Returns:
        The completion text, or ``None`` if ``OPENROUTER_API_KEY`` is unset,
        the request fails after its retries, times out, or the response can't
        be parsed.
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return None

    resolved_model = model or os.getenv("OPENROUTER_MODEL") or _DEFAULT_MODEL
    resolved_provider_order = provider_order or (
        [p.strip() for p in os.getenv("OPENROUTER_PROVIDER", "").split(",") if p.strip()] or None
    )

    payload: dict[str, Any] = {
        "model": resolved_model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": prompt},
        ],
    }
    if resolved_provider_order:
        payload["provider"] = {"order": resolved_provider_order, "allow_fallbacks": False}

    for attempt in range(1, _MAX_LLM_RETRIES + 1):
        try:
            response = httpx.post(
                _OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as exc:
            # exc_info alone only logs the status code -- the response body
            # usually names the real reason (bad model slug, policy setting,
            # rate limit, provider hiccup, ...), so log it explicitly instead
            # of discarding it.
            status = exc.response.status_code
            body = exc.response.text
            if attempt == _MAX_LLM_RETRIES or not _is_transient_status(status, body):
                logger.warning(
                    "OpenRouter call failed (%s); caller falls back. Response body: %s",
                    status, body,
                )
                return None
            delay = _retry_delay(attempt)
            logger.warning(
                "OpenRouter call failed (%s); retrying in %.1fs (attempt %d/%d). "
                "Response body: %s",
                status, delay, attempt + 1, _MAX_LLM_RETRIES, body,
            )
        except Exception as exc:
            transient = _is_transient_connection_error(exc) or _is_transient_parse_error(exc)
            if attempt == _MAX_LLM_RETRIES or not transient:
                logger.warning("OpenRouter call failed; caller falls back.", exc_info=True)
                return None
            delay = _retry_delay(attempt)
            logger.warning(
                "OpenRouter call failed (%s); retrying in %.1fs (attempt %d/%d).",
                type(exc).__name__, delay, attempt + 1, _MAX_LLM_RETRIES,
            )
        time.sleep(_retry_delay(attempt))

    return None  # pragma: no cover - the loop always returns above
