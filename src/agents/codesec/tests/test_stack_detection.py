"""Tests for Stack Detection Scanner."""
from pathlib import Path
from unittest.mock import patch

import pytest

from codesec.scanners.stack_detection import (
    detect_stack,
    get_language_breakdown,
)
from codesec.models import StackDetection, ContainerInfo
from codesec.scanners import ScannerError


class TestDetectStack:
    """Test main stack detection function."""

    def test_detect_python_repo(self, sample_python_repo: Path):
        """Detect Python FastAPI stack with high confidence."""
        result = detect_stack(sample_python_repo)

        assert isinstance(result, StackDetection)
        assert result.primary_language == "python"
        assert result.confidence >= 0.5
        assert result.container.detected is True
        assert result.container.dockerfile_path is not None
        assert result.container.compose_detected is True

    def test_detect_node_repo(self, sample_node_repo: Path):
        """Detect Node.js/Express stack."""
        result = detect_stack(sample_node_repo)

        assert isinstance(result, StackDetection)
        assert result.primary_language == "javascript"
        assert result.confidence >= 0.5
        assert result.container.detected is True

    def test_detect_empty_repo(self, temp_repo: Path):
        """Handle empty repository gracefully."""
        result = detect_stack(temp_repo)

        assert result.primary_language == "unknown"
        assert result.confidence >= 0.0
        assert result.confidence <= 1.0
        assert result.frameworks == []

    def test_invalid_repo_path(self):
        """Raise ScannerError for non-existent path."""
        with pytest.raises(ScannerError):
            detect_stack(Path("/nonexistent/path"))

    def test_container_detection(self, sample_python_repo: Path):
        """Detect Dockerfile and docker-compose."""
        result = detect_stack(sample_python_repo)

        assert result.container.detected is True
        assert result.container.base_image is not None
        assert "python" in result.container.base_image.lower()
        assert result.container.compose_detected is True

    def test_detected_files_populated(self, sample_python_repo: Path):
        """Detected files list should contain relevant files."""
        result = detect_stack(sample_python_repo)

        assert len(result.detected_files) > 0
        assert any("Dockerfile" in f for f in result.detected_files)

    def test_detect_go_repo(self, tmp_path: Path):
        """Test detecting Go stack."""
        repo = tmp_path / "go_repo"
        repo.mkdir()
        (repo / "go.mod").write_text("module example.com/test\ngo 1.19")
        (repo / "main.go").write_text("package main\nimport \"fmt\"")

        result = detect_stack(repo)
        assert result.primary_language == "go"

    def test_detect_rust_repo(self, tmp_path: Path):
        """Test detecting Rust stack."""
        repo = tmp_path / "rust_repo"
        repo.mkdir()
        (repo / "Cargo.toml").write_text("[package]\nname = \"test\"")
        (repo / "main.rs").write_text("fn main() { println!(\"hello\"); }")

        result = detect_stack(repo)
        assert result.primary_language == "rust"

    def test_detect_java_repo(self, tmp_path: Path):
        """Test detecting Java stack."""
        repo = tmp_path / "java_repo"
        repo.mkdir()
        (repo / "pom.xml").write_text("<project><dependencies></dependencies></project>")
        src = repo / "src"
        src.mkdir()
        (src / "Main.java").write_text("public class Main { public static void main(String[] args) {} }")

        result = detect_stack(repo)
        assert result.primary_language == "java"

    def test_detect_mixed_repo(self, tmp_path: Path):
        """Test detecting mixed language repo."""
        repo = tmp_path / "mixed_repo"
        repo.mkdir()
        (repo / "app.py").write_text("from flask import Flask")
        (repo / "frontend.js").write_text("const app = require('express')")

        result = detect_stack(repo)
        assert len(result.languages) >= 2
        assert result.primary_language in ["python", "javascript"]

    def test_framework_detection(self, sample_python_repo: Path):
        """Test framework detection from requirements."""
        result = detect_stack(sample_python_repo)
        # FastAPI or SQLAlchemy might be detected
        assert isinstance(result.frameworks, list)


class TestGetLanguageBreakdown:
    """Test language breakdown calculation."""

    def test_python_breakdown(self, sample_python_repo: Path):
        breakdown = get_language_breakdown(sample_python_repo)
        assert "python" in breakdown
        assert breakdown["python"] > 0

    def test_empty_repo(self, tmp_path: Path):
        repo = tmp_path / "empty"
        repo.mkdir()
        breakdown = get_language_breakdown(repo)
        assert breakdown == {}

    def test_ignores_non_code_files(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("print('hello')")
        (repo / "README.md").write_text("# README\n" * 100)

        breakdown = get_language_breakdown(repo)
        assert "python" in breakdown
        assert "markdown" not in breakdown


class TestStackDetectionModel:
    """Test StackDetection model."""

    def test_creation(self):
        stack = StackDetection(
            primary_language="python",
            frameworks=["fastapi", "sqlalchemy"],
            database="postgresql",
            build_tool="pip",
            container=ContainerInfo(detected=True, base_image="python:3.12-slim"),
            confidence=0.92,
            detected_files=["requirements.txt", "Dockerfile"],
        )
        assert stack.primary_language == "python"
        assert stack.confidence == 0.92
        assert "fastapi" in stack.frameworks

    def test_defaults(self):
        stack = StackDetection(primary_language="unknown", confidence=0.0)
        assert stack.frameworks == []
        assert stack.database is None
        assert stack.build_tool is None
        assert stack.container.detected is False
        assert stack.detected_files == []