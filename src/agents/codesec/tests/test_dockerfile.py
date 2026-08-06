"""Tests for Dockerfile Scanner."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codesec.scanners.dockerfile_scanner import (
    run_dockerfile_scan,
    _run_trivy_config,
    _run_hadolint,
    _run_builtin_checks,
    _get_remediation,
)
from codesec.models import DockerfileFinding, Severity


class TestGetRemediation:
    """Test remediation advice lookup."""

    def test_known_rule(self):
        remediation = _get_remediation("DS001")
        assert remediation is not None
        assert "non-root" in remediation.lower()

    def test_unknown_rule(self):
        assert _get_remediation("DS999") is None


class TestRunBuiltinChecks:
    """Test built-in Dockerfile security checks."""

    def test_detect_root_user(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "Dockerfile").write_text("FROM python:3.9\nUSER root\n")

        findings = _run_builtin_checks(repo)
        root_issues = [f for f in findings if "root" in f.message.lower()]
        assert len(root_issues) > 0
        assert root_issues[0].severity == Severity.HIGH

    def test_detect_latest_tag(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "Dockerfile").write_text("FROM python:latest\n")

        findings = _run_builtin_checks(repo)
        latest_issues = [f for f in findings if "latest" in f.message.lower()]
        assert len(latest_issues) > 0

    def test_detect_hardcoded_password(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "Dockerfile").write_text("FROM python:3.9\nENV PASSWORD=secret123\n")

        findings = _run_builtin_checks(repo)
        pw_issues = [f for f in findings if "password" in f.message.lower()]
        assert len(pw_issues) > 0
        assert pw_issues[0].severity == Severity.CRITICAL

    def test_detect_curl_pipe(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "Dockerfile").write_text('FROM python:3.9\nRUN curl http://example.com/install.sh | sh\n')

        findings = _run_builtin_checks(repo)
        curl_issues = [f for f in findings if "curl" in f.message.lower()]
        assert len(curl_issues) > 0

    def test_skip_comments(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "Dockerfile").write_text("FROM python:3.9\n# USER root\n")

        findings = _run_builtin_checks(repo)
        assert len(findings) == 0

    def test_no_dockerfile(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("print('hello')")

        findings = _run_builtin_checks(repo)
        assert findings == []

    def test_good_dockerfile(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "Dockerfile").write_text("""
FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
USER nobody
EXPOSE 5000
CMD ["python", "app.py"]
""")

        findings = _run_builtin_checks(repo)
        # Should have minimal or no issues
        assert len(findings) <= 1


class TestRunDockerfileScan:
    """Test the main Dockerfile scan function."""

    def test_run_dockerfile_scan_with_dockerfile(self, sample_python_repo: Path):
        """Scan repo that has a Dockerfile."""
        findings = run_dockerfile_scan(sample_python_repo)

        assert isinstance(findings, list)
        for finding in findings:
            assert isinstance(finding, DockerfileFinding)

    def test_run_dockerfile_scan_no_dockerfile(self, temp_repo: Path):
        """Scan repo with no Dockerfile."""
        findings = run_dockerfile_scan(temp_repo)
        assert findings == []

    @patch("codesec.scanners.dockerfile_scanner._run_trivy_config")
    @patch("codesec.scanners.dockerfile_scanner._run_hadolint")
    def test_run_dockerfile_scan_uses_trivy_first(self, mock_hadolint, mock_trivy, tmp_path: Path):
        mock_trivy.return_value = [
            DockerfileFinding(
                rule_id="DS001",
                tool="trivy",
                severity=Severity.HIGH,
                file="Dockerfile",
                line=1,
                message="Test issue",
            )
        ]
        mock_hadolint.return_value = []

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "Dockerfile").write_text("FROM python:latest\n")

        findings = run_dockerfile_scan(repo)

        mock_trivy.assert_called_once()
        assert len(findings) >= 1

    @patch("codesec.scanners.dockerfile_scanner._run_trivy_config")
    @patch("codesec.scanners.dockerfile_scanner._run_hadolint")
    def test_run_dockerfile_scan_deduplicates(self, mock_hadolint, mock_trivy, tmp_path: Path):
        finding = DockerfileFinding(
            rule_id="DS001",
            tool="trivy",
            severity=Severity.HIGH,
            file="Dockerfile",
            line=5,
            message="Same issue",
        )
        mock_trivy.return_value = [finding]
        mock_hadolint.return_value = [finding]  # Duplicate

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "Dockerfile").write_text("FROM python:3.9\n")

        findings = run_dockerfile_scan(repo)

        # Should deduplicate by (file, line, rule_id)
        assert len(findings) == 1


class TestDockerfileFindingModel:
    """Test DockerfileFinding model."""

    def test_creation(self):
        finding = DockerfileFinding(
            rule_id="DS001",
            tool="trivy",
            severity=Severity.HIGH,
            category="dockerfile",
            file="Dockerfile",
            line=5,
            message="Running as root",
            snippet="USER root",
            remediation="Add 'USER appuser'",
        )
        assert finding.rule_id == "DS001"
        assert finding.severity == Severity.HIGH
        assert finding.line == 5

    def test_defaults(self):
        finding = DockerfileFinding(
            rule_id="DS002",
            tool="builtin",
            severity=Severity.MEDIUM,
            file="Dockerfile",
            line=1,
            message="Test",
        )
        assert finding.category == "dockerfile"
        assert finding.snippet is None
        assert finding.remediation is None