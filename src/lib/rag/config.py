from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Final

# Import-time-safe fallbacks. Environment overrides are read at call time in
# get_rag_config() (never at import time) so that a later load_dotenv() cannot
# silently change what an already-imported module reads as its defaults.
DEFAULT_QDRANT_URL: Final[str] = "http://localhost:6333"
DEFAULT_QDRANT_COLLECTION: Final[str] = "devguard_repos"

DEFAULT_HF_MODEL: Final[str] = "BAAI/bge-large-en-v1.5"
DEFAULT_EMBEDDING_DIM: Final[int] = 1024

DEFAULT_GEMINI_MODEL: Final[str] = "gemini-flash-latest"

DEFAULT_CHUNK_SIZE: Final[int] = 1000
DEFAULT_CHUNK_OVERLAP: Final[int] = 200


@dataclass(frozen=True)
class RAGConfig:

    qdrant_url: str = DEFAULT_QDRANT_URL
    qdrant_collection: str = DEFAULT_QDRANT_COLLECTION

    hf_model: str = DEFAULT_HF_MODEL
    embedding_dim: int = DEFAULT_EMBEDDING_DIM

    gemini_api_key: str | None = field(default=None)
    gemini_model: str = DEFAULT_GEMINI_MODEL

    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP


def get_rag_config() -> RAGConfig:
    """Load RAG configuration from environment (reads env vars at call time)."""
    return RAGConfig(
        qdrant_url=os.getenv("QDRANT_URL", DEFAULT_QDRANT_URL),
        qdrant_collection=os.getenv("QDRANT_COLLECTION", DEFAULT_QDRANT_COLLECTION),
        hf_model=os.getenv("HF_MODEL", DEFAULT_HF_MODEL),
        embedding_dim=int(os.getenv("EMBEDDING_DIM", str(DEFAULT_EMBEDDING_DIM))),
        gemini_api_key=os.getenv("GEMINI_API_KEY"),
        gemini_model=os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
        chunk_size=int(os.getenv("CHUNK_SIZE", str(DEFAULT_CHUNK_SIZE))),
        chunk_overlap=int(os.getenv("CHUNK_OVERLAP", str(DEFAULT_CHUNK_OVERLAP))),
    )
