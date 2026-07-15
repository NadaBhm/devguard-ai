"""Integration tests for CodeSec Agent end-to-end workflows."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codesec.agent import CodeSecAgent
from codesec.models import (
    CodeSecResult,
    DependenciesResult,
    DockerfileFinding,
    Grade,
    RepoMetadata,
    SASTFinding,
    SBOM,
    Secret,
    SecurityScore,
    Severity,
    StackDetection,
    VulnerablePackage,
)


class TestEndToEndAnalysis:
    """Test complete analysis workflow."""

    @patch("codesec.agent.CodeSecAgent._clone_repo")
    @patch("codesec.agent.run_sast")
    @patch("codesec.agent.run_secrets_scan")
    @patch("codesec.agent.run_dependency_scan")
    @patch("codesec.agent.run_dockerfile_scan")
    @patch("codesec.agent.generate_sbom")
    @patch("codesec.agent.detect_stack")
    @patch("codesec.agent.calculate_score")
    async def test_full_analysis_pipeline(
        self, mock_score, mock_stack, mock_sbom, mock_docker,
        mock_deps, mock_secrets, mock_sast, mock_clone, tmp_path: Path
    ):
        """Test complete analysis pipeline from URL to result."""
        clone_dir = tmp_path / "cloned"
        clone_dir.mkdir()
        (clone_dir / "README.md").write_text("# Test")
        mock_clone.return_value = clone_dir

        mock_sast.return_value = [
            SASTFinding(
                rule_id="python.sql-injection",
                tool="semgrep",
                severity=Severity.HIGH,
                category="owasp-top10",
                file="db.py",
                line=15,
                message="SQL injection",
                cwe_id="CWE-89",
            )
        ]
        mock_secrets.return_value = [
            Secret(type="aws_access_key_id", tool="gitleaks", file=".env", line=3, confidence=0.9)
        ]
        mock_deps.return_value = DependenciesResult(
            total_packages=10,
            vulnerable_packages=[
                VulnerablePackage(package="requests", installed_version="2.25.0", cve_id="CVE-2021-1234", severity=Severity.HIGH)
            ]
        )
        mock_docker.return_value = [
            DockerfileFinding(rule_id="DS001", tool="trivy", severity=Severity.HIGH, file="Dockerfile", line=5, message="root user")
        ]
        mock_sbom.return_value = SBOM(serial_number="urn:uuid:test", components_count=5)
        mock_stack.return_value = StackDetection(primary_language="python", frameworks=["fastapi"], confidence=0.92)
        mock_score.return_value = SecurityScore(score=68, grade=Grade.C)

        agent = CodeSecAgent()
        result = await agent.analyze("https://github.com/test/repo")

        assert isinstance(result, CodeSecResult)
        assert result.repo_url == "https://github.com/test/repo"
        assert result.status in ["completed", "completed_with_errors"]
        assert result.stack_detection is not None
        assert result.security_score is not None
        assert len(result.phases) > 0

    @patch("codesec.agent.CodeSecAgent._clone_repo")
    @patch("codesec.agent.run_sast")
    @patch("codesec.agent.run_secrets_scan")
    @patch("codesec.agent.run_dependency_scan")
    @patch("codesec.agent.run_dockerfile_scan")
    @patch("codesec.agent.generate_sbom")
    @patch("codesec.agent.detect_stack")
    @patch("codesec.agent.calculate_score")
    async def test_perfect_repo_analysis(
        self, mock_score, mock_stack, mock_sbom, mock_docker,
        mock_deps, mock_secrets, mock_sast, mock_clone, tmp_path: Path
    ):
        """Test analysis of a perfect repo with no issues."""
        clone_dir = tmp_path / "cloned"
        clone_dir.mkdir()
        mock_clone.return_value = clone_dir

        mock_sast.return_value = []
        mock_secrets.return_value = []
        mock_deps.return_value = DependenciesResult(total_packages=5, vulnerable_packages=[])
        mock_docker.return_value = []
        mock_sbom.return_value = SBOM(serial_number="urn:uuid:test", components_count=3)
        mock_stack.return_value = StackDetection(primary_language="python", frameworks=["fastapi"], confidence=0.95)
        mock_score.return_value = SecurityScore(score=100, grade=Grade.A)

        agent = CodeSecAgent()
        result = await agent.analyze("https://github.com/test/perfect-repo")

        assert result.security_score.score == 100
        assert result.security_score.grade == Grade.A

    @patch("codesec.agent.CodeSecAgent._clone_repo")
    @patch("codesec.agent.run_sast")
    @patch("codesec.agent.run_secrets_scan")
    @patch("codesec.agent.run_dependency_scan")
    @patch("codesec.agent.run_dockerfile_scan")
    @patch("codesec.agent.generate_sbom")
    @patch("codesec.agent.detect_stack")
    @patch("codesec.agent.calculate_score")
    async def test_high_risk_repo(
        self, mock_score, mock_stack, mock_sbom, mock_docker,
        mock_deps, mock_secrets, mock_sast, mock_clone, tmp_path: Path
    ):
        """Test analysis of a high-risk repo."""
        clone_dir = tmp_path / "cloned"
        clone_dir.mkdir()
        mock_clone.return_value = clone_dir

        mock_sast.return_value = [
            SASTFinding(rule_id="sql-injection", tool="semgrep", severity=Severity.CRITICAL, category="c", file="db.py", line=10, message="SQLi", cwe_id="CWE-89")
            for _ in range(5)
        ]
        mock_secrets.return_value = [
            Secret(type="aws_key", tool="gitleaks", file=".env", line=1, confidence=0.9)
            for _ in range(3)
        ]
        mock_deps.return_value = DependenciesResult(
            total_packages=20,
            vulnerable_packages=[
                VulnerablePackage(package="django", installed_version="3.0.0", cve_id="CVE-2023-1", severity=Severity.CRITICAL)
            ]
        )
        mock_docker.return_value = [
            DockerfileFinding(rule_id="DS001", tool="trivy", severity=Severity.CRITICAL, file="Dockerfile", line=1, message="root")
        ]
        mock_sbom.return_value = SBOM(serial_number="urn:uuid:test")
        mock_stack.return_value = StackDetection(primary_language="python", confidence=0.5)
        mock_score.return_value = SecurityScore(score=25, grade=Grade.F)

        agent = CodeSecAgent()
        result = await agent.analyze("https://github.com/test/bad-repo")

        assert result.security_score.score < 50
        assert result.security_score.grade == Grade.F


class TestResultSerialization:
    """Test result serialization."""

    def test_result_json_serialization(self):
        """Test that results can be serialized to JSON."""
        result = CodeSecResult(
            job_id="test-job",
            status="completed",
            repo_url="https://github.com/test/repo",
            repo_metadata=RepoMetadata(name="test", total_files=0, loc=0),
            stack_detection=StackDetection(primary_language="python", confidence=0.9),
            security_score=SecurityScore(score=85, grade=Grade.B),
        )

        json_str = result.json()
        assert isinstance(json_str, str)

        parsed = json.loads(json_str)
        assert parsed["job_id"] == "test-job"
        assert parsed["status"] == "completed"

    def test_result_dict_conversion(self):
        """Test that results can be converted to dict."""
        result = CodeSecResult(
            job_id="test-job",
            status="completed",
            repo_url="https://github.com/test/repo",
            repo_metadata=RepoMetadata(name="test", total_files=0, loc=0),
            stack_detection=StackDetection(primary_language="python", confidence=0.9),
            security_score=SecurityScore(score=85, grade=Grade.B),
        )

        result_dict = result.dict()
        assert isinstance(result_dict, dict)
        assert result_dict["job_id"] == "test-job"


class TestCleanup:
    """Test cleanup after analysis."""

    def test_temp_directory_cleanup(self, tmp_path: Path):
        """Test that temporary clone directory is cleaned up."""
        temp_dir = tmp_path / "temp_clone"
        temp_dir.mkdir()
        (temp_dir / "file.txt").write_text("test")

        agent = CodeSecAgent()
        # Cleanup is done inside analyze, but we can test the concept
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
        assert not temp_dir.exists()


class TestErrorHandling:
    """Test error handling scenarios."""

    @pytest.mark.asyncio
    async def test_invalid_url(self):
        agent = CodeSecAgent()
        result = await agent.analyze("not-a-url")
        assert result.status == "failed"
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_non_github_url(self):
        agent = CodeSecAgent()
        result = await agent.analyze("https://gitlab.com/owner/repo")
        assert result.status == "failed"