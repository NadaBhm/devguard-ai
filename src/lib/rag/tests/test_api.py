"""Tests for RAG Public API."""
from unittest.mock import MagicMock, patch

import pytest

from lib.rag.api import ask_about_repo, get_repo_context


class TestAskAboutRepo:
    """Primary integration point for Orchestrator Chat."""

    @patch("lib.rag.api.ask_repo")
    def test_ask_about_repo_delegates(self, mock_ask_repo):
        mock_ask_repo.return_value = "It uses FastAPI."

        result = ask_about_repo(
            job_id="550e8400-e29b-41d4-a716-446655440000",
            question="What framework?",
            top_k=3,
        )

        assert result == "It uses FastAPI."
        mock_ask_repo.assert_called_once_with(
            "What framework?",
            "550e8400-e29b-41d4-a716-446655440000",
            3,
            None,
        )

    @patch("lib.rag.api.ask_repo")
    def test_ask_about_repo_no_context(self, mock_ask_repo):
        mock_ask_repo.return_value = "No relevant context found for this repository."

        result = ask_about_repo(job_id="test-job", question="random?")

        assert "No relevant context" in result


class TestGetRepoContext:
    """Raw context retrieval for debugging."""

    @patch("lib.rag.api.retrieve_context")
    def test_get_repo_context_delegates(self, mock_retrieve):
        mock_retrieve.return_value = "[Source: README.md]\nThis is a FastAPI project."

        result = get_repo_context(
            job_id="test-job",
            question="What framework?",
            top_k=5,
        )

        assert "FastAPI" in result
        mock_retrieve.assert_called_once_with("What framework?", "test-job", 5, None)