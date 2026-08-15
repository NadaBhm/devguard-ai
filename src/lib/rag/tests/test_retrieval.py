from unittest.mock import MagicMock, patch

import pytest

from lib.rag.retrieval import similarity_search, retrieve_context, ask_repo
from lib.rag.models import SearchResult
from lib.rag.config import RAGConfig


@pytest.fixture
def mock_config():
    return RAGConfig(
        qdrant_url="http://mock:6333",
        qdrant_collection="test",
        gemini_api_key="fake-key",
        gemini_model="gemini-1.5-flash",
    )


class TestSimilaritySearch:
    @patch("lib.rag.retrieval.QdrantClient")
    @patch("lib.rag.retrieval.EmbeddingClient")
    def test_search_returns_results(self, mock_embedder_class, mock_client_class, mock_config):
        mock_embedder = MagicMock()
        mock_embedder.embed_single.return_value = [0.1] * 768
        mock_embedder_class.return_value = mock_embedder

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.points = [
            MagicMock(score=0.95, payload={"text": "hello", "path": "README.md"})
        ]
        mock_client.query_points.return_value = mock_response
        mock_client_class.return_value = mock_client

        results = similarity_search("test query", job_id="test-job", config=mock_config)

        assert len(results) == 1
        assert results[0].score == 0.95


class TestRetrieveContext:
    @patch("lib.rag.retrieval.similarity_search")
    def test_retrieve_formats_context(self, mock_search):
        mock_search.return_value = [
            SearchResult(text="FastAPI docs", path="README.md", score=0.9, type="documentation")
        ]

        context = retrieve_context("what framework?", job_id="test-job")

        assert "FastAPI docs" in context
        assert "README.md" in context


class TestAskRepo:
    @patch("lib.rag.retrieval.GeminiClient")
    @patch("lib.rag.retrieval.retrieve_context")
    def test_ask_repo_with_context(self, mock_retrieve, mock_client_class):
        mock_retrieve.return_value = "This is a FastAPI project."
        mock_client = MagicMock()
        mock_client.query_with_context.return_value = "It uses FastAPI."
        mock_client_class.return_value = mock_client

        response = ask_repo("What framework?", job_id="test")

        assert "FastAPI" in response

    @patch("lib.rag.retrieval.retrieve_context")
    def test_ask_repo_no_context(self, mock_retrieve):
        mock_retrieve.return_value = ""

        response = ask_repo("What framework?", job_id="test")

        assert "No relevant context" in response