"""Shared configuration for DevGuard AI."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class GeminiConfig:
    """Gemini API configuration."""
    API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    DEFAULT_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")
    TEMPERATURE: float = float(os.getenv("GEMINI_TEMPERATURE", "0.3"))
    MAX_OUTPUT_TOKENS: int = int(os.getenv("GEMINI_MAX_TOKENS", "4096"))
    REQUEST_TIMEOUT: int = int(os.getenv("GEMINI_TIMEOUT", "60"))


# Validate on import
if not GeminiConfig.API_KEY:
    import warnings
    warnings.warn("GEMINI_API_KEY not set. LLM features will fail.")