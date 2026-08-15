from __future__ import annotations

from .api import ask_about_repo, get_repo_context
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
    "ask_about_repo",
    "get_repo_context",
]