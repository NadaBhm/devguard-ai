"""Tests for Dependency Vulnerability Scanner."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codesec.scanners.dependencies import (
    run_dependency_scan,
    _parse_pip_audit_output,
    _parse_manifest_files,
    _run_pip_audit,
    _run_safety,
    _run_trivy_fs,
)
from codesec.models import DependenciesResult, VulnerablePackage, Severity


class TestParsePipAuditOutput:
    """Test parsing pip-audit JSON output."""

    def test_parse_valid_output(self):
        stdout = json.dumps({
            "dependencies": [
                {
                    "name": "requests",
                    "version": "2.25.0",
                    "fix_versions": ["2.31.0"],
                    "vulns": [
                        {
                            "id": "PYSEC-2023-1",
                            "severity": "high",
                            "description": "Session fixation vulnerability",
                            "cvss": 7.5
                        }
                    ]
                }
            ]
        })
        findings = _parse_pip_audit_output(stdout)
        assert len(findings) == 1
        assert findings[0].package == "requests"
        assert findings[0].installed_version == "2.25.0"
        assert findings[0].fixed_version == "2.31.0"
        assert findings[0].cve_id == "PYSEC-2023-1"
        assert findings[0].severity == Severity.HIGH

    def test_parse_empty_dependencies(self):
        stdout = json.dumps({"dependencies": []})
        findings = _parse_pip_audit_output(stdout)
        assert findings == []

    def test_parse_invalid_json(self):
        findings = _parse_pip_audit_output("not json")
        assert findings == []

    def test_parse_no_vulns(self):
        stdout = json.dumps({
            "dependencies": [
                {
                    "name": "flask",
                    "version": "2.0.1",
                    "vulns": []
                }
            ]
        })
        findings = _parse_pip_audit_output(stdout)
        assert findings == []


class TestParseManifestFiles:
    """Test manifest file parsing."""

    def test_parse_requirements_txt(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "requirements.txt").write_text("flask==2.0.1\nrequests>=2.25.0\n")

        total, direct, transitive = _parse_manifest_files(repo)
        assert direct == 2
        assert total >= 2

    def test_parse_package_json(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "package.json").write_text(json.dumps({
            "dependencies": {"express": "^4.17.1"},
            "devDependencies": {"jest": "^29.0.0"}
        }))

        total, direct, transitive = _parse_manifest_files(repo)
        assert direct == 2

    def test_parse_go_mod(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "go.mod").write_text("module example.com/test\ngo 1.19\nrequire github.com/gin-gonic/gin v1.9.0\n")

        total, direct, transitive = _parse_manifest_files(repo)
        assert direct >= 1

    def test_empty_repo(self, tmp_path: Path):
        repo = tmp_path / "repo"
        repo.mkdir()

        total, direct, transitive = _parse_manifest_files(repo)
        assert total == 0
        assert direct == 0


class TestRunDependencyScan:
    """Test the main dependency scan function."""

    def test_run_dependency_scan_empty_repo(self, temp_repo: Path):
        """Dependency scan on empty repo."""
        result = run_dependency_scan(temp_repo)

        assert isinstance(result, DependenciesResult)
        assert result.total_packages == 0
        assert result.vulnerable_packages == []

    def test_run_dependency_scan_python_repo(self, sample_python_repo: Path):
        """Dependency scan on Python repo."""
        result = run_dependency_scan(sample_python_repo)

        assert isinstance(result, DependenciesResult)
        assert result.total_packages >= 0
        assert isinstance(result.vulnerable_packages, list)

    @patch("codesec.scanners.dependencies._run_pip_audit")
    @patch("codesec.scanners.dependencies._run_safety")
    @patch("codesec.scanners.dependencies._run_trivy_fs")
    def test_run_dependency_scan_uses_pip_audit_first(
        self, mock_trivy, mock_safety, mock_pip_audit, sample_python_repo: Path
    ):
        mock_pip_audit.return_value = [
            VulnerablePackage(
                package="requests",
                installed_version="2.25.0",
                fixed_version="2.31.0",
                cve_id="CVE-2021-1234",
                severity=Severity.HIGH,
            )
        ]
        mock_safety.return_value = []
        mock_trivy.return_value = []

        result = run_dependency_scan(sample_python_repo)

        mock_pip_audit.assert_called_once()
        # Safety and Trivy should still run for additional coverage
        assert len(result.vulnerable_packages) >= 1

    @patch("codesec.scanners.dependencies._run_pip_audit")
    @patch("codesec.scanners.dependencies._run_safety")
    @patch("codesec.scanners.dependencies._run_trivy_fs")
    def test_run_dependency_scan_deduplicates(
        self, mock_trivy, mock_safety, mock_pip_audit, tmp_path: Path
    ):
        """Test deduplication of vulnerabilities from multiple tools."""
        vuln = VulnerablePackage(
            package="requests",
            installed_version="2.25.0",
            cve_id="CVE-2021-1234",
            severity=Severity.HIGH,
        )
        mock_pip_audit.return_value = [vuln]
        mock_safety.return_value = [vuln]  # Same vuln
        mock_trivy.return_value = []

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "requirements.txt").write_text("requests==2.25.0")

        result = run_dependency_scan(repo)

        # Should deduplicate - only 1 unique (package, cve_id)
        assert len(result.vulnerable_packages) == 1


class TestVulnerablePackageModel:
    """Test VulnerablePackage model."""

    def test_creation(self):
        pkg = VulnerablePackage(
            package="requests",
            installed_version="2.25.0",
            fixed_version="2.31.0",
            cve_id="CVE-2021-1234",
            severity=Severity.HIGH,
            cvss_score=7.5,
            description="Session fixation vulnerability",
        )
        assert pkg.package == "requests"
        assert pkg.severity == Severity.HIGH
        assert pkg.cvss_score == 7.5

    def test_optional_fields(self):
        pkg = VulnerablePackage(
            package="test",
            installed_version="1.0",
            severity=Severity.LOW,
        )
        assert pkg.fixed_version is None
        assert pkg.cve_id is None
        assert pkg.cvss_score is None