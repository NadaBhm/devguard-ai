from __future__ import annotations

import logging

from qdrant_client import QdrantClient

from .config import RAGConfig, get_rag_config
from .embeddings import EmbeddingClient
from .llm import GeminiClient
from .models import SearchResult

logger = logging.getLogger(__name__)


def similarity_search(
    query: str,
    job_id: str,
    top_k: int = 5,
    config: RAGConfig | None = None,
) -> list[SearchResult]:
    config = config or get_rag_config()
    collection_name = f"{config.qdrant_collection}_{job_id}"

    client = QdrantClient(url=config.qdrant_url)
    embedder = EmbeddingClient(config)

    # Use query-specific embedding for better retrieval quality
    query_vector = embedder.embed_single(query, is_query=True)

    try:
        result = client.query_points(
            collection_name=collection_name,
            query=query_vector,
            limit=top_k,
            with_payload=True,
        )
        return [
            SearchResult(
                text=point.payload.get("text", "") if point.payload else "",
                path=point.payload.get("path", "unknown") if point.payload else "unknown",
                score=point.score,
                type=point.payload.get("type", "unknown") if point.payload else "unknown",
            )
            for point in result.points
        ]
    except Exception as exc:
        logger.error("Search failed: %s", exc)
        return []


def retrieve_context(
    query: str,
    job_id: str,
    top_k: int = 5,
    config: RAGConfig | None = None,
) -> str:
    results = similarity_search(query, job_id, top_k, config)

    if not results:
        return ""

    parts: list[str] = []
    for result in results:
        parts.append(
            f"[Source: {result.path} | Relevance: {result.score:.3f}]\n{result.text}\n"
        )

    return "\n---\n".join(parts)


def ask_repo(
    query: str,
    job_id: str,
    top_k: int = 5,
    config: RAGConfig | None = None,
) -> str:
    """End-to-end RAG: retrieve context and query Gemini."""
    context = retrieve_context(query, job_id, top_k, config)

    if not context:
        return "No relevant context found for this repository."

    llm = GeminiClient(config)
    return llm.query_with_context(query, context)