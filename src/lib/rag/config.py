"""
RAG Configuration
==================
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final


DEFAULT_QDRANT_URL: Final[str] = os.getenv("QDRANT_URL", "http://localhost:6333")
DEFAULT_QDRANT_COLLECTION: Final[str] = os.getenv("QDRANT_COLLECTION", "devguard_repos")

# Hugging Face embeddings (local, free)
DEFAULT_HF_MODEL: Final[str] = os.getenv("HF_MODEL", "BAAI/bge-base-en-v1.5")
DEFAULT_EMBEDDING_DIM: Final[int] = int(os.getenv("EMBEDDING_DIM", "768"))

# Gemini LLM (free tier)
DEFAULT_GEMINI_MODEL: Final[str] = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

# Chunking
DEFAULT_CHUNK_SIZE: Final[int] = int(os.getenv("CHUNK_SIZE", "1000"))
DEFAULT_CHUNK_OVERLAP: Final[int] = int(os.getenv("CHUNK_OVERLAP", "200"))


@dataclass(frozen=True)
class RAGConfig:
    """RAG pipeline configuration."""

    qdrant_url: str = DEFAULT_QDRANT_URL
    qdrant_collection: str = DEFAULT_QDRANT_COLLECTION
    
    # Hugging Face embeddings
    hf_model: str = DEFAULT_HF_MODEL
    embedding_dim: int = DEFAULT_EMBEDDING_DIM
    
    # Gemini LLM
    gemini_api_key: str | None = os.getenv("GEMINI_API_KEY")
    gemini_model: str = DEFAULT_GEMINI_MODEL
    
    # Chunking
    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP


def get_rag_config() -> RAGConfig:
    """Load RAG configuration from environment."""
    return RAGConfig()