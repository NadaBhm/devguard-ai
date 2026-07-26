"""
CodeSec Secrets Scanner
========================
Detects hardcoded credentials and sensitive values in repository files.

US-1.1.4: As a user, I want secrets detection with >80% recall.

Author: Generated
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from ..models import Secret, Severity
from . import ScannerError, find_files, read_file_safe, run_subprocess

logger = logging.getLogger(__name__)

SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "aws_access_key_id": re.compile(r"AKIA[0-9A-Z]{16}"),
    "aws_secret_access_key": re.compile(r"(?i)aws_secret_access_key\s*=\s*[\"']?([A-Za-z0-9/+=]{40})[\"']?"),
    "generic_api_key": re.compile(r"(?i)(api_key|apikey|secret|token)\s*[=:]\s*[\"']?([A-Za-z0-9_\-]{16,64})[\"']?"),
    "private_key": re.compile(r"-----BEGIN (RSA|EC|OPENSSH|PGP) PRIVATE KEY-----"),
}

IGNORE_PATTERNS = [
    re.compile(r"test", re.IGNORECASE),
    re.compile(r"example", re.IGNORECASE),
    re.compile(r"changeme", re.IGNORECASE),
    re.compile(r"dummy", re.IGNORECASE),
]


def _mask_value(value: str) -> str:
    if len(value) <= 6:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def _is_test_value(candidate: str) -> bool:
    return any(pattern.search(candidate) for pattern in IGNORE_PATTERNS)


def _parse_secret_match(secret_type: str, match: re.Match[str], file_path: Path, line_number: int) -> Secret:
    value = match.group(1) if match.lastindex else match.group(0)
    masked = _mask_value(value)
    return Secret(
        type=secret_type,
        tool="regex-fallback",
        file=file_path.relative_to(file_path.parents[0] if file_path.parts else file_path).as_posix(),
        line=line_number,
        column=max(1, match.start(1) + 1 if match.lastindex else match.start(0) + 1),
        value_preview=masked,
        severity=Severity.HIGH,
        confidence=0.8,
        remediation="Remove hardcoded secrets and use a secure secret manager.",
    )


def _run_regex_fallback(repo_path: Path) -> list[Secret]:
    findings: list[Secret] = []
    files = find_files(repo_path, patterns=("*",), exclude=("*.pyc", "*.lock", "node_modules/*", "venv/*", ".venv/*"))
    for file_path in files:
        content = read_file_safe(file_path, max_size_mb=1)
        if not content:
            continue

        for i, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            for secret_type, pattern in SECRET_PATTERNS.items():
                for match in pattern.finditer(line):
                    candidate = match.group(1) if match.lastindex else match.group(0)
                    if candidate and not _is_test_value(candidate):
                        findings.append(_parse_secret_match(secret_type, match, file_path, i))
    return findings


def run_secrets_scan(repo_path: Path) -> list[Secret]:
    """Run secrets detection on a repository."""
    all_findings: list[Secret] = []

    # External tool fallback: try GitLeaks if installed.
    try:
        result = run_subprocess(["gitleaks", "detect", "--no-git", "--report-format=json", "--path=."] , cwd=repo_path, timeout=60)
        if result.returncode == 0 and result.stdout:
            try:
                import json

                data = json.loads(result.stdout)
                for item in data:
                    findings = item.get("finding", item)
                    file_path = findings.get("file") or findings.get("File") or ""
                    line = int(findings.get("line", 1)) if findings.get("line") else 1
                    value = findings.get("line") if findings.get("line") else None
                    all_findings.append(
                        Secret(
                            type=findings.get("rule", "secret"),
                            tool="gitleaks",
                            file=file_path,
                            line=line,
                            column=1,
                            value_preview=_mask_value(str(value)) if value else None,
                            severity=Severity.HIGH,
                            confidence=0.8,
                        )
                    )
            except Exception:
                logger.warning("Failed to parse gitleaks output; using regex fallback.")
    except ScannerError:
        logger.info("GitLeaks not available; using regex fallback for secrets detection.")

    if not all_findings:
        all_findings = _run_regex_fallback(repo_path)

    return all_findings
