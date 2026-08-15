"""Gemini client factory — isolates global configure() side-effects."""

from __future__ import annotations

import warnings
from typing import Any

# Suppress deprecation warning for google.generativeai
with warnings.catch_warnings():
    warnings.simplefilter("ignore", FutureWarning)
    import google.generativeai as genai

from .config import RAGConfig, get_rag_config


class GeminiClientFactory:
    """Create GenerativeModel instances without polluting every __init__."""
    
    _configured_key: str | None = None

    @classmethod
    def create(cls, config: RAGConfig | None = None) -> Any:
        """Return a fresh Gemini model (configures globally on first call)."""
        config = config or get_rag_config()
        key = config.gemini_api_key or ""
        model_name = config.gemini_model
        
        # Guard: only re-configure if the key actually changes
        if cls._configured_key != key and key:
            genai.configure(api_key=key)  # type: ignore[reportPrivateImportUsage]
            cls._configured_key = key
        
        # Fresh instance per call — no global model cache, which leaks state between tests
        return genai.GenerativeModel(model_name)  # type: ignore[reportPrivateImportUsage]