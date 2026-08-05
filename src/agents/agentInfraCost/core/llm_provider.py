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
"""

from __future__ import annotations

import logging
import os
from typing import Final

import httpx

logger = logging.getLogger(__name__)

_OPENROUTER_URL: Final[str] = "https://openrouter.ai/api/v1/chat/completions"
_DEFAULT_MODEL: Final[str] = "openai/gpt-4o-mini"
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 20.0


def call_llm(
    prompt: str,
    system_instruction: str,
    *,
    model: str | None = None,
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
        temperature: Passed straight through to the API.
        timeout: Hard ceiling in seconds; a slow LLM must never hang the
            pipeline.

    Returns:
        The completion text, or ``None`` if ``OPENROUTER_API_KEY`` is unset,
        the request fails, times out, or the response can't be parsed.
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return None

    resolved_model = model or os.getenv("OPENROUTER_MODEL") or _DEFAULT_MODEL

    try:
        response = httpx.post(
            _OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": resolved_model,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except httpx.HTTPStatusError as exc:
        # exc_info alone only logs the status code -- the response body
        # usually names the real reason (bad model slug, policy setting,
        # rate limit, ...), so log it explicitly instead of discarding it.
        logger.warning(
            "OpenRouter call failed (%s); caller falls back. Response body: %s",
            exc.response.status_code, exc.response.text,
        )
        return None
    except Exception:
        logger.warning("OpenRouter call failed; caller falls back.", exc_info=True)
        return None
