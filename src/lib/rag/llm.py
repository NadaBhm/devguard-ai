from __future__ import annotations

import logging
from typing import Any

from .config import RAGConfig, get_rag_config
from .gemini_client import GeminiClientFactory

logger = logging.getLogger(__name__)


class GeminiClient:
    def __init__(self, config: RAGConfig | None = None) -> None:
        self.config = config or get_rag_config()
        # Factory handles global configure() centrally
        self.model: Any = GeminiClientFactory.create(self.config)

    def query(self, prompt: str) -> str:
        try:
            response = self.model.generate_content(prompt)
            return response.text or ""
        except Exception as exc:
            logger.error("Gemini query failed: %s", exc)
            return f"Error: {exc}"

    def query_with_context(self, query: str, context: str) -> str:
        prompt = f"""You are a helpful assistant analyzing a GitHub repository.
Use the following context to answer the question. If the answer is not in the context, say so.

Context:
{context}

Question: {query}

Answer:"""

        return self.query(prompt)


def query_with_rag(query: str, context: str, config: RAGConfig | None = None) -> str:
    client = GeminiClient(config)
    return client.query_with_context(query, context)