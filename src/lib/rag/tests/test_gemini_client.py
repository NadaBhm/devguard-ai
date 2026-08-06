"""Tests for Gemini client factory."""
from unittest.mock import MagicMock, patch

import pytest

from lib.rag.gemini_client import GeminiClientFactory
from lib.rag.config import RAGConfig


class TestGeminiClientFactory:
    """Test factory pattern and caching."""

    def setup_method(self):
        """Reset factory state before each test."""
        GeminiClientFactory._configured_key = None
        GeminiClientFactory._models.clear()

    @patch("lib.rag.gemini_client.genai")
    def test_configure_called_once(self, mock_genai):
        """genai.configure should only be called when key changes."""
        config = RAGConfig(gemini_api_key="key-1", gemini_model="gemini-1.5-flash")
        
        GeminiClientFactory.create(config)
        GeminiClientFactory.create(config)  # Same key, should not re-configure
        
        mock_genai.configure.assert_called_once_with(api_key="key-1")

    @patch("lib.rag.gemini_client.genai")
    def test_reconfigure_on_key_change(self, mock_genai):
        """Should re-configure when API key changes."""
        config1 = RAGConfig(gemini_api_key="key-1", gemini_model="gemini-1.5-flash")
        config2 = RAGConfig(gemini_api_key="key-2", gemini_model="gemini-1.5-flash")
        
        GeminiClientFactory.create(config1)
        GeminiClientFactory.create(config2)
        
        assert mock_genai.configure.call_count == 2
        mock_genai.configure.assert_called_with(api_key="key-2")

    @patch("lib.rag.gemini_client.genai")
    def test_model_caching(self, mock_genai):
        """Same model name should return cached instance."""
        config = RAGConfig(gemini_api_key="key-1", gemini_model="gemini-1.5-flash")
        mock_model = MagicMock()
        mock_genai.GenerativeModel.return_value = mock_model
        
        m1 = GeminiClientFactory.create(config)
        m2 = GeminiClientFactory.create(config)
        
        assert m1 is m2
        mock_genai.GenerativeModel.assert_called_once()

    @patch("lib.rag.gemini_client.genai")
    def test_different_model_names(self, mock_genai):
        """Different model names should create different instances."""
        config1 = RAGConfig(gemini_api_key="key-1", gemini_model="gemini-1.5-flash")
        config2 = RAGConfig(gemini_api_key="key-1", gemini_model="gemini-1.5-pro")
        
        mock_flash = MagicMock()
        mock_pro = MagicMock()
        mock_genai.GenerativeModel.side_effect = [mock_flash, mock_pro]
        
        m1 = GeminiClientFactory.create(config1)
        m2 = GeminiClientFactory.create(config2)
        
        assert m1 is mock_flash
        assert m2 is mock_pro
        assert m1 is not m2

    @patch("lib.rag.gemini_client.genai")
    def test_no_key_no_configure(self, mock_genai):
        """Empty key should not call configure."""
        config = RAGConfig(gemini_api_key=None, gemini_model="gemini-1.5-flash")
        
        GeminiClientFactory.create(config)
        
        mock_genai.configure.assert_not_called()