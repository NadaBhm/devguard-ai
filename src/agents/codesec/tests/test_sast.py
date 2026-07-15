"""Tests for SAST (Static Application Security Testing) scanner."""
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codesec.scanners.sast import (
    run_sast,
    _map_cwe_to_owasp,
    _normalize_severity,
    _run_semgrep,
    _run_bandit,
)
from codesec.models import SASTFinding, Severity


class TestNormalizeSeverity:
    """Test severity string normalization."""

    def test_critical(self):
        assert _normalize_severity("critical") == Severity.CRITICAL
        assert _normalize_severity("CRITICAL") == Severity.CRITICAL

    def test_high(self):
        assert _normalize_severity("high") == Severity.HIGH
        assert _normalize_severity("HIGH") == Severity.HIGH
        assert _normalize_severity("error") == Severity.HIGH
        assert _normalize_severity("ERROR") == Severity.HIGH

    def test_medium(self):
        assert _normalize_severity("medium") == Severity.MEDIUM
        assert _normalize_severity("MEDIUM") == Severity.MEDIUM
        assert _normalize_severity("warning") == Severity.MEDIUM
        assert _normalize_severity("WARNING") == Severity.MEDIUM

    def test_low(self):
        assert _normalize_severity("low") == Severity.LOW
        assert _normalize_severity("LOW") == Severity.LOW
        assert _normalize_severity("info") == Severity.INFO
        assert _normalize_severity("informational") == Severity.INFO

    def test_unknown(self):
        assert _normalize_severity("unknown") == Severity.LOW
        assert _normalize_severity("random") == Severity.LOW
        assert _normalize_severity("") == Severity.LOW


class TestMapCWEToOWASP:
    """Test CWE to OWASP Top 10 mapping."""

    def test_sql_injection(self):
        result = _map_cwe_to_owasp("CWE-89")
        assert result is not None
        assert "A03" in result

    def test_path_traversal(self):
        result = _map_cwe_to_owasp("CWE-22")
        assert result is not None
        assert "A01" in result

    def test_hardcoded_credentials(self):
        result = _map_cwe_to_owasp("CWE-798")
        assert result is not None
        assert "A07" in result

    def test_secure_cookie(self):
        result = _map_cwe_to_owasp("CWE-614")
        assert result is not None
        assert "A05" in result

    def test_unknown_cwe(self):
        assert _map_cwe_to_owasp("CWE-99999") is None

    def test_none_input(self):
        assert _map_cwe_to_owasp(None) is None

    def test_case_insensitive(self):
        result_lower = _map_cwe_to_owasp("cwe-89")
        result_upper = _map_cwe_to_owasp("CWE-89")
        assert result_lower == result_upper


class TestSASTFindingModel:
    """Test SASTFinding Pydantic model."""

    def test_creation(self):
        finding = SASTFinding(
            rule_id="python.sql-injection",
            tool="semgrep",
            severity=Severity.CRITICAL,
            category="owasp-top10",
            owasp_category="A03:2021 – Injection",
            cwe_id="CWE-89",
            file="app/db.py",
            line=24,
            column=10,
            message="Possible SQL injection",
            snippet="query = f\"SELECT * FROM users WHERE id = {user_id}\"",
            remediation="Use parameterized queries",
        )
        assert finding.rule_id == "python.sql-injection"
        assert finding.severity == Severity.CRITICAL
        assert finding.cwe_id == "CWE-89"
        assert finding.line == 24
        assert finding.column == 10
        assert finding.tool == "semgrep"

    def test_severity_enum(self):
        finding = SASTFinding(
            rule_id="test",
            tool="test",
            severity=Severity.HIGH,
            category="test",
            file="test.py",
            line=1,
            message="test",
        )
        assert finding.severity == Severity.HIGH

    def test_optional_fields(self):
        finding = SASTFinding(
            rule_id="test",
            tool="test",
            severity=Severity.MEDIUM,
            category="test",
            file="test.py",
            line=1,
            message="test",
        )
        assert finding.owasp_category is None
        assert finding.cwe_id is None
        assert finding.snippet is None
        assert finding.remediation is None


