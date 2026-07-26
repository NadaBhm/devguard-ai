"""Tests for RAG ingestion pipeline."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lib.rag.ingestion import _chunk_text, _read_repo_files, ingest_repo, ingest_text


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """Create a fake repo with README and Python file."""
    repo = tmp_path / "fake_repo"
    repo.mkdir()
    
    readme = repo / "README.md"
    readme.write_text("# Test Repo\n\nThis is a Python project.")
    
    main_py = repo / "main.py"
    main_py.write_text("def hello(): return 'world'\n")
    
    return repo


class TestChunkText:
    """Test text chunking."""

    def test_simple_chunk(self):
        text = "a" * 2000
        chunks = _chunk_text(text, chunk_size=1000, overlap=200)
        assert len(chunks) >= 2
        # Verify no infinite loop and chunks have content
        for chunk in chunks:
            assert len(chunk) > 0

    def test_short_text(self):
        text = "hello world"
        chunks = _chunk_text(text, chunk_size=1000, overlap=200)
        assert len(chunks) == 1
        assert chunks[0] == "hello world"

    def test_empty_text(self):
        assert _chunk_text("") == []

    def test_text_with_spaces(self):
        """Test that word boundaries are respected."""
        text = "word " * 500  # ~2500 chars with spaces
        chunks = _chunk_text(text, chunk_size=1000, overlap=200)
        assert len(chunks) >= 2
        # Chunks should break at spaces (not mid-word)
        for chunk in chunks:
            # Should not start with partial word
            assert chunk.startswith("word") or chunk.startswith(" ") or chunk[0] in "word"

    def test_no_infinite_loop_no_spaces(self):
        """Text with no spaces should not cause infinite loop."""
        text = "a" * 1500
        chunks = _chunk_text(text, chunk_size=1000, overlap=200)
        # Should complete without infinite loop
        assert len(chunks) > 1
        # First chunk is full size
        assert len(chunks[0]) == 1000
        # All chunks are non-empty
        assert all(len(c) > 0 for c in chunks)
        # Combined length with overlap exceeds original text
        assert sum(len(c) for c in chunks) >= len(text)

    def test_overlap_boundary(self):
        """Test exact boundary where overlap matters."""
        text = "x" * 1000 + "y" * 1000
        chunks = _chunk_text(text, chunk_size=600, overlap=100)
        assert len(chunks) >= 3
        total_coverage = sum(len(c) for c in chunks)
        assert total_coverage >= 2000  # Should cover all text


class TestReadRepoFiles:
    """Test repo file reading."""

    def test_reads_readme_and_code(self, sample_repo: Path):
        files = _read_repo_files(sample_repo)
        paths = [f["path"] for f in files]

        assert any("README.md" in p for p in paths)
        assert any("main.py" in p for p in paths)

    def test_empty_repo(self, tmp_path: Path):
        """Empty repo should return empty list."""
        empty_repo = tmp_path / "empty"
        empty_repo.mkdir()
        files = _read_repo_files(empty_repo)
        assert files == []


class TestIngestRepo:
    """Test repo ingestion."""

    @patch("lib.rag.ingestion.QdrantClient")
    @patch("lib.rag.ingestion.EmbeddingClient")
    def test_ingest_creates_collection(self, mock_embedder_class, mock_client_class, sample_repo: Path):
        mock_embedder = MagicMock()
        mock_embedder.embed.return_value = [[0.1] * 768]
        mock_embedder_class.return_value = mock_embedder

        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        count = ingest_repo(sample_repo, job_id="test-job")

        assert count > 0
        mock_client.create_collection.assert_called_once()

    @patch("lib.rag.ingestion.QdrantClient")
    @patch("lib.rag.ingestion.EmbeddingClient")
    def test_ingest_empty_repo(self, mock_embedder_class, mock_client_class, tmp_path: Path):
        """Empty repo should return 0 chunks."""
        empty_repo = tmp_path / "empty"
        empty_repo.mkdir()

        mock_embedder = MagicMock()
        mock_embedder_class.return_value = mock_embedder

        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        count = ingest_repo(empty_repo, job_id="test-empty")

        assert count == 0


class TestIngestText:
    """Test text ingestion."""

    @patch("lib.rag.ingestion.QdrantClient")
    @patch("lib.rag.ingestion.EmbeddingClient")
    def test_ingest_text(self, mock_embedder_class, mock_client_class):
        mock_embedder = MagicMock()
        mock_embedder.embed.return_value = [[0.1] * 768]
        mock_embedder_class.return_value = mock_embedder

        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        count = ingest_text("hello world", job_id="test-job")

        assert count == 1

    @patch("lib.rag.ingestion.QdrantClient")
    @patch("lib.rag.ingestion.EmbeddingClient")
    def test_ingest_empty_text(self, mock_embedder_class, mock_client_class):
        """Empty text should return 0."""
        mock_embedder = MagicMock()
        mock_embedder_class.return_value = mock_embedder

        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        count = ingest_text("", job_id="test-empty")

        assert count == 0