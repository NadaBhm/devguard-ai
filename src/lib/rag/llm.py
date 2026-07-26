"""
LLM Integration — Google Gemini
============================================
Queries Gemini with RAG-retrieved context.
"""

from __future__ import annotations

import logging
from .config import RAGConfig, get_rag_config
from .gemini_client import GeminiClientFactory

logger = logging.getLogger(__name__)


class GeminiClient:
    """Client for querying Google Gemini."""

    def __init__(self, config: RAGConfig | None = None) -> None:
        self.config = config or get_rag_config()
        # ✅ No more genai.configure() here — factory handles it centrally
        self.model = GeminiClientFactory.create(self.config)

    def query(self, prompt: str) -> str:
        """Send prompt to Gemini and return response."""
        try:
            response = self.model.generate_content(prompt)  # type: ignore[reportAttributeAccessIssue]
            return response.text or ""  # type: ignore[reportAttributeAccessIssue]
        except Exception as exc:
            logger.error("Gemini query failed: %s", exc)
            return f"Error: {exc}"

    def query_with_context(self, query: str, context: str) -> str:
        """
        Query with RAG context.

        Args:
            query: User question.
            context: Retrieved context from vector DB.
        """
        prompt = f"""You are a helpful assistant analyzing a GitHub repository.
Use the following context to answer the question. If the answer is not in the context, say so.

Context:
{context}

Question: {query}

Answer:"""

        return self.query(prompt)


def query_with_rag(query: str, context: str, config: RAGConfig | None = None) -> str:
    """Convenience function."""
    client = GeminiClient(config)
    return client.query_with_context(query, context)