"""
Retrieval + LLM
===============
Semantic search + Gemini response generation.
"""

from __future__ import annotations

import logging
from typing import Any

from qdrant_client import QdrantClient

from .config import RAGConfig, get_rag_config
from .embeddings import EmbeddingClient
from .llm import GeminiClient

logger = logging.getLogger(__name__)

def similarity_search(
    query: str,
    job_id: str,
    top_k: int = 5,
    config: RAGConfig | None = None,
) -> list[Any]:
    """Search for relevant chunks."""
    config = config or get_rag_config()
    collection_name = f"{config.qdrant_collection}_{job_id}"

    client = QdrantClient(url=config.qdrant_url)
    embedder = EmbeddingClient(config)

    query_vector = embedder.embed_single(query)

    try:
        result = client.query_points(
            collection_name=collection_name,
            query=query_vector,
            limit=top_k,
            with_payload=True,
        )
        return result.points
    except Exception as exc:
        logger.error("Search failed: %s", exc)
        return []

def retrieve_context(
    query: str,
    job_id: str,
    top_k: int = 5,
    config: RAGConfig | None = None,
) -> str:
    """Format retrieved chunks for prompt."""
    results = similarity_search(query, job_id, top_k, config)

    if not results:
        return ""

    parts: list[str] = []
    for point in results:
        payload = point.payload or {}
        text = payload.get("text", "")
        path = payload.get("path", "unknown")
        score = point.score

        parts.append(f"[Source: {path} | Relevance: {score:.3f}]\n{text}\n")

    return "\n---\n".join(parts)


def ask_repo(
    query: str,
    job_id: str,
    top_k: int = 5,
    config: RAGConfig | None = None,
) -> str:
    """
    End-to-end RAG: retrieve context + query Gemini.

    Args:
        query: User question about the repo.
        job_id: Job ID (links to Qdrant collection).
        top_k: Number of chunks to retrieve.

    Returns:
        Gemini's answer based on repo context.
    """
    context = retrieve_context(query, job_id, top_k, config)

    if not context:
        return "No relevant context found for this repository."

    llm = GeminiClient(config)
    return llm.query_with_context(query, context)