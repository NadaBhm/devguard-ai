"""
CodeSec Agent — Main Orchestrator
====================================
Coordinates all security scanners in parallel where possible, aggregates results,
and produces the final CodeSecResult JSON matching the mockup schema exactly.

US-1.1.1: Submit GitHub URL → clone → analyze within 5 seconds initiation.
US-1.1.2: Stack detection with >=80% accuracy.
US-1.1.3: SAST for OWASP Top 10 with severity classification.
US-1.1.4: Secrets detection with >80% recall.
US-1.1.5: Security score 0-100 with grade A-F.
US-1.1.6: SBOM generation in CycloneDX/SPDX format.

Design Decisions:
- Parallel execution: stack_detection runs first (needed for metadata), then
  sast, secrets, dependencies, dockerfile, and sbom run in parallel via asyncio.
- Sandboxed: Only reads files, never executes arbitrary code from the repo.
- Public repos only: Validates GitHub URL format, rejects private repos.
- Phase tracking: Each scanner reports its phase status for real-time progress.

Integration Contracts:
- InfraCost: Consumes stack_detection output (primary_language, frameworks,
  database, container.detected) to recommend AWS services.
- Orchestrator: Triggers via CodeSecAgent.analyze(repo_url) and receives
  CodeSecResult as the return value (LangGraph node contract).
- RAG: Embeds README.md, docs/, and key code snippets for chat context.

Author: Nada 
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import DEFAULT_CLONE_DIR, GITHUB_URL_PATTERN, MAX_FILES_PER_REPO, MAX_REPO_SIZE_MB
from .models import (
    CodeSecResult,
    DependenciesResult,
    Grade,
    LanguageBreakdown,
    PhaseInfo,
    PhaseStatus,
    RepoMetadata,
    SASTFinding,
    SBOM,
    Secret,
    SecurityScore,
    StackDetection,
    Summary,
)
from .scanners.dependencies import run_dependency_scan
from .scanners.dockerfile_scanner import run_dockerfile_scan
from .scanners.sast import run_sast
from .scanners.sbom import generate_sbom
from .scanners.scorer import calculate_score
from .scanners.secrets import run_secrets_scan
from .scanners.stack_detection import detect_stack, get_language_breakdown

logger = logging.getLogger(__name__)


class CodeSecAgent:
    """
    Main orchestrator for the CodeSec security analysis pipeline.

    Usage:
        agent = CodeSecAgent()
        result = await agent.analyze("https://github.com/owner/repo")
    """

    def __init__(self, clone_dir: str | None = None) -> None:
        """
        Initialize the CodeSec agent.

        Args:
            clone_dir: Directory to clone repositories into. Defaults to
                       /tmp/codesec-clones or env CODESEC_CLONE_DIR.
        """
        self.clone_dir = Path(clone_dir or DEFAULT_CLONE_DIR)
        self.clone_dir.mkdir(parents=True, exist_ok=True)

    def _validate_github_url(self, url: str | None) -> str:
        """
        Validate that the provided URL is a public GitHub repository URL.

        Args:
            url: Repository URL string.

        Returns:
            Cleaned URL string.

        Raises:
            ValueError: If URL is invalid, not GitHub, or appears to be private.
        """
        if url is None:
            raise ValueError("URL cannot be None")

        if not url or not url.startswith("http"):
            raise ValueError("URL must be a valid HTTP/HTTPS URL")

        parsed = urlparse(url)
        if parsed.netloc.lower() != "github.com":
            raise ValueError("Only public GitHub repositories are supported")

        if not re.match(GITHUB_URL_PATTERN, url, re.IGNORECASE):
            raise ValueError("Invalid GitHub repository URL format")

        # Reject obvious private repo indicators
        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) < 2:
            raise ValueError("GitHub URL must contain owner and repository name")

        # Clean URL: remove trailing slash, .git, and fragment
        cleaned = url.rstrip("/").removesuffix(".git")
        if "?" in cleaned:
            cleaned = cleaned.split("?")[0]

        return cleaned

    def _clone_repo(self, repo_url: str, job_id: str) -> Path:
        """
        Clone a public GitHub repository to the local filesystem.

        Args:
            repo_url: Validated GitHub URL.
            job_id: Unique job identifier for directory naming.

        Returns:
            Path to the cloned repository root.

        Raises:
            RuntimeError: If cloning fails.
        """
        repo_name = repo_url.rstrip("/").split("/")[-1]
        target_dir = self.clone_dir / f"{job_id}_{repo_name}"

        # Remove existing directory if present
        if target_dir.exists():
            shutil.rmtree(target_dir)

        logger.info("Cloning %s into %s", repo_url, target_dir)

        cmd = [
            "git",
            "clone",
            "--depth=1",  # Shallow clone for speed
            "--single-branch",
            "--branch=main",
            repo_url,
            str(target_dir),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if result.returncode != 0:
                # Try default branch if main fails
                cmd[5] = "--branch=master"
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"Git clone failed: {result.stderr}")
        except subprocess.TimeoutExpired:
            raise RuntimeError("Git clone timed out after 60 seconds")
        except FileNotFoundError:
            raise RuntimeError("Git is not installed or not in PATH")

        # Validate size constraints
        total_size = sum(f.stat().st_size for f in target_dir.rglob("*") if f.is_file())
        total_size_mb = total_size / (1024 * 1024)
        if total_size_mb > MAX_REPO_SIZE_MB:
            shutil.rmtree(target_dir)
            raise RuntimeError(f"Repository exceeds {MAX_REPO_SIZE_MB} MB limit ({total_size_mb:.1f} MB)")

        total_files = sum(1 for _ in target_dir.rglob("*") if _.is_file())
        if total_files > MAX_FILES_PER_REPO:
            shutil.rmtree(target_dir)
            raise RuntimeError(f"Repository exceeds {MAX_FILES_PER_REPO} file limit ({total_files} files)")

        logger.info("Clone complete: %s (%.1f MB, %d files)", target_dir, total_size_mb, total_files)
        return target_dir

    def _get_repo_metadata(self, repo_path: Path, repo_url: str) -> RepoMetadata:
        """Extract repository metadata from cloned directory."""
        repo_name = repo_url.rstrip("/").split("/")[-1]
        total_files = sum(1 for _ in repo_path.rglob("*") if _.is_file())
        lang_breakdown = get_language_breakdown(repo_path)
        total_loc = sum(lang_breakdown.values())

        # Try to get current commit SHA
        commit_sha = None
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                commit_sha = result.stdout.strip()[:12]
        except Exception:
            pass

        # Try to get current branch
        branch = "main"
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_path), "branch", "--show-current"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                branch = result.stdout.strip() or "main"
        except Exception:
            pass

        return RepoMetadata(
            name=repo_name,
            branch=branch,
            commit_sha=commit_sha,
            total_files=total_files,
            loc=total_loc,
            language_breakdown=lang_breakdown,
        )

    async def _run_scanners(self, repo_path: Path) -> dict[str, Any]:
        """
        Run all scanners. Stack detection runs first, then the rest in parallel.

        Args:
            repo_path: Path to cloned repository.

        Returns:
            Dictionary of scanner results.
        """
        # Phase 1: Stack detection (needed for metadata and downstream consumers)
        stack_result = detect_stack(repo_path)

        # Phase 2: Run remaining scanners in parallel
        loop = asyncio.get_event_loop()

        sast_task = loop.run_in_executor(None, run_sast, repo_path)
        secrets_task = loop.run_in_executor(None, run_secrets_scan, repo_path)
        deps_task = loop.run_in_executor(None, run_dependency_scan, repo_path)
        dockerfile_task = loop.run_in_executor(None, run_dockerfile_scan, repo_path)
        sbom_task = loop.run_in_executor(None, generate_sbom, repo_path)

        sast_result = await sast_task
        secrets_result = await secrets_task
        deps_result = await deps_task
        dockerfile_result = await dockerfile_task
        sbom_result = await sbom_task

        return {
            "stack": stack_result,
            "sast": sast_result,
            "secrets": secrets_result,
            "dependencies": deps_result,
            "dockerfile": dockerfile_result,
            "sbom": sbom_result,
        }

    async def analyze(self, repo_url: str, job_id: str | None = None) -> CodeSecResult:
        """
        Run the complete CodeSec analysis pipeline on a public GitHub repository.

        Args:
            repo_url: Public GitHub repository URL.
            job_id: Optional job identifier. If not provided, one is generated.

        Returns:
            CodeSecResult containing all scan results, scores, and metadata.

        Raises:
            ValueError: If URL is invalid.
            RuntimeError: If cloning or scanning fails critically.
        """
        import uuid

        job_id = job_id or str(uuid.uuid4())
        phases: list[PhaseInfo] = []
        error_message: str | None = None

        def _add_phase(name: str, status: PhaseStatus, started: datetime | None = None, completed: datetime | None = None, err: str | None = None) -> None:
            phases.append(PhaseInfo(name=name, status=status, started_at=started, completed_at=completed, error_message=err))

        try:
            # Validate URL
            validated_url = self._validate_github_url(repo_url)
        except ValueError as exc:
            _add_phase("validation", PhaseStatus.FAILED, err=str(exc))
            return CodeSecResult(
                job_id=job_id,
                status="failed",
                error=str(exc),
                repo_url=repo_url,
                repo_metadata=RepoMetadata(name="", total_files=0, loc=0),
                phases=phases,
                stack_detection=StackDetection(primary_language="unknown", confidence=0.0),
                security_score=SecurityScore(score=0, grade=Grade.F),
            )

        _add_phase("validation", PhaseStatus.COMPLETED)

        # Clone repository
        clone_start = datetime.now(timezone.utc)
        _add_phase("clone", PhaseStatus.RUNNING, started=clone_start)
        try:
            repo_path = self._clone_repo(validated_url, job_id)
            clone_end = datetime.now(timezone.utc)
            _add_phase("clone", PhaseStatus.COMPLETED, started=clone_start, completed=clone_end)
        except RuntimeError as exc:
            clone_end = datetime.now(timezone.utc)
            _add_phase("clone", PhaseStatus.FAILED, started=clone_start, completed=clone_end, err=str(exc))
            return CodeSecResult(
                job_id=job_id,
                status="failed",
                error=str(exc),
                repo_url=validated_url,
                repo_metadata=RepoMetadata(name="", total_files=0, loc=0),
                phases=phases,
                stack_detection=StackDetection(primary_language="unknown", confidence=0.0),
                security_score=SecurityScore(score=0, grade=Grade.F),
            )

        # Get metadata
        repo_metadata = self._get_repo_metadata(repo_path, validated_url)

        # Run stack detection
        stack_start = datetime.now(timezone.utc)
        _add_phase("stack_detection", PhaseStatus.RUNNING, started=stack_start)
        try:
            stack_result = detect_stack(repo_path)
            stack_end = datetime.now(timezone.utc)
            _add_phase("stack_detection", PhaseStatus.COMPLETED, started=stack_start, completed=stack_end)
        except Exception as exc:
            stack_end = datetime.now(timezone.utc)
            _add_phase("stack_detection", PhaseStatus.FAILED, started=stack_start, completed=stack_end, err=str(exc))
            stack_result = StackDetection(primary_language="unknown", confidence=0.0)

        # Run remaining scanners in parallel
        scan_start = datetime.now(timezone.utc)
        for name in ["sast", "secrets", "dependencies", "dockerfile_scan", "sbom"]:
            _add_phase(name, PhaseStatus.RUNNING, started=scan_start)

        try:
            results = await self._run_scanners(repo_path)
            scan_end = datetime.now(timezone.utc)
            for name in ["sast", "secrets", "dependencies", "dockerfile_scan", "sbom"]:
                _add_phase(name, PhaseStatus.COMPLETED, started=scan_start, completed=scan_end)
        except Exception as exc:
            scan_end = datetime.now(timezone.utc)
            for name in ["sast", "secrets", "dependencies", "dockerfile_scan", "sbom"]:
                _add_phase(name, PhaseStatus.FAILED, started=scan_start, completed=scan_end, err=str(exc))
            results = {
                "stack": stack_result,
                "sast": [],
                "secrets": [],
                "dependencies": DependenciesResult(),
                "dockerfile": [],
                "sbom": SBOM(serial_number=f"urn:uuid:{uuid.uuid4()}"),
            }
            error_message = str(exc)

        # Calculate security score
        score_start = datetime.now(timezone.utc)
        _add_phase("scoring", PhaseStatus.RUNNING, started=score_start)
        try:
            security_score = calculate_score(
                sast_findings=results["sast"],
                secrets=results["secrets"],
                vulnerable_packages=results["dependencies"].vulnerable_packages,
                dockerfile_findings=results["dockerfile"],
                sbom=results["sbom"],
                stack_detection=stack_result,
            )
            score_end = datetime.now(timezone.utc)
            _add_phase("scoring", PhaseStatus.COMPLETED, started=score_start, completed=score_end)
        except Exception as exc:
            score_end = datetime.now(timezone.utc)
            _add_phase("scoring", PhaseStatus.FAILED, started=score_start, completed=score_end, err=str(exc))
            security_score = SecurityScore(score=0, grade=Grade.F)
            error_message = error_message or str(exc)

        # Build summary
        summary = Summary(
            files_scanned=repo_metadata.total_files,
            sast_findings_count=len(results["sast"]),
            secrets_found_count=len(results["secrets"]),
            vulnerable_dependencies_count=len(results["dependencies"].vulnerable_packages),
            dockerfile_issues_count=len(results["dockerfile"]),
            total_critical=security_score.severity_counts.critical,
            total_high=security_score.severity_counts.high,
            total_medium=security_score.severity_counts.medium,
            total_low=security_score.severity_counts.low,
            total_info=security_score.severity_counts.info,
        )

        # Set SBOM download URL
        results["sbom"].download_url = f"/api/jobs/{job_id}/sbom/download"

        # Cleanup clone directory
        try:
            shutil.rmtree(repo_path, ignore_errors=True)
        except Exception:
            pass

        result = CodeSecResult(
            job_id=job_id,
            status="completed" if not error_message else "completed_with_errors",
            error=error_message,
            repo_url=validated_url,
            repo_metadata=repo_metadata,
            phases=phases,
            summary=summary,
            stack_detection=stack_result,
            sast_findings=results["sast"],
            secrets=results["secrets"],
            dependencies=results["dependencies"],
            dockerfile_findings=results["dockerfile"],
            sbom=results["sbom"],
            security_score=security_score,
        )

        logger.info(
            "CodeSec analysis complete for job %s: score=%d/100, grade=%s, findings=%d",
            job_id,
            result.security_score.score,
            result.security_score.grade.value,
            summary.sast_findings_count + summary.secrets_found_count + summary.vulnerable_dependencies_count + summary.dockerfile_issues_count,
        )
        return result

    def analyze_sync(self, repo_url: str, job_id: str | None = None) -> CodeSecResult:
        """Synchronous wrapper for analyze()."""
        return asyncio.run(self.analyze(repo_url, job_id))