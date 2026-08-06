"""Tests for RAG configuration."""
import pytest

from lib.rag.config import RAGConfig, get_rag_config


class TestRAGConfig:
    """Test configuration defaults and overrides."""

    def test_default_values(self):
        config = RAGConfig()
        assert config.qdrant_url == "http://localhost:6333"
        assert config.qdrant_collection == "devguard_repos"
        assert config.hf_model == "BAAI/bge-base-en-v1.5"
        assert config.embedding_dim == 768
        assert config.gemini_model == "gemini-1.5-flash"
        assert config.chunk_size == 1000
        assert config.chunk_overlap == 200

    def test_custom_values(self):
        config = RAGConfig(
            qdrant_url="http://custom:6333",
            hf_model="custom-model",
            embedding_dim=512,
            chunk_size=500,
        )
        assert config.qdrant_url == "http://custom:6333"
        assert config.hf_model == "custom-model"
        assert config.embedding_dim == 512
        assert config.chunk_size == 500

    def test_frozen_dataclass(self):
        """Config should be immutable."""
        config = RAGConfig()
        with pytest.raises(AttributeError):
            config.qdrant_url = "new-url"  # type: ignore[reportAttributeAccessIssue]


class TestGetRAGConfig:
    """Test factory function."""

    def test_returns_rag_config(self):
        config = get_rag_config()
        assert isinstance(config, RAGConfig)

    def test_env_override(self, monkeypatch):
        """get_rag_config should read env vars dynamically."""
        monkeypatch.setenv("QDRANT_URL", "http://env-host:6333")
        monkeypatch.setenv("HF_MODEL", "env-model")
        monkeypatch.setenv("EMBEDDING_DIM", "384")

        config = get_rag_config()
        assert config.qdrant_url == "http://env-host:6333"
        assert config.hf_model == "env-model"
        assert config.embedding_dim == 384