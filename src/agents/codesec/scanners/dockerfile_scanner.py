
"""
CodeSec Dockerfile Scanner
===========================
Checks Dockerfiles and docker-compose files for security best practices.

Technology Decision (ADR):
- Primary: Trivy config scan — comprehensive Dockerfile checks, OSS,
  maintained by Aqua Security, JSON output, integrates with our existing
  Trivy dependency scanning. citeweb_search:5#3
- Secondary: Hadolint — fast linting for Dockerfile best practices,
  but focused on style/efficiency rather than security. Used as supplement.
- Not chosen: Checkov — powerful but heavier, more suited for IaC (Terraform,
  CloudFormation) than Dockerfile-specific checks.

"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ..config import TOOLS
from ..models import DockerfileFinding, Severity
from . import ScannerError, find_files, read_file_safe, run_subprocess

logger = logging.getLogger(__name__)


def _run_trivy_config(repo_path: Path) -> list[DockerfileFinding]:
    """Run Trivy config scan on Dockerfiles."""
    tool = TOOLS["trivy"]
    if not tool.enabled:
        return []

    dockerfile_paths = find_files(
        repo_path,
        patterns=("Dockerfile*", "*.dockerfile", "docker-compose*.yml", "docker-compose*.yaml"),
    )
    if not dockerfile_paths:
        logger.info("No Dockerfiles found for Trivy config scan.")
        return []

    report_path = repo_path / ".codesec_trivy_config.json"
    cmd = [
        tool.executable,
        "config",
        "--format=json",
        f"--output={report_path}",
        str(repo_path),
    ]

    try:
        result = run_subprocess(cmd, cwd=repo_path, timeout=tool.timeout_seconds)
    except ScannerError:
        report_path.unlink(missing_ok=True)
        raise

    findings: list[DockerfileFinding] = []
    if not report_path.exists():
        return findings

    try:
        with open(report_path, "r") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        report_path.unlink(missing_ok=True)
        return findings
    finally:
        report_path.unlink(missing_ok=True)

    for result_item in data.get("Results", []):
        for misconf in result_item.get("Misconfigurations", []):
            severity = Severity.LOW
            sev_str = misconf.get("Severity", "LOW")
            try:
                severity = Severity(sev_str.lower())
            except ValueError:
                pass

            findings.append(
                DockerfileFinding(
                    rule_id=misconf.get("ID", "DS000"),
                    tool="trivy",
                    severity=severity,
                    category="dockerfile",
                    file=result_item.get("Target", ""),
                    line=misconf.get("CauseMetadata", {}).get("StartLine", 1),
                    message=misconf.get("Title", misconf.get("Description", "")),
                    snippet=misconf.get("CauseMetadata", {}).get("Code", {}).get("Lines", [""])[0] if misconf.get("CauseMetadata", {}).get("Code", {}).get("Lines") else None,
                    remediation=misconf.get("Resolution", None),
                )
            )

    logger.info("Trivy config scan found %d Dockerfile issues", len(findings))
    return findings


def _run_hadolint(repo_path: Path) -> list[DockerfileFinding]:
    """Run Hadolint on Dockerfiles for additional best-practice checks."""
    tool = TOOLS["hadolint"]
    if not tool.enabled:
        return []

    dockerfile_paths = find_files(repo_path, patterns=("Dockerfile*", "*.dockerfile"))
    if not dockerfile_paths:
        return []

    all_findings: list[DockerfileFinding] = []
    for df_path in dockerfile_paths:
        cmd = [
            tool.executable,
            "--format=json",
            str(df_path),
        ]
        try:
            result = run_subprocess(cmd, cwd=repo_path, timeout=tool.timeout_seconds)
            data = json.loads(result.stdout)
            for hit in data:
                severity = Severity.LOW
                if hit.get("level") == "error":
                    severity = Severity.HIGH
                elif hit.get("level") == "warning":
                    severity = Severity.MEDIUM

                all_findings.append(
                    DockerfileFinding(
                        rule_id=f"DL{hit.get('code', '0000')}",
                        tool="hadolint",
                        severity=severity,
                        category="dockerfile",
                        file=str(df_path.relative_to(repo_path)),
                        line=hit.get("line", 1),
                        message=hit.get("message", ""),
                        snippet=None,
                        remediation=None,
                    )
                )
        except (ScannerError, json.JSONDecodeError) as exc:
            logger.warning("Hadolint failed for %s: %s", df_path, exc)

    logger.info("Hadolint found %d Dockerfile issues", len(all_findings))
    return all_findings


def _run_builtin_checks(repo_path: Path) -> list[DockerfileFinding]:
    """
    Built-in Dockerfile security checks when no external tools are available.
    Covers critical security rules that every Dockerfile should pass.
    """
    findings: list[DockerfileFinding] = []
    dockerfile_paths = find_files(repo_path, patterns=("Dockerfile*", "*.dockerfile"))

    for df_path in dockerfile_paths:
        content = read_file_safe(df_path, max_size_mb=1)
        if not content:
            continue

        rel_path = df_path.relative_to(repo_path).as_posix()
        lines = content.splitlines()

        checks = [
            (r"^\s*USER\s+root\b", "Running as root user is a security risk", Severity.HIGH, "DS001"),
            (r"^\s*FROM\s+.*:latest\b", "Using 'latest' tag makes builds non-deterministic", Severity.MEDIUM, "DS002"),
            (r"^\s*FROM\s+scratch\b", None, None, None),  # scratch is fine
            (r"^\s*ADD\s+.*https?://", "ADD with remote URL bypasses layer caching and verification", Severity.MEDIUM, "DS003"),
            (r"^\s*EXPOSE\s+22\b", "Exposing SSH port (22) is unnecessary in containers", Severity.MEDIUM, "DS004"),
            (r"^\s*HEALTHCHECK\s+NONE", "Disabling healthchecks prevents runtime monitoring", Severity.MEDIUM, "DS005"),
            (r"^\s*ENV\s+.*PASSWORD\s*[=\s]", "Hardcoded passwords in ENV are insecure", Severity.CRITICAL, "DS006"),
            (r"^\s*ENV\s+.*SECRET\s*[=\s]", "Hardcoded secrets in ENV are insecure", Severity.CRITICAL, "DS007"),
            (r"^\s*RUN\s+.*curl\s+.*\|\s*sh", "Piping curl to shell is dangerous (supply chain risk)", Severity.HIGH, "DS008"),
            (r"^\s*RUN\s+.*wget\s+.*\|\s*sh", "Piping wget to shell is dangerous (supply chain risk)", Severity.HIGH, "DS009"),
            (r"^\s*RUN\s+.*apt-get\s+.*upgrade", "Running apt-get upgrade in Dockerfile creates non-deterministic layers", Severity.LOW, "DS010"),
        ]

        for line_num, line in enumerate(lines, start=1):
            for pattern, message, severity, rule_id in checks:
                if message is None:
                    continue
                if re.search(pattern, line, re.IGNORECASE):
                    # Check for false positives (e.g., commented lines)
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue

                    findings.append(
                        DockerfileFinding(
                            rule_id=rule_id,
                            tool="builtin",
                            severity=severity,
                            category="dockerfile",
                            file=rel_path,
                            line=line_num,
                            message=message,
                            snippet=line.strip(),
                            remediation=_get_remediation(rule_id),
                        )
                    )

    logger.info("Built-in Dockerfile checks found %d issues", len(findings))
    return findings


def _get_remediation(rule_id: str) -> str | None:
    """Get remediation advice for built-in Dockerfile rules."""
    remediations = {
        "DS001": "Add 'USER appuser' after creating a non-root user with 'RUN useradd -m appuser'",
        "DS002": "Pin to a specific version tag, e.g., 'FROM python:3.12-slim'",
        "DS003": "Use 'curl' + 'RUN' with checksum verification instead of ADD",
        "DS004": "Remove 'EXPOSE 22' unless the container is an SSH bastion",
        "DS005": "Add a proper HEALTHCHECK instruction",
        "DS006": "Use Docker secrets or environment variables injected at runtime",
        "DS007": "Use Docker secrets or environment variables injected at runtime",
        "DS008": "Download the script, verify checksum, then execute",
        "DS009": "Download the script, verify checksum, then execute",
        "DS010": "Pin specific package versions instead of upgrading",
    }
    return remediations.get(rule_id)


def run_dockerfile_scan(repo_path: Path) -> list[DockerfileFinding]:
    """
    Run Dockerfile security scanning.

    Tries Trivy config scan first, then Hadolint, then falls back to
    built-in regex-based checks.

    Args:
        repo_path: Path to the cloned repository.

    Returns:
        Combined list of DockerfileFinding objects.
    """
    all_findings: list[DockerfileFinding] = []

    # Try Trivy config
    try:
        trivy_findings = _run_trivy_config(repo_path)
        all_findings.extend(trivy_findings)
    except ScannerError as exc:
        logger.warning("Trivy config scan failed: %s", exc)

    # Try Hadolint
    try:
        hadolint_findings = _run_hadolint(repo_path)
        all_findings.extend(hadolint_findings)
    except ScannerError as exc:
        logger.warning("Hadolint failed: %s", exc)

    # Fallback to built-in checks
    if not all_findings:
        builtin_findings = _run_builtin_checks(repo_path)
        all_findings.extend(builtin_findings)

    # Deduplicate by (file, line, rule_id)
    seen: set[tuple[str, int, str]] = set()
    deduped: list[DockerfileFinding] = []
    for f in all_findings:
        key = (f.file, f.line, f.rule_id)
        if key not in seen:
            seen.add(key)
            deduped.append(f)

    logger.info("Dockerfile scan complete: %d unique findings", len(deduped))
    return deduped