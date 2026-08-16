from __future__ import annotations

import logging
from typing import Any

from sentence_transformers import SentenceTransformer

from .config import RAGConfig, get_rag_config

logger = logging.getLogger(__name__)


class EmbeddingClient:
    _model_cache: dict[str, SentenceTransformer] = {}

    def __init__(self, config: RAGConfig | None = None) -> None:
        self.config = config or get_rag_config()
        self.model = self._load_model()

    def _load_model(self) -> SentenceTransformer:
        model_name = self.config.hf_model

        if model_name not in EmbeddingClient._model_cache:
            logger.info("Loading embedding model: %s", model_name)
            EmbeddingClient._model_cache[model_name] = SentenceTransformer(
                model_name,
                device="cpu",
            )

        return EmbeddingClient._model_cache[model_name]

    def embed(self, texts: list[str], is_query: bool = False) -> list[list[float]]:
        """Generate embeddings; if is_query, apply the BGE retrieval prefix (documents stay raw)."""
        if not texts:
            return []

        if is_query and "bge" in self.config.hf_model.lower():
            texts = [
                "Represent this sentence for searching relevant passages: " + t
                for t in texts
            ]

        try:
            embeddings = self.model.encode(
                texts,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            return embeddings.tolist()
        except Exception as exc:
            logger.error("Embedding generation failed: %s", exc)
            raise

    def embed_single(self, text: str, is_query: bool = False) -> list[float]:
        return self.embed([text], is_query=is_query)[0]


def get_embedding(text: str, config: RAGConfig | None = None) -> list[float]:
    client = EmbeddingClient(config)
    return client.embed_single(text, is_query=True)