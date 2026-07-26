"""
RAG Backend — Retrieval-Augmented Generation
=============================================
Shared library for embedding repo docs and semantic retrieval.

Stack:
- Embeddings: Hugging Face (local, free)
- Vector DB: Qdrant (self-hosted)
- LLM: Google Gemini (free tier)
"""

from __future__ import annotations

from .config import RAGConfig, get_rag_config
from .embeddings import EmbeddingClient, get_embedding
from .ingestion import ingest_repo, ingest_text
from .llm import GeminiClient, query_with_rag
from .retrieval import ask_repo, retrieve_context, similarity_search

__all__ = [
    "RAGConfig",
    "get_rag_config",
    "EmbeddingClient",
    "get_embedding",
    "GeminiClient",
    "ingest_repo",
    "ingest_text",
    "query_with_rag",
    "ask_repo",
    "retrieve_context",
    "similarity_search",
]