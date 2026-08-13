"""Tests for core.repo_ingestor (whole-repo digest for Gate-2 regeneration).

core.llm_provider.call_llm is always monkeypatched here — no test reaches
OpenRouter for real. The focus is:

  - file selection: text/config files are read, vendor/binary/lock/minified
    content is never touched
  - chunking: whole files stay together, only oversized files are split
  - the fail-soft contract: any failure returns ``None``, never an error
  - caching: the digest is repo-only, so it is cached per ``commit_sha``
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from core.repo_ingestor import (
    _DIGEST_HEADER,
    chunk_files,
    clear_digest_cache,
    ingest_repo,
    read_all_text_files,
)


def _make_repo(tmp_path: Path) -> Path:
    """A small, realistic repo: code, docs, config, and decoys to filter."""
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "main.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/health')\n"
        "def h():\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )
    (repo / "app" / "utils.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "README.md").write_text("# Demo\n\nFastAPI + Postgres service.\n", encoding="utf-8")
    (repo / "Dockerfile").write_text("FROM python:3.12-slim\nCOPY . /app\n", encoding="utf-8")
    (repo / ".env.example").write_text("DATABASE_URL=postgresql://db\n", encoding="utf-8")
    (repo / "requirements.txt").write_text("fastapi==0.110.0\nsqlalchemy\n", encoding="utf-8")

    # Decoys that must be filtered out:
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "junk.js").write_text("console.log(1)", encoding="utf-8")
    (repo / "vendor").mkdir()
    (repo / "vendor" / "dep.go").write_text("package dep", encoding="utf-8")
    (repo / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (repo / "yarn.lock").write_text("lock", encoding="utf-8")
    (repo / "app" / "min.js").write_text("var a=1", encoding="utf-8")
    (repo / "app" / "data.py").write_bytes(b"\x00\x01\x02binary\x00")
    return repo


class TestReadAllTextFiles:
    def test_reads_code_docs_and_config(self, tmp_path) -> None:
        repo = _make_repo(tmp_path)
        paths = [f["path"] for f in read_all_text_files(repo)]

        assert "app/main.py" in paths
        assert "app/utils.py" in paths
        assert "README.md" in paths
        assert "Dockerfile" in paths
        assert ".env.example" in paths
        assert "requirements.txt" in paths

    def test_filters_vendor_binary_lock_and_minified(self, tmp_path) -> None:
        repo = _make_repo(tmp_path)
        paths = [f["path"] for f in read_all_text_files(repo)]

        assert "node_modules/junk.js" not in paths
        assert "vendor/dep.go" not in paths
        assert "package-lock.json" not in paths
        assert "yarn.lock" not in paths
        assert "app/min.js" not in paths
        assert "app/data.py" not in paths

    def test_reads_are_sorted_for_determinism(self, tmp_path) -> None:
        repo = _make_repo(tmp_path)
        paths = [f["path"] for f in read_all_text_files(repo)]
        assert paths == sorted(paths)

    def test_empty_dir_yields_no_files(self, tmp_path) -> None:
        assert read_all_text_files(tmp_path / "empty") == []


class TestChunkFiles:
    def test_small_files_stay_together(self, tmp_path) -> None:
        files = [
            {"path": "a.py", "content": "x" * 100},
            {"path": "b.py", "content": "y" * 100},
        ]
        chunks = chunk_files(files, chunk_bytes=10_000)
        assert len(chunks) == 1
        assert "=== a.py ===" in chunks[0]
        assert "=== b.py ===" in chunks[0]

    def test_large_chunk_is_flushed(self, tmp_path) -> None:
        files = [
            {"path": "a.py", "content": "x" * 200},
            {"path": "b.py", "content": "y" * 200},
        ]
        chunks = chunk_files(files, chunk_bytes=300)
        assert len(chunks) == 2

    def test_oversized_file_is_split_into_parts(self, tmp_path) -> None:
        files = [{"path": "big.py", "content": "x" * 500}]
        chunks = chunk_files(files, chunk_bytes=200)
        assert len(chunks) == 3
        assert "=== big.py (partie 1) ===" in chunks[0]
        assert "=== big.py (partie 2) ===" in chunks[1]
        # every part survives intact
        assert "".join(c.rsplit("===\n", 1)[-1] for c in chunks) == "x" * 500


def _patch_call_llm(monkeypatch: pytest.MonkeyPatch, return_value) -> None:
    monkeypatch.setattr("core.repo_ingestor.call_llm", lambda *a, **k: return_value)


class TestIngestRepo:
    @pytest.fixture(autouse=True)
    def _fresh_cache(self):
        clear_digest_cache()
        yield
        clear_digest_cache()

    def test_merges_facts_from_all_chunks(self, monkeypatch, tmp_path) -> None:
        repo = _make_repo(tmp_path)
        _patch_call_llm(
            monkeypatch,
            json.dumps({"facts": ["port 8000", "uses Postgres"]}),
        )

        digest = ingest_repo(repo, "job-1", commit_sha="abc123")

        assert digest is not None
        assert _DIGEST_HEADER in digest
        assert "- port 8000" in digest
        assert "- uses Postgres" in digest

    def test_fail_soft_on_none_reply(self, monkeypatch, tmp_path) -> None:
        repo = _make_repo(tmp_path)
        _patch_call_llm(monkeypatch, None)
        assert ingest_repo(repo, "job-1") is None

    def test_fail_soft_on_malformed_reply(self, monkeypatch, tmp_path) -> None:
        repo = _make_repo(tmp_path)
        _patch_call_llm(monkeypatch, "this is not json")
        assert ingest_repo(repo, "job-1") is None

    def test_empty_repo_returns_none(self, monkeypatch, tmp_path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        _patch_call_llm(monkeypatch, json.dumps({"facts": ["unused"]}))
        assert ingest_repo(empty, "job-1") is None

    def test_no_facts_returns_none(self, monkeypatch, tmp_path) -> None:
        repo = _make_repo(tmp_path)
        _patch_call_llm(monkeypatch, json.dumps({"facts": []}))
        assert ingest_repo(repo, "job-1") is None

    def test_no_api_key_returns_none(self, tmp_path) -> None:
        # conftest's autouse fixture deletes OPENROUTER_API_KEY, so the real
        # call_llm (unpatched) must fail-soft without a network round-trip.
        repo = _make_repo(tmp_path)
        assert ingest_repo(repo, "job-1") is None

    def test_digest_is_cached_per_commit(self, monkeypatch, tmp_path) -> None:
        repo = _make_repo(tmp_path)
        calls = {"n": 0}

        def fake_call_llm(*a, **k):
            calls["n"] += 1
            return json.dumps({"facts": ["fact"]})

        monkeypatch.setattr("core.repo_ingestor.call_llm", fake_call_llm)

        first = ingest_repo(repo, "job-1", commit_sha="sha-1")
        second = ingest_repo(repo, "job-1", commit_sha="sha-1")

        assert first == second
        assert calls["n"] == 1  # second call served from cache, no map pass

    def test_unknown_commit_is_never_cached(self, monkeypatch, tmp_path) -> None:
        repo = _make_repo(tmp_path)
        calls = {"n": 0}

        def fake_call_llm(*a, **k):
            calls["n"] += 1
            return json.dumps({"facts": ["fact"]})

        monkeypatch.setattr("core.repo_ingestor.call_llm", fake_call_llm)

        ingest_repo(repo, "job-1", commit_sha="unknown")
        ingest_repo(repo, "job-1", commit_sha="unknown")

        assert calls["n"] == 2