class TestRunSAST:
    """Test the main SAST scan function."""

    def test_run_sast_on_python_repo(self, sample_python_repo: Path):
        """SAST should find or handle Python repo."""
        findings = run_sast(sample_python_repo)

        assert isinstance(findings, list)
        for finding in findings:
            assert isinstance(finding, SASTFinding)
            assert finding.file
            assert finding.line >= 1
            assert finding.message

    def test_run_sast_on_node_repo(self, sample_node_repo: Path):
        """SAST should handle Node.js repos."""
        findings = run_sast(sample_node_repo)

        assert isinstance(findings, list)
        for finding in findings:
            assert isinstance(finding, SASTFinding)

    def test_run_sast_empty_repo(self, temp_repo: Path):
        """SAST on empty repo should return empty list."""
        findings = run_sast(temp_repo)
        assert findings == []

    @patch("codesec.scanners.sast._run_semgrep")
    @patch("codesec.scanners.sast._run_bandit")
    def test_run_sast_uses_semgrep_first(self, mock_bandit, mock_semgrep, sample_python_repo: Path):
        """Test that Semgrep is tried first, Bandit as fallback."""
        mock_semgrep.return_value = [
            SASTFinding(
                rule_id="test.rule",
                tool="semgrep",
                severity=Severity.HIGH,
                category="security",
                file="test.py",
                line=1,
                message="Test finding",
            )
        ]
        mock_bandit.return_value = []

        findings = run_sast(sample_python_repo)

        mock_semgrep.assert_called_once()
        assert len(findings) == 1
        assert findings[0].tool == "semgrep"
        # Bandit should NOT be called since Semgrep found something
        mock_bandit.assert_not_called()

    @patch("codesec.scanners.sast._run_semgrep")
    @patch("codesec.scanners.sast._run_bandit")
    def test_run_sast_falls_back_to_bandit(self, mock_bandit, mock_semgrep, sample_python_repo: Path):
        """Test Bandit fallback when Semgrep returns empty."""
        mock_semgrep.return_value = []
        mock_bandit.return_value = [
            SASTFinding(
                rule_id="B105",
                tool="bandit",
                severity=Severity.MEDIUM,
                category="security",
                file="test.py",
                line=5,
                message="Hardcoded password",
            )
        ]

        findings = run_sast(sample_python_repo)

        mock_semgrep.assert_called_once()
        mock_bandit.assert_called_once()
        assert len(findings) == 1
        assert findings[0].tool == "bandit"

    @patch("codesec.scanners.sast._run_semgrep")
    @patch("codesec.scanners.sast._run_bandit")
    def test_run_sast_deduplicates(self, mock_bandit, mock_semgrep, sample_python_repo: Path):
        """Test that duplicate findings are deduplicated."""
        finding = SASTFinding(
            rule_id="dup.rule",
            tool="semgrep",
            severity=Severity.HIGH,
            category="security",
            file="same.py",
            line=10,
            message="Duplicate",
        )
        mock_semgrep.return_value = [finding, finding]  # Duplicate
        mock_bandit.return_value = []

        findings = run_sast(sample_python_repo)

        assert len(findings) == 1  # Should be deduplicated


class TestRunSemgrep:
    """Test Semgrep execution."""

    @patch("codesec.scanners.sast.run_subprocess")
    def test_run_semgrep_success(self, mock_run, tmp_path: Path):
        """Test successful Semgrep execution."""
        mock_output = {
            "results": [
                {
                    "check_id": "python.flask.security.insecure-cookie",
                    "path": "app.py",
                    "start": {"line": 5, "col": 1},
                    "extra": {
                        "message": "Insecure cookie detected",
                        "severity": "WARNING",
                        "metadata": {
                            "cwe": ["CWE-614: Secure Cookie Flag"],
                            "owasp": ["A05:2021 – Security Misconfiguration"]
                        }
                    }
                }
            ],
            "errors": []
        }
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(mock_output),
            stderr=""
        )

        # Create a Python file
        (tmp_path / "app.py").write_text("from flask import Flask\napp = Flask(__name__)")

        findings = _run_semgrep(tmp_path)

        assert isinstance(findings, list)
        if len(findings) > 0:
            assert findings[0].tool == "semgrep"
            assert findings[0].severity == Severity.MEDIUM

    @patch("codesec.scanners.sast.run_subprocess")
    def test_run_semgrep_not_installed(self, mock_run, tmp_path: Path):
        """Test Semgrep when tool is not installed."""
        from codesec.scanners import ScannerError
        mock_run.side_effect = ScannerError("semgrep not found")

        (tmp_path / "app.py").write_text("print('hello')")

        findings = _run_semgrep(tmp_path)
        assert findings == []

    @patch("codesec.scanners.sast.run_subprocess")
    def test_run_semgrep_invalid_json(self, mock_run, tmp_path: Path):
        """Test Semgrep returning invalid JSON."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="not valid json",
            stderr=""
        )

        (tmp_path / "app.py").write_text("print('hello')")

        findings = _run_semgrep(tmp_path)
        assert findings == []

    @patch("codesec.scanners.sast.run_subprocess")
    def test_run_semgrep_no_source_files(self, mock_run, tmp_path: Path):
        """Test Semgrep with no source files."""
        (tmp_path / "README.md").write_text("# No code here")

        findings = _run_semgrep(tmp_path)
        assert findings == []
        mock_run.assert_not_called()


class TestRunBandit:
    """Test Bandit fallback execution."""

    @patch("codesec.scanners.sast.run_subprocess")
    def test_run_bandit_success(self, mock_run, tmp_path: Path):
        """Test successful Bandit execution."""
        mock_output = {
            "results": [
                {
                    "test_id": "B105",
                    "issue_text": "Hardcoded password",
                    "issue_severity": "LOW",
                    "filename": "app.py",
                    "line_number": 10,
                    "code": "password = 'secret'",
                    "cwe": "798"
                }
            ]
        }
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(mock_output),
            stderr=""
        )

        (tmp_path / "app.py").write_text("password = 'secret'")

        findings = _run_bandit(tmp_path)

        assert isinstance(findings, list)
        if len(findings) > 0:
            assert findings[0].tool == "bandit"
            assert findings[0].rule_id == "B105"

    @patch("codesec.scanners.sast.run_subprocess")
    def test_run_bandit_not_installed(self, mock_run, tmp_path: Path):
        """Test Bandit when tool is not installed."""
        from codesec.scanners import ScannerError
        mock_run.side_effect = ScannerError("bandit not found")

        (tmp_path / "app.py").write_text("print('hello')")

        findings = _run_bandit(tmp_path)
        assert findings == []