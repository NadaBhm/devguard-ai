from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from .config import RAGConfig, get_rag_config
from .embeddings import EmbeddingClient
from qdrant_client.http.exceptions import UnexpectedResponse

logger = logging.getLogger(__name__)


def _chunk_text(text: str, chunk_size: int = 1000, overlap: int = 200) -> list[str]:
    if not text or chunk_size <= 0:
        return []

    chunks: list[str] = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = min(start + chunk_size, text_len)

        # Prefer a word boundary over a mid-word cut (skip at end of text)
        if end < text_len:
            original_end = end
            while end > start and text[end - 1] not in " \n":
                end -= 1
            if end == start:
                end = original_end

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        next_start = end - overlap
        if next_start <= start:  # Prevent infinite loop at end of text
            next_start = end
        start = next_start

    return chunks


def _read_repo_files(repo_path: Path) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    code_extensions = (".py", ".js", ".ts", ".go", ".java", ".rb", ".rs")
    
    IGNORE_DIRS = {".venv", "venv", "node_modules", "__pycache__", ".git", ".pytest_cache", "dist", "build"}

    def _should_ignore(path: Path) -> bool:
        for part in path.parts:
            if part in IGNORE_DIRS:
                return True
        return False

    for pattern in ("README*", "CONTRIBUTING*", "*.md"):
        for file_path in repo_path.rglob(pattern):
            if file_path.is_file() and not _should_ignore(file_path):
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    rel_path = file_path.relative_to(repo_path).as_posix()
                    files.append({
                        "path": rel_path,
                        "content": content,
                        "type": "documentation",
                    })
                except Exception:
                    pass

    code_count = 0
    for ext in code_extensions:
        if code_count >= 20:
            break
        for file_path in repo_path.rglob(f"*{ext}"):
            if code_count >= 20:
                break
            if file_path.is_file() and not _should_ignore(file_path):
                try:
                    content = file_path.read_text(encoding="utf-8", errors="ignore")
                    rel_path = file_path.relative_to(repo_path).as_posix()
                    files.append({
                        "path": rel_path,
                        "content": content,
                        "type": "code",
                    })
                    code_count += 1
                except Exception:
                    pass

    return files

def ingest_repo(
    repo_path: Path,
    job_id: str,
    config: RAGConfig | None = None,
) -> int:
    """Ingest a repository into Qdrant; returns the number of chunks ingested."""
    config = config or get_rag_config()
    collection_name = f"{config.qdrant_collection}_{job_id}"

    client = QdrantClient(url=config.qdrant_url)
    embedder = EmbeddingClient(config)

    try:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(
                size=config.embedding_dim,
                distance=Distance.COSINE,
            ),
        )
        logger.info("Created Qdrant collection: %s", collection_name)
    except UnexpectedResponse as exc:
        if "already exists" in str(exc).lower():
            logger.info("Collection %s already exists", collection_name)
        else:
            logger.error("Qdrant error creating collection: %s", exc)
            raise

    files = _read_repo_files(repo_path)
    all_chunks: list[dict[str, Any]] = []

    for file_info in files:
        chunks = _chunk_text(file_info["content"], config.chunk_size, config.chunk_overlap)
        for i, chunk in enumerate(chunks):
            all_chunks.append({
                "text": chunk,
                "path": file_info["path"],
                "type": file_info["type"],
                "chunk_index": i,
            })

    if not all_chunks:
        logger.warning("No chunks generated from repo: %s", repo_path)
        return 0

    texts = [c["text"] for c in all_chunks]
    embeddings = embedder.embed(texts)

    points = [
        PointStruct(
            id=str(uuid.uuid4()),
            vector=embedding,
            payload={
                "text": chunk["text"],
                "path": chunk["path"],
                "type": chunk["type"],
                "chunk_index": chunk["chunk_index"],
                "job_id": job_id,
            },
        )
        for chunk, embedding in zip(all_chunks, embeddings)
    ]

    client.upsert(collection_name=collection_name, points=points)
    logger.info("Ingested %d chunks into %s", len(points), collection_name)

    return len(points)


def ingest_text(
    text: str,
    job_id: str,
    metadata: dict[str, Any] | None = None,
    config: RAGConfig | None = None,
) -> int:
    config = config or get_rag_config()
    collection_name = f"{config.qdrant_collection}_{job_id}"

    client = QdrantClient(url=config.qdrant_url)
    embedder = EmbeddingClient(config)

    try:
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(
                size=config.embedding_dim,
                distance=Distance.COSINE,
            ),
        )
    except Exception:
        pass

    chunks = _chunk_text(text, config.chunk_size, config.chunk_overlap)
    if not chunks:
        return 0

    embeddings = embedder.embed(chunks)

    points = [
        PointStruct(
            id=str(uuid.uuid4()),
            vector=embedding,
            payload={
                "text": chunk,
                **(metadata or {}),
                "job_id": job_id,
            },
        )
        for chunk, embedding in zip(chunks, embeddings)
    ]

    client.upsert(collection_name=collection_name, points=points)
    return len(points)