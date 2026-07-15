"""Tests for CodeSec Agent."""
import asyncio
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from codesec.agent import CodeSecAgent
from codesec.models import (
    CodeSecResult,
    Grade,
    PhaseStatus,
    RepoMetadata,
    SASTFinding,
    SBOM,
    Secret,
    SecurityScore,
    StackDetection,
    Summary,
    Severity,
    VulnerablePackage,
    DockerfileFinding,
    DependenciesResult,
)


class TestCodeSecAgentInitialization:
    """Test agent initialization."""

    def test_agent_default_init(self):
        agent = CodeSecAgent()
        assert agent.clone_dir.exists()

    def test_agent_custom_clone_dir(self, tmp_path: Path):
        agent = CodeSecAgent(clone_dir=str(tmp_path))
        assert agent.clone_dir == tmp_path


class TestValidateGitHubURL:
    """Test GitHub URL validation."""

    def test_valid_https_url(self):
        agent = CodeSecAgent()
        url = agent._validate_github_url("https://github.com/owner/repo")
        assert url == "https://github.com/owner/repo"

    def test_valid_url_with_git_suffix(self):
        agent = CodeSecAgent()
        url = agent._validate_github_url("https://github.com/owner/repo.git")
        assert url == "https://github.com/owner/repo"

    def test_invalid_url_not_github(self):
        agent = CodeSecAgent()
        with pytest.raises(ValueError):
            agent._validate_github_url("https://gitlab.com/owner/repo")

    def test_invalid_url_malformed(self):
        agent = CodeSecAgent()
        with pytest.raises(ValueError):
            agent._validate_github_url("not-a-url")

    def test_invalid_url_empty(self):
        agent = CodeSecAgent()
        with pytest.raises(ValueError):
            agent._validate_github_url("")

    def test_invalid_url_none(self):
        agent = CodeSecAgent()
        with pytest.raises(ValueError):
            agent._validate_github_url(None)

    def test_url_with_path(self):
        agent = CodeSecAgent()
        url = agent._validate_github_url("https://github.com/owner/repo/tree/main")
        assert url == "https://github.com/owner/repo/tree/main"


