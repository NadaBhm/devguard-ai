"""Tests for Stack Detection Scanner."""
from pathlib import Path
from unittest.mock import patch

import pytest

from codesec.scanners.stack_detection import (
    detect_stack,
    get_language_breakdown,
)
from codesec.models import StackDetection, ContainerInfo
from codesec.scanners import ScannerError, is_dockerfile_rel_path


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

    def test_detect_bare_static_site(self, tmp_path: Path):
        """Regression: a JS-less static site (only .html/.css) reported
        primary_language='unknown' because those extensions were never counted,
        so it never reached S3. Now html wins and the S3 decision can fire."""
        repo = tmp_path / "static_repo"
        repo.mkdir()
        (repo / "index.html").write_text("<html><body>Hello</body></html>")
        (repo / "style.css").write_text("body { color: red; }")

        result = detect_stack(repo)
        assert result.primary_language == "html"
        assert result.container.detected is False
        assert result.database is None
        assert result.confidence >= 0.5

    def test_detect_mixed_html_static_site(self, tmp_path: Path):
        """HTML must beat a small JS/TS footprint for a static site."""
        repo = tmp_path / "static_repo_js"
        repo.mkdir()
        (repo / "index.html").write_text(
            "<html><body>" + "x" * 500 + "</body></html>"
        )
        (repo / "style.css").write_text("body { color: red; }")
        (repo / "app.js").write_text("document.title = 'hi';")

        result = detect_stack(repo)
        assert result.primary_language == "html"

    def test_prose_mentions_do_not_false_positive_static_site(
        self, tmp_path: Path
    ):
        """Regression: a static homepage whose README/HTML *mentions* tech
        (\"built with Express + Prisma, FastAPI, Postgres\") must NOT be
        detected as using those frameworks or a database, otherwise it is
        misrouted away from S3."""
        repo = tmp_path / "static_prose_repo"
        repo.mkdir()
        (repo / "index.html").write_text(
            "<html><body>2023 backend — express + prisma, fastapi, postgres</body></html>"
        )
        (repo / "README.md").write_text(
            "Portfolio. Built with flask, sqlalchemy, mongodb, redis, next.js.\n"
        )
        (repo / "style.css").write_text("body { color: red; }")

        result = detect_stack(repo)
        assert result.primary_language == "html"
        assert result.frameworks == []
        assert result.database is None

    def test_binary_assets_do_not_false_positive_database(
        self, tmp_path: Path
    ):
        """Regression: binary assets (e.g. .woff2 fonts) read as text with
        errors=ignore can contain the substring \"pg\" at random bytes and
        false-positive the postgresql database detection."""
        repo = tmp_path / "static_binary_repo"
        repo.mkdir()
        (repo / "index.html").write_text("<html><body>Hello</body></html>")
        fonts_dir = repo / "fonts"
        fonts_dir.mkdir()
        (fonts_dir / "webfont.woff2").write_bytes(b"\x00\x01pg\x00\xff\xfe")
        (repo / "README.md").write_text("Portfolio site.")

        result = detect_stack(repo)
        assert result.database is None
        assert result.frameworks == []

    def test_lockfile_hashes_do_not_false_positive_database(
        self, tmp_path: Path
    ):
        """Base64 integrity hashes containing "pg" must not flag postgres."""
        repo = tmp_path / "static_lockfile_repo"
        repo.mkdir()
        (repo / "index.html").write_text("<html><body>Hello</body></html>")
        (repo / "package.json").write_text(
            '{"name":"static","devDependencies":{"webpack":"^5.0.0"}}'
        )
        (repo / "package-lock.json").write_text(
            '{"packages":{"":{}},"node_modules/foo":{"integrity":"sha512-dgweJhWWpgKYrQaMNH+f0pbo"}}'
        )

        result = detect_stack(repo)
        assert result.database is None
        assert result.frameworks == []

    def test_postgres_still_detected_without_bare_pg(self, tmp_path: Path):
        """'postgres' detects; bare 'pg' does not (hash regression)."""
        repo = tmp_path / "node_pg_repo"
        repo.mkdir()
        (repo / "package.json").write_text(
            '{"name":"api","dependencies":{"postgres":"^3.0.0","express":"^4.0.0"}}'
        )
        (repo / "index.js").write_text(
            'const postgres = require("postgres"); module.exports = () => postgres();'
        )
        assert detect_stack(repo).database == "postgresql"

        repo2 = tmp_path / "node_pg_bare_repo"
        repo2.mkdir()
        (repo2 / "package.json").write_text('{"name":"api","dependencies":{"pg":"^8.0.0"}}')
        (repo2 / "index.js").write_text('const { Pool } = require("pg");')
        assert detect_stack(repo2).database is None

    def _write_multi_container_repo(
        self, tmp_path: Path, compose_yml: str
    ) -> None:
        """Seed a repo with two service Dockerfiles + a compose file."""
        (tmp_path / "docker-compose.yml").write_text(compose_yml)
        for svc, port in (("api", "8000"), ("worker", "8001")):
            d = tmp_path / svc
            d.mkdir(exist_ok=True)
            (d / "Dockerfile").write_text(f"FROM node:20-alpine\nEXPOSE {port}\n")
            (d / "package.json").write_text('{"name": "' + svc + '"}\n')

    def test_compose_host_ports_pick_primary(self, tmp_path: Path):
        """The ALB-primary container should be the compose service exposing a
        host port, not whatever sorts first on disk."""
        self._write_multi_container_repo(
            tmp_path,
            "services:\n"
            "  worker:\n"
            "    build: ./worker\n"
            "    ports:\n      - \"8001:8001\"\n"
            "  api:\n"
            "    build: ./api\n"
            "    ports:\n      - \"8000:8000\"\n",
        )
        result = detect_stack(tmp_path)
        # api binds the conventional web port 8000 -> primary
        assert result.container.dockerfile_path == "api/Dockerfile"
        assert [c.dockerfile_path for c in result.containers][0] == "api/Dockerfile"

    def test_compose_entrypoint_name_breaks_port_tie(self, tmp_path: Path):
        """When several services publish host ports, a gateway/entrypoint name
        (with a less common port) should still win."""
        self._write_multi_container_repo(
            tmp_path,
            "services:\n"
            "  checkout:\n"
            "    build: ./worker\n"
            "    ports:\n      - \"8001:8001\"\n"
            "  gateway:\n"
            "    build: ./api\n"
            "    ports:\n      - \"9000:9000\"\n",
        )
        result = detect_stack(tmp_path)
        assert result.container.dockerfile_path == "api/Dockerfile"

    def test_compose_absent_keeps_scan_order(self, tmp_path: Path):
        """Without a compose file (or with no host ports), primary falls back
        to scan order."""
        self._write_multi_container_repo(
            tmp_path,
            "services:\n"
            "  api:\n"
            "    build: ./api\n"
            "    expose:\n      - \"8000\"\n"
            "  worker:\n"
            "    build: ./worker\n",
        )
        result = detect_stack(tmp_path)
        assert result.container.dockerfile_path == "api/Dockerfile"

    def test_compose_build_object_context(self, tmp_path: Path):
        """Compose ``build: {context, dockerfile}`` form maps to the nested
        Dockerfile path correctly."""
        (tmp_path / "docker-compose.yml").write_text(
            "services:\n"
            "  web:\n"
            "    build:\n"
            "      context: .\n"
            "      dockerfile: services/web/Dockerfile\n"
            "    ports:\n      - \"80:80\"\n"
            "  api:\n"
            "    build:\n"
            "      context: .\n"
            "      dockerfile: services/api/Dockerfile\n"
        )
        for svc, port in (("web", "80"), ("api", "8080")):
            p = tmp_path / "services" / svc
            p.mkdir(parents=True, exist_ok=True)
            (p / "Dockerfile").write_text(f"FROM nginx:alpine\nEXPOSE {port}\n")
        result = detect_stack(tmp_path)
        assert result.container.dockerfile_path == "services/web/Dockerfile"

    def test_dockerfile_predicate_rejects_templates(self):
        """Files that merely contain the word 'dockerfile' (templates, docs)
        must not be treated as containers."""
        assert is_dockerfile_rel_path("Dockerfile")
        assert is_dockerfile_rel_path("dockerfile")
        assert is_dockerfile_rel_path("Dockerfile.prod")
        assert is_dockerfile_rel_path("app.Dockerfile")
        assert is_dockerfile_rel_path("services/api/Dockerfile")
        assert not is_dockerfile_rel_path("resources/templates/nginx.dockerfile.twig")
        assert not is_dockerfile_rel_path("Dockerfile.example.txt")
        assert not is_dockerfile_rel_path("docs/DOCKERFILE_GUIDE.md")

    def test_template_dockerfile_false_positives_not_detected(self, tmp_path: Path):
        """End-to-end: a repo whose only 'dockerfile' names are Twig templates
        yields no container, not a bogus one."""
        (tmp_path / "resources" / "templates").mkdir(parents=True)
        (tmp_path / "resources" / "templates" / "nginx.dockerfile.twig").write_text(
            "FROM nginx:alpine\nEXPOSE 80\n"
        )
        result = detect_stack(tmp_path)
        assert result.containers == []
        assert result.container.detected is False

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