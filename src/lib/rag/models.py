"""
RAG Pydantic Models
====================
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChunkPayload(BaseModel):
    """Payload for a vector chunk in Qdrant."""

    text: str = Field(..., description="Chunk text content")
    path: str = Field(default="", description="Source file path")
    type: str = Field(default="unknown", description="document or code")
    chunk_index: int = Field(default=0)
    job_id: str = Field(...)


class SearchResult(BaseModel):
    """Formatted search result."""

    text: str = Field(...)
    path: str = Field(...)
    score: float = Field(...)
    type: str = Field(default="unknown")