class TestCloneRepo:
    """Test repository cloning."""

    @patch("codesec.agent.subprocess.run")
    def test_clone_success(self, mock_run, tmp_path: Path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        agent = CodeSecAgent(clone_dir=str(tmp_path))
        repo_path = agent._clone_repo("https://github.com/owner/repo", "test-job")

        assert isinstance(repo_path, Path)
        mock_run.assert_called()

    @patch("codesec.agent.subprocess.run")
    def test_clone_failure(self, mock_run, tmp_path: Path):
        mock_run.return_value = MagicMock(returncode=128, stderr="Repository not found")

        agent = CodeSecAgent(clone_dir=str(tmp_path))
        with pytest.raises(RuntimeError):
            agent._clone_repo("https://github.com/nonexistent/repo", "test-job")

    @patch("codesec.agent.subprocess.run")
    def test_clone_timeout(self, mock_run, tmp_path: Path):
        mock_run.side_effect = subprocess.TimeoutExpired("git", 60)

        agent = CodeSecAgent(clone_dir=str(tmp_path))
        with pytest.raises(RuntimeError):
            agent._clone_repo("https://github.com/owner/repo", "test-job")


class TestGetRepoMetadata:
    """Test metadata extraction."""

    def test_metadata_from_repo(self, sample_python_repo: Path):
        agent = CodeSecAgent()
        metadata = agent._get_repo_metadata(sample_python_repo, "https://github.com/test/repo")

        assert isinstance(metadata, RepoMetadata)
        assert metadata.name == "repo"
        assert metadata.total_files > 0
        assert metadata.loc > 0
        assert "python" in metadata.language_breakdown


class TestAnalyze:
    """Test the main analysis function."""

    @patch("codesec.agent.run_sast")
    @patch("codesec.agent.run_secrets_scan")
    @patch("codesec.agent.run_dependency_scan")
    @patch("codesec.agent.run_dockerfile_scan")
    @patch("codesec.agent.generate_sbom")
    @patch("codesec.agent.detect_stack")
    @patch("codesec.agent.calculate_score")
    @patch("codesec.agent.CodeSecAgent._clone_repo")
    async def test_analyze_success(
        self, mock_clone, mock_score, mock_stack, mock_sbom, mock_docker,
        mock_deps, mock_secrets, mock_sast, tmp_path: Path
    ):
        mock_clone.return_value = tmp_path / "cloned_repo"
        mock_clone.return_value.mkdir(exist_ok=True)
        (mock_clone.return_value / "README.md").write_text("# Test")

        mock_sast.return_value = []
        mock_secrets.return_value = []
        mock_deps.return_value = DependenciesResult()
        mock_docker.return_value = []
        mock_sbom.return_value = SBOM(serial_number="urn:uuid:test")
        mock_stack.return_value = StackDetection(primary_language="python", confidence=0.9)
        mock_score.return_value = SecurityScore(score=95, grade=Grade.A)

        agent = CodeSecAgent()
        result = await agent.analyze("https://github.com/owner/repo")

        assert isinstance(result, CodeSecResult)
        assert result.status in ["completed", "completed_with_errors"]
        assert result.repo_url == "https://github.com/owner/repo"
        assert result.security_score is not None

    @patch("codesec.agent.CodeSecAgent._validate_github_url")
    async def test_analyze_invalid_url(self, mock_validate, tmp_path: Path):
        mock_validate.side_effect = ValueError("Invalid URL")

        agent = CodeSecAgent()
        result = await agent.analyze("not-a-url")

        assert isinstance(result, CodeSecResult)
        assert result.status == "failed"
        assert result.error is not None

    @patch("codesec.agent.CodeSecAgent._clone_repo")
    async def test_analyze_clone_failure(self, mock_clone, tmp_path: Path):
        mock_clone.side_effect = RuntimeError("Clone failed")

        agent = CodeSecAgent()
        result = await agent.analyze("https://github.com/owner/repo")

        assert isinstance(result, CodeSecResult)
        assert result.status == "failed"


class TestAnalyzeSyncWrapper:
    """Test synchronous wrapper."""

    @patch("codesec.agent.CodeSecAgent.analyze")
    def test_analyze_sync(self, mock_analyze, tmp_path: Path):
        mock_analyze.return_value = CodeSecResult(
            job_id="test",
            repo_url="https://github.com/owner/repo",
            repo_metadata=RepoMetadata(name="repo", total_files=0, loc=0),
            stack_detection=StackDetection(primary_language="python", confidence=0.9),
            security_score=SecurityScore(score=85, grade=Grade.B),
        )

        agent = CodeSecAgent()
        result = agent.analyze_sync("https://github.com/owner/repo")

        assert isinstance(result, CodeSecResult)
        assert result.repo_url == "https://github.com/owner/repo"
        mock_analyze.assert_called_once()


class TestCodeSecResultModel:
    """Test CodeSecResult model."""

    def test_creation(self):
        result = CodeSecResult(
            job_id="550e8400-e29b-41d4-a716-446655440000",
            status="completed",
            repo_url="https://github.com/owner/repo",
            repo_metadata=RepoMetadata(name="repo", total_files=10, loc=500),
            stack_detection=StackDetection(primary_language="python", confidence=0.92),
            security_score=SecurityScore(score=75, grade=Grade.C),
        )
        assert result.job_id == "550e8400-e29b-41d4-a716-446655440000"
        assert result.status == "completed"

    def test_defaults(self):
        result = CodeSecResult(
            job_id="test",
            repo_url="https://github.com/owner/repo",
            repo_metadata=RepoMetadata(name="repo", total_files=0, loc=0),
            stack_detection=StackDetection(primary_language="python", confidence=0.0),
            security_score=SecurityScore(score=0, grade=Grade.F),
        )
        assert result.phases == []
        assert result.sast_findings == []
        assert result.secrets == []
        assert result.error is None