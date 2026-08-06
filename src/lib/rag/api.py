"""
RAG Public API — Semantic Retrieval 
===============================================
Single entry-point for the Orchestrator and Chat backend.

Usage:
    from lib.rag.api import ask_about_repo
    answer = ask_about_repo(job_id="550e-...", question="What framework?")
"""

from __future__ import annotations

import logging

from .config import RAGConfig, get_rag_config
from .retrieval import ask_repo, retrieve_context

logger = logging.getLogger(__name__)


def ask_about_repo(
    job_id: str,
    question: str,
    top_k: int = 5,
    config: RAGConfig | None = None,
) -> str:
    """
    Ask a natural-language question about an analyzed repository.

    This is the primary integration point for the Orchestrator Chat node.
    It retrieves relevant chunks from Qdrant and generates an answer via Gemini.

    Args:
        job_id: The CodeSec analysis job ID (links to Qdrant collection).
        question: User question in any language.
        top_k: Number of document chunks to retrieve.

    Returns:
        LLM-generated answer, or a fallback message if no context is found.
    """
    logger.info("[RAG API] job=%s question=%r", job_id, question)
    return ask_repo(question, job_id, top_k, config)


def get_repo_context(
    job_id: str,
    question: str,
    top_k: int = 5,
    config: RAGConfig | None = None,
) -> str:
    """
    Retrieve raw context chunks for a question (debug / custom prompting).

    Returns the formatted context string that would be fed to the LLM,
    without actually calling the LLM.
    """
    return retrieve_context(question, job_id, top_k, config)