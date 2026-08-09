"""Shared configuration for DevGuard AI."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class GeminiConfig:
    """Gemini API configuration."""
    API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    # Must match a real value in gemini_client.GeminiModel -- "gemini-3.5-flash"
    # doesn't exist there (only gemini-2.5-*), so any caller that actually
    # wired GeminiConfig.DEFAULT_MODEL into GeminiClient(model=...) would send
    # an invalid model name to the API. Currently dormant (no call site does
    # that yet -- GeminiClient() defaults to GeminiModel.FLASH on its own),
    # but fixed here so it doesn't become a real bug the first time someone
    # uses this config as intended.
    DEFAULT_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    TEMPERATURE: float = float(os.getenv("GEMINI_TEMPERATURE", "0.3"))
    MAX_OUTPUT_TOKENS: int = int(os.getenv("GEMINI_MAX_TOKENS", "4096"))
    REQUEST_TIMEOUT: int = int(os.getenv("GEMINI_TIMEOUT", "60"))


# Validate on import
if not GeminiConfig.API_KEY:
    import warnings
    warnings.warn("GEMINI_API_KEY not set. LLM features will fail.")