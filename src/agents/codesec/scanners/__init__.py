"""
CodeSec Scanners Package
=========================
Contains all security scanning modules for the CodeSec agent.
Each scanner returns a standardized internal result that agent.py aggregates
into the final CodeSecResult schema.

Scanners:
    stack_detection  — Language/framework/database detection
    sast            — Static Application Security Testing (OWASP Top 10)
    secrets         — Hardcoded credential detection
    dependencies    — CVE detection in package manifests
    dockerfile      — Container security best practices
    sbom            — Software Bill of Materials generation
    scorer          — Security score calculator (0-100, grade A-F)
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def run_subprocess(
    cmd: list[str],
    cwd: Path | None = None,
    timeout: int = 300,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """
    Execute a subprocess command safely with timeout and error handling.

    Security: Never passes shell=True. All arguments are passed as a list
    to prevent shell injection. citespec-NFR-security
    """
    logger.debug("Running command: %s (cwd=%s, timeout=%d)", " ".join(cmd), cwd, timeout)
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=capture_output,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result
    except subprocess.TimeoutExpired as exc:
        logger.error("Command timed out after %ds: %s", timeout, " ".join(cmd))
        raise ScannerError(f"Scanner timeout: {' '.join(cmd)}") from exc
    except FileNotFoundError as exc:
        logger.error("Executable not found: %s", cmd[0])
        raise ScannerError(f"Tool not installed: {cmd[0]}") from exc


class ScannerError(Exception):
    """Base exception for all scanner failures."""

    pass


def find_files(repo_path: Path, patterns: tuple[str, ...], exclude: tuple[str, ...] = ()) -> list[Path]:
    """
    Recursively find files in repo_path matching any of the given patterns,
    excluding paths that match any exclude pattern.
    """
    matched: list[Path] = []
    for root, _dirs, files in os.walk(repo_path):
        root_path = Path(root)
        for filename in files:
            file_path = root_path / filename
            rel_path = file_path.relative_to(repo_path).as_posix()

            # Check excludes first
            if any(fnmatch.fnmatch(rel_path, ex) or fnmatch.fnmatch(filename, ex) for ex in exclude):
                continue

            if any(fnmatch.fnmatch(filename, pat) or fnmatch.fnmatch(rel_path, pat) for pat in patterns):
                matched.append(file_path)

    return matched


def read_file_safe(file_path: Path, max_size_mb: int = 10) -> str | None:
    """
    Safely read a file with size limits. Returns None if unreadable.
    Prevents memory exhaustion from huge files. citespec-NFR-security
    """
    try:
        size = file_path.stat().st_size
        max_bytes = max_size_mb * 1024 * 1024
        if size > max_bytes:
            logger.warning("File too large (%d MB > %d MB limit): %s", size // (1024 * 1024), max_size_mb, file_path)
            return None
        return file_path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("Could not read file %s: %s", file_path, exc)
        return None


def hash_file(file_path: Path) -> str:
    """Compute SHA-256 hash of file contents for deduplication."""
    h = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


__all__ = [
    "run_subprocess",
    "ScannerError",
    "find_files",
    "read_file_safe",
    "hash_file",
]