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
    """Ask a natural-language question about an analyzed repository.

    Primary integration point for the Orchestrator Chat node; returns a fallback
    message when no context is found.
    """
    logger.info("[RAG API] job=%s question=%r", job_id, question)
    return ask_repo(question, job_id, top_k, config)


def get_repo_context(
    job_id: str,
    question: str,
    top_k: int = 5,
    config: RAGConfig | None = None,
) -> str:
    """Retrieve raw context chunks for a question (debug / custom prompting) without calling the LLM."""
    return retrieve_context(question, job_id, top_k, config)