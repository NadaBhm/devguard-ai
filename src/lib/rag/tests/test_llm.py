from unittest.mock import MagicMock, patch

import pytest

from lib.rag.llm import GeminiClient, query_with_rag
from lib.rag.config import RAGConfig


@pytest.fixture
def mock_config():
    return RAGConfig(
        gemini_api_key="fake-key",
        gemini_model="gemini-1.5-flash",
    )


class TestGeminiClient:
    @patch("lib.rag.llm.GeminiClientFactory")
    def test_query(self, mock_factory, mock_config):
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "Test response"
        mock_model.generate_content.return_value = mock_response
        mock_factory.create.return_value = mock_model

        client = GeminiClient(mock_config)
        response = client.query("Hello")

        assert response == "Test response"
        mock_model.generate_content.assert_called_once_with("Hello")

    @patch("lib.rag.llm.GeminiClientFactory")
    def test_query_with_context(self, mock_factory, mock_config):
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "FastAPI"
        mock_model.generate_content.return_value = mock_response
        mock_factory.create.return_value = mock_model

        client = GeminiClient(mock_config)
        response = client.query_with_context(
            "What framework?",
            "This repo uses FastAPI."
        )
        assert "FastAPI" in response


class TestQueryWithRag:
    @patch("lib.rag.llm.GeminiClient")
    def test_query_with_rag(self, mock_client_class):
        mock_client = MagicMock()
        mock_client.query_with_context.return_value = "It uses FastAPI."
        mock_client_class.return_value = mock_client

        response = query_with_rag(
            "What framework?",
            "This is a FastAPI project."
        )
        assert "FastAPI" in response