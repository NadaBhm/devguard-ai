"""Tests for Secrets Scanner."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codesec.scanners.secrets import (
    run_secrets_scan,
    _run_regex_fallback,
    _mask_value,
    _is_test_value,
    SECRET_PATTERNS,
)
from codesec.models import Secret, Severity


class TestMaskValue:
    """Test secret value masking."""

    def test_short_value(self):
        assert _mask_value("abc") == "***"

    def test_long_value(self):
        masked = _mask_value("AKIAIOSFODNN7EXAMPLE")
        assert masked.startswith("AKIA")
        assert masked.endswith("MPLE")
        assert "..." in masked

    def test_six_char_value(self):
        assert _mask_value("123456") == "***"


class TestIsTestValue:
    """Test test value detection."""

    def test_test_keyword(self):
        assert _is_test_value("test_password_123") is True

    def test_example_keyword(self):
        assert _is_test_value("example_key") is True

    def test_changeme_keyword(self):
        assert _is_test_value("changeme") is True

    def test_dummy_keyword(self):
        assert _is_test_value("dummy_secret") is True

    def test_real_value(self):
        assert _is_test_value("AKIAIOSFODNN7EXAMPLE") is False


class TestSecretPatterns:
    """Test regex patterns for secret detection."""

    def test_aws_access_key_pattern(self):
        pattern = SECRET_PATTERNS["aws_access_key_id"]
        match = pattern.search("AKIAIOSFODNN7EXAMPLE")
        assert match is not None

    def test_aws_access_key_no_match(self):
        pattern = SECRET_PATTERNS["aws_access_key_id"]
        match = pattern.search("NOTANAWSKEY12345")
        assert match is None

    def test_private_key_pattern(self):
        pattern = SECRET_PATTERNS["private_key"]
        match = pattern.search("-----BEGIN RSA PRIVATE KEY-----")
        assert match is not None

    def test_generic_api_key_pattern(self):
        pattern = SECRET_PATTERNS["generic_api_key"]
        match = pattern.search('api_key = "sk_test_1234567890abcdef"')
        assert match is not None


class TestRunRegexFallback:
    """Test regex-based secret detection."""

    def test_detect_aws_key(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "config.py").write_text("AWS_ACCESS_KEY_ID = 'AKIAIOSFODNN7EXAMPLE'")

        findings = _run_regex_fallback(repo)
        aws_secrets = [s for s in findings if "aws" in s.type.lower()]
        assert len(aws_secrets) > 0

    def test_detect_private_key(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "key.pem").write_text("-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...")

        findings = _run_regex_fallback(repo)
        key_secrets = [s for s in findings if "private" in s.type.lower()]
        assert len(key_secrets) > 0

    def test_skip_test_values(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "config.py").write_text("password = 'test_password_123'")

        findings = _run_regex_fallback(repo)
        # Should skip because "test" is in ignore patterns
        assert len(findings) == 0

    def test_skip_comments(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "config.py").write_text("# AWS_ACCESS_KEY_ID = 'AKIAIOSFODNN7EXAMPLE'")

        findings = _run_regex_fallback(repo)
        assert len(findings) == 0

    def test_no_secrets_in_clean_repo(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "app.py").write_text("print('hello world')\nx = 42")

        findings = _run_regex_fallback(repo)
        assert len(findings) == 0

    def test_skip_binary_files(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

        findings = _run_regex_fallback(repo)
        assert len(findings) == 0


class TestRunSecretsScan:
    """Test the main secrets scan function."""

    def test_run_secrets_scan_on_python_repo(self, sample_python_repo: Path):
        """Secrets scan should find secrets in Python repo."""
        findings = run_secrets_scan(sample_python_repo)

        assert isinstance(findings, list)
        for finding in findings:
            assert isinstance(finding, Secret)
            assert finding.file
            assert finding.line >= 1

    def test_run_secrets_scan_empty_repo(self, temp_repo: Path):
        """Secrets scan on empty repo should return empty list."""
        findings = run_secrets_scan(temp_repo)
        assert findings == []

    @patch("codesec.scanners.secrets.run_subprocess")
    def test_run_secrets_scan_with_gitleaks(self, mock_run, tmp_path: Path):
        """Test secrets scan when GitLeaks is available."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps([{
                "rule": "aws-access-key",
                "file": "config.py",
                "line": "AWS_KEY = 'AKIA...'",
            }]),
            stderr=""
        )

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "config.py").write_text("AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'")

        findings = run_secrets_scan(repo)

        assert isinstance(findings, list)
        mock_run.assert_called_once()

    @patch("codesec.scanners.secrets.run_subprocess")
    def test_run_secrets_scan_gitleaks_not_found(self, mock_run, tmp_path: Path):
        """Test fallback to regex when GitLeaks is not installed."""
        from codesec.scanners import ScannerError
        mock_run.side_effect = ScannerError("gitleaks not found")

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "config.py").write_text("AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'")

        findings = run_secrets_scan(repo)

        assert isinstance(findings, list)
        # Should still find via regex fallback
        aws_secrets = [s for s in findings if "aws" in s.type.lower()]
        assert len(aws_secrets) > 0


class TestSecretModel:
    """Test Secret Pydantic model."""

    def test_creation(self):
        secret = Secret(
            type="aws_access_key_id",
            tool="gitleaks",
            file="config.py",
            line=5,
            column=1,
            value_preview="AKIA...MPLE",
            severity=Severity.HIGH,
            confidence=0.9,
            remediation="Remove hardcoded secrets and use a secure secret manager.",
        )
        assert secret.type == "aws_access_key_id"
        assert secret.severity == Severity.HIGH
        assert secret.confidence == 0.9

    def test_defaults(self):
        secret = Secret(
            type="api_key",
            tool="regex-fallback",
            file=".env",
            line=1,
        )
        assert secret.severity == Severity.HIGH  # default
        assert secret.confidence == 0.0  # default
        assert secret.column == 1  # default