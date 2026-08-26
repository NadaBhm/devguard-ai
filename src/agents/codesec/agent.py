"""
CodeSec Agent — Main Orchestrator.
Coordinates all security scanners in parallel and produces the final
CodeSecResult JSON.

Design decisions:
- stack_detection runs first (metadata needed downstream); sast, secrets,
  dependencies, dockerfile, and sbom run in parallel via asyncio.
- Sandboxed: reads files only, never executes repo code.
- Public GitHub/GitLab URLs only; local folder paths also supported.
- Each scanner reports its phase status for real-time progress.
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

from .config import (
    DEFAULT_CLONE_DIR,
    GITHUB_URL_PATTERN,
    GITLAB_URL_PATTERN,
    MAX_FILES_PER_REPO,
    MAX_REPO_SIZE_MB,
    TOOLS,
)
from .models import (
    CodeSecResult,
    DependenciesResult,
    Grade,
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
from .scanners import find_dockerfiles
from .scanners.dependencies import run_dependency_scan
from .scanners.dockerfile_scanner import run_dockerfile_scan
from .scanners.sast import run_sast
from .scanners.sbom import generate_sbom
from .scanners.scorer import calculate_score
from .scanners.secrets import run_secrets_scan
from .scanners.stack_detection import detect_stack, get_language_breakdown

logger = logging.getLogger(__name__)

_MAX_DOCKERFILE_BYTES = 256 * 1024


def _capture_dockerfile_contents(repo_path: Path, stack_result: Any) -> dict[str, str]:
    """Read every Dockerfile in the repository, keyed by repo-relative path.

    Prefers the exact Dockerfiles the stack detector identified
    (``stack_detection.containers[*].dockerfile_path``), falling back to
    scanning for ``Dockerfile*`` / ``*.dockerfile`` across the clone so
    variants like ``Dockerfile.backend`` are still captured. Multi-container
    repos get one entry per file. Returns ``{}`` (never raises) when no
    readable, size-safe Dockerfile exists.
    """
    contents: dict[str, str] = {}
    try:
        candidates: list[Path] = []

        detected = getattr(getattr(stack_result, "container", None), "dockerfile_path", None)
        for cont in getattr(stack_result, "containers", None) or []:
            df = getattr(cont, "dockerfile_path", None)
            if df:
                df_path = repo_path / df
                if df_path.is_file():
                    candidates.append(df_path)
        if not candidates and detected:
            detected_path = repo_path / detected
            if detected_path.is_file():
                candidates.append(detected_path)

        if not candidates:
            candidates = find_dockerfiles(repo_path)

        for candidate in candidates:
            try:
                if candidate.is_file() and candidate.stat().st_size <= _MAX_DOCKERFILE_BYTES:
                    content = candidate.read_text(encoding="utf-8", errors="replace")
                    if content:
                        rel = candidate.relative_to(repo_path).as_posix()
                        contents[rel] = content
            except OSError:
                continue
    except Exception:
        pass
    return contents


def _capture_dockerfile_content(repo_path: Path, stack_result: Any) -> str | None:
    """Read the repository's primary Dockerfile content for downstream agents.

    Backward-compatible single-string view over ``_capture_dockerfile_contents``
    — returns the first captured Dockerfile, preferring the one the stack
    detector identified. Returns ``None`` (never raises) when none exists.
    """
    contents = _capture_dockerfile_contents(repo_path, stack_result)
    if not contents:
        return None
    detected = getattr(getattr(stack_result, "container", None), "dockerfile_path", None)
    if detected and detected in contents:
        return contents[detected]
    return next(iter(contents.values()))


class CodeSecAgent:
    """
    Main orchestrator for the CodeSec security analysis pipeline.

    Usage:
        agent = CodeSecAgent()
        result = await agent.analyze("https://github.com/owner/repo")
    """

    def __init__(self, clone_dir: str | None = None) -> None:
        self.clone_dir = Path(clone_dir or DEFAULT_CLONE_DIR)
        self.clone_dir.mkdir(parents=True, exist_ok=True)

    def _validate_repo_url(self, url: str | None) -> str:
        """Validate that the URL is a public GitHub or GitLab repository URL."""
        if url is None:
            raise ValueError("URL cannot be None")

        if not url or not url.startswith("http"):
            raise ValueError("URL must be a valid HTTP/HTTPS URL")

        parsed = urlparse(url)
        host = parsed.netloc.lower()

        if host not in ("github.com", "gitlab.com"):
            raise ValueError("Only public GitHub or GitLab repositories are supported")

        pattern = GITHUB_URL_PATTERN if host == "github.com" else GITLAB_URL_PATTERN
        if not re.match(pattern, url, re.IGNORECASE):
            raise ValueError("Invalid repository URL format for host: %s" % host)

        path_parts = parsed.path.strip("/").split("/")
        if len(path_parts) < 2:
            raise ValueError("Repository URL must contain owner and repository name")

        cleaned = url.rstrip("/").removesuffix(".git")
        if "?" in cleaned:
            cleaned = cleaned.split("?")[0]

        return cleaned

    def _clone_repo(self, repo_url: str, job_id: str, commit_sha: str | None = None) -> Path:
        """Clone a repository to the local filesystem. ``commit_sha`` pins the
        checkout to that exact commit via the shared helper (None = tip)."""
        if commit_sha and commit_sha != "HEAD":
            repo_name = repo_url.rstrip("/").split("/")[-1]
            target_dir = self.clone_dir / f"{job_id}_{repo_name}"
            if target_dir.exists():
                shutil.rmtree(target_dir)
            logger.info("Cloning %s @ %s into %s", repo_url, commit_sha, target_dir)
            from src.lib.repo import clone_repo as _shared_clone
            return _shared_clone(
                repo_url, target_dir,
                max_size_mb=MAX_REPO_SIZE_MB,
                max_files=MAX_FILES_PER_REPO,
                timeout=300,
                commit_sha=commit_sha,
            )
        repo_name = repo_url.rstrip("/").split("/")[-1]
        target_dir = self.clone_dir / f"{job_id}_{repo_name}"

        if target_dir.exists():
            shutil.rmtree(target_dir)

        logger.info("Cloning %s into %s", repo_url, target_dir)

        cmd = [
            "git",
            "clone",
            "--depth=1",
            "--single-branch",
            "--branch=main",
            repo_url,
            str(target_dir),
        ]

        try:
            # 300s: 60s expired for 66MB repos while docker buildx saturated
            # the link (pnpm) — clone is I/O-bound, not hung.
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            if result.returncode != 0:
                if target_dir.exists():
                    shutil.rmtree(target_dir)
                cmd[4] = "--branch=master"
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
            if result.returncode != 0:
                # Some repos default to neither main nor master (e.g. canary).
                # Fall back to the remote's default branch.
                if target_dir.exists():
                    shutil.rmtree(target_dir)
                fallback_cmd = [
                    "git",
                    "clone",
                    "--depth=1",
                    repo_url,
                    str(target_dir),
                ]
                result = subprocess.run(
                    fallback_cmd,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    check=False,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"Git clone failed: {result.stderr}")
        except subprocess.TimeoutExpired:
            raise RuntimeError("Git clone timed out after 60 seconds")
        except FileNotFoundError:
            raise RuntimeError("Git is not installed or not in PATH")

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

    async def _run_scanners(
        self, repo_path: Path, stack_result: StackDetection, _add_phase
    ) -> dict[str, Any]:
        """Run all scanners in parallel with per-scanner timing."""
        loop = asyncio.get_event_loop()

        async def _run(name: str, func, *args):
            start = datetime.now(timezone.utc)
            _add_phase(name, PhaseStatus.RUNNING, started=start)
            try:
                result = await loop.run_in_executor(None, func, *args)
                end = datetime.now(timezone.utc)
                _add_phase(name, PhaseStatus.COMPLETED, started=start, completed=end)
                return name, result, None
            except Exception as exc:
                end = datetime.now(timezone.utc)
                _add_phase(name, PhaseStatus.FAILED, started=start, completed=end, err=str(exc))
                return name, None, exc

        tasks = [
            _run("sast", run_sast, repo_path),
            _run("secrets", run_secrets_scan, repo_path),
            _run("dependencies", run_dependency_scan, repo_path),
            _run("dockerfile", run_dockerfile_scan, repo_path),
            _run("sbom", generate_sbom, repo_path),
        ]
        results_list = await asyncio.gather(*tasks)

        results: dict[str, Any] = {"stack": stack_result}
        errors: list[Exception] = []
        for name, result, exc in results_list:
            if exc:
                errors.append(exc)
            else:
                results[name] = result

        if errors:
            raise errors[0]

        return results

    async def analyze(self, repo_url: str, job_id: str | None = None, commit_sha: str | None = None) -> CodeSecResult:
        """Run the complete CodeSec analysis pipeline. ``commit_sha`` pins
        checkout to that commit (None/HEAD = tip)."""
        import uuid

        job_id = job_id or str(uuid.uuid4())
        phases: list[PhaseInfo] = []
        error_message: str | None = None

        def _add_phase(name: str, status: PhaseStatus, started: datetime | None = None, completed: datetime | None = None, err: str | None = None) -> None:
            phases.append(PhaseInfo(name=name, status=status, started_at=started, completed_at=completed, error_message=err))

        # Support local folder paths (uploaded project) or remote Git URLs
        repo_path: Path | None = None
        validated_url: str | None = None

        if repo_url:
            candidate = Path(repo_url)
            if candidate.exists() and candidate.is_dir():
                repo_path = candidate.resolve()
                validated_url = str(repo_path)
            else:
                try:
                    validated_url = self._validate_repo_url(repo_url)
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

        clone_start = datetime.now(timezone.utc)
        _add_phase("clone", PhaseStatus.RUNNING, started=clone_start)
        try:
            if repo_path is None:
                repo_path = self._clone_repo(validated_url or "", job_id, commit_sha=commit_sha)
            else:
                # Local path: run RAG ingestion if available
                try:
                    from lib.rag.ingestion import ingest_repo

                    ingest_repo(repo_path, job_id=job_id)
                except Exception as exc:
                    logger.warning("RAG ingestion failed (non-critical): %s", exc)

            clone_end = datetime.now(timezone.utc)
            _add_phase("clone", PhaseStatus.COMPLETED, started=clone_start, completed=clone_end)
        except RuntimeError as exc:
            clone_end = datetime.now(timezone.utc)
            _add_phase("clone", PhaseStatus.FAILED, started=clone_start, completed=clone_end, err=str(exc))
            return CodeSecResult(
                job_id=job_id,
                status="failed",
                error=str(exc),
                repo_url=validated_url or repo_url,
                repo_metadata=RepoMetadata(name="", total_files=0, loc=0),
                phases=phases,
                stack_detection=StackDetection(primary_language="unknown", confidence=0.0),
                security_score=SecurityScore(score=0, grade=Grade.F),
            )

        validated_url = validated_url or (str(repo_path) if repo_path is not None else repo_url)
        repo_metadata = self._get_repo_metadata(repo_path, validated_url)

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

        try:
            results = await self._run_scanners(repo_path, stack_result, _add_phase)
        except Exception as exc:
            results = {
                "stack": stack_result,
                "sast": [],
                "secrets": [],
                "dependencies": DependenciesResult(),
                "dockerfile": [],
                "sbom": SBOM(serial_number=f"urn:uuid:{uuid.uuid4()}"),
            }
            error_message = str(exc)

        if results["dependencies"].vulnerable_packages:
            vuln_map = {
                v.package.lower(): v 
                for v in results["dependencies"].vulnerable_packages
            }
            for comp in results["sbom"].components:
                if comp.name.lower() in vuln_map:
                    cve = vuln_map[comp.name.lower()].cve_id
                    if cve and cve not in comp.cve_ids:
                        comp.cve_ids.append(cve)

        # Mark scanners with missing tools as SKIPPED so failure is loud, not a silent empty result.
        def _tool_installed(name: str) -> bool:
            tool = TOOLS.get(name)
            return bool(tool and tool.enabled and shutil.which(tool.executable))

        coverage = {
            "sast": _tool_installed("semgrep") or _tool_installed("bandit"),
            "secrets": _tool_installed("gitleaks") or _tool_installed("trufflehog"),
            "dependencies": _tool_installed("pip_audit") or _tool_installed("safety") or _tool_installed("trivy"),
            "dockerfile": _tool_installed("hadolint") or _tool_installed("trivy") or _tool_installed("checkov"),
            "sbom": _tool_installed("cyclonedx") or _tool_installed("syft"),
        }
        missing = [name for name, ok in coverage.items() if not ok]
        if missing:
            logger.warning(
                "[%s] Scanners skipped (tools not installed): %s. "
                "Run `make install-tools` and `pip install -r requirements.txt`.",
                job_id, ", ".join(missing),
            )
            for name in missing:
                _add_phase(name, PhaseStatus.SKIPPED, err="tool not installed")

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
                scanner_coverage=coverage,
            )
            score_end = datetime.now(timezone.utc)
            _add_phase("scoring", PhaseStatus.COMPLETED, started=score_start, completed=score_end)
        except Exception as exc:
            score_end = datetime.now(timezone.utc)
            _add_phase("scoring", PhaseStatus.FAILED, started=score_start, completed=score_end, err=str(exc))
            security_score = SecurityScore(score=0, grade=Grade.F)
            error_message = error_message or str(exc)

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

        results["sbom"].download_url = f"/api/jobs/{job_id}/sbom/download"

        # Capture Dockerfiles for downstream agents (the clone is removed below)
        dockerfile_contents = _capture_dockerfile_contents(repo_path, stack_result)
        dockerfile_content = next(iter(dockerfile_contents.values())) if dockerfile_contents else None
        for df_rel in dockerfile_contents:
            logger.info("Captured Dockerfile for downstream agents: %s", df_rel)

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
            dockerfile_content=dockerfile_content,
            dockerfile_contents=dockerfile_contents,
        )

        logger.info(
            "CodeSec analysis complete for job %s: score=%d/100, grade=%s, findings=%d",
            job_id,
            result.security_score.score,
            result.security_score.grade.value,
            summary.sast_findings_count + summary.secrets_found_count + summary.vulnerable_dependencies_count + summary.dockerfile_issues_count,
        )
        return result

    def analyze_sync(self, repo_url: str, job_id: str | None = None, commit_sha: str | None = None) -> CodeSecResult:
        """Synchronous wrapper for analyze()."""
        return asyncio.run(self.analyze(repo_url, job_id, commit_sha))