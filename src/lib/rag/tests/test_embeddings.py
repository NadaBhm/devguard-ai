"""Tests for Hugging Face embedding client."""
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from lib.rag.embeddings import EmbeddingClient, get_embedding
from lib.rag.config import RAGConfig


class TestEmbeddingClient:
    """Test embedding generation."""

    def setup_method(self):
        """Clear model cache before each test to avoid cross-test pollution."""
        EmbeddingClient._model_cache.clear()

    @patch("lib.rag.embeddings.SentenceTransformer")
    def test_model_caching(self, mock_st_class):
        """Same model should not be loaded twice."""
        mock_model = MagicMock()
        mock_model.encode.return_value = np.array([[0.1] * 768])
        mock_st_class.return_value = mock_model

        config = RAGConfig(hf_model="BAAI/bge-base-en-v1.5")
        client1 = EmbeddingClient(config)
        client2 = EmbeddingClient(config)

        # Should reuse cached model
        mock_st_class.assert_called_once()
        assert client1.model is client2.model

    @patch("lib.rag.embeddings.SentenceTransformer")
    def test_bge_query_prefix_added(self, mock_st_class):
        """BGE queries need instruction prefix."""
        mock_model = MagicMock()
        mock_model.encode.return_value = np.array([[0.1] * 768])
        mock_st_class.return_value = mock_model

        config = RAGConfig(hf_model="BAAI/bge-base-en-v1.5")
        client = EmbeddingClient(config)
        client.embed(["hello world"], is_query=True)

        call_args = mock_model.encode.call_args
        texts = call_args[0][0]
        assert all(
            t.startswith("Represent this sentence for searching relevant passages: ")
            for t in texts
        )

    @patch("lib.rag.embeddings.SentenceTransformer")
    def test_bge_document_no_prefix(self, mock_st_class):
        """BGE documents should NOT get prefix — only queries do."""
        mock_model = MagicMock()
        mock_model.encode.return_value = np.array([[0.1] * 768])
        mock_st_class.return_value = mock_model

        config = RAGConfig(hf_model="BAAI/bge-base-en-v1.5")
        client = EmbeddingClient(config)
        client.embed(["hello world"], is_query=False)

        call_args = mock_model.encode.call_args
        texts = call_args[0][0]
        assert not any(t.startswith("Represent") for t in texts)
        # Raw text preserved
        assert texts[0] == "hello world"

    @patch("lib.rag.embeddings.SentenceTransformer")
    def test_non_bge_no_prefix(self, mock_st_class):
        """Non-BGE models should not get prefix."""
        mock_model = MagicMock()
        mock_model.encode.return_value = np.array([[0.1] * 768])
        mock_st_class.return_value = mock_model

        config = RAGConfig(hf_model="sentence-transformers/all-MiniLM-L6-v2")
        client = EmbeddingClient(config)
        client.embed(["hello world"], is_query=True)

        call_args = mock_model.encode.call_args
        texts = call_args[0][0]
        assert not any(t.startswith("Represent") for t in texts)

    @patch("lib.rag.embeddings.SentenceTransformer")
    def test_embed_empty_list(self, mock_st_class):
        """Empty list should return empty list."""
        mock_model = MagicMock()
        mock_st_class.return_value = mock_model

        client = EmbeddingClient()
        assert client.embed([]) == []

    @patch("lib.rag.embeddings.SentenceTransformer")
    def test_embed_single(self, mock_st_class):
        """embed_single should return a flat list."""
        mock_model = MagicMock()
        mock_model.encode.return_value = np.array([[0.1] * 768])
        mock_st_class.return_value = mock_model

        client = EmbeddingClient()
        result = client.embed_single("test")

        assert isinstance(result, list)
        assert len(result) == 768
        assert isinstance(result[0], float)

    @patch("lib.rag.embeddings.SentenceTransformer")
    def test_embed_error_handling(self, mock_st_class):
        """Errors should be raised, not swallowed."""
        mock_model = MagicMock()
        mock_model.encode.side_effect = RuntimeError("CUDA out of memory")
        mock_st_class.return_value = mock_model

        client = EmbeddingClient()
        with pytest.raises(RuntimeError, match="CUDA out of memory"):
            client.embed(["test"])


class TestGetEmbedding:
    """Test convenience function."""

    def setup_method(self):
        EmbeddingClient._model_cache.clear()

    @patch("lib.rag.embeddings.SentenceTransformer")
    def test_get_embedding(self, mock_st_class):
        mock_model = MagicMock()
        mock_model.encode.return_value = np.array([[0.1] * 768])
        mock_st_class.return_value = mock_model

        result = get_embedding("hello")
        assert len(result) == 768