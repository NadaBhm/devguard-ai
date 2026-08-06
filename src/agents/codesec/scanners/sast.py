"""
CodeSec SAST Scanner
=====================
Runs static analysis for OWASP Top 10 vulnerabilities using Semgrep
(with Bandit fallback), parses output, and maps findings to OWASP/CWE categories.

US-1.1.3: As a security-conscious developer, I want to detect vulnerabilities
in my code so that I can fix them before deployment.

Technology Decision (ADR):
- Primary: Semgrep — fast, multi-language, 710+ Pro rules for Python, excellent
  Python library maturity (semgrep package on PyPI), OSS license (LGPL-2.1).
  Benchmark F1: 69.4% on OWASP Benchmark. citeweb_search:5#7
- Fallback: Bandit — Python-specific, lightweight, Apache-2.0, good for
  Python-only repos when Semgrep is unavailable.
- Not chosen: CodeQL — higher accuracy (F1 74.4%) but requires complex setup,
  GitHub dependency, and is slower. Overkill for our 5-minute pipeline target. citeweb_search:5#7

"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ..config import OWASP_CWE_MAP, TOOLS
from ..models import SASTFinding, Severity
from . import ScannerError, find_files, read_file_safe, run_subprocess

logger = logging.getLogger(__name__)


def _map_cwe_to_owasp(cwe_id: str | None) -> str | None:
    """Map a CWE ID to its OWASP Top 10 2021 category."""
    if not cwe_id:
        return None
    cwe_upper = cwe_id.upper()
    for owasp_cat, cwe_list in OWASP_CWE_MAP.items():
        if cwe_upper in [c.upper() for c in cwe_list]:
            return owasp_cat
    return None


def _normalize_severity(sev: str | None) -> Severity:
    """Normalize various severity strings to enum values for compatibility."""
    mapping = {
        "critical": Severity.CRITICAL,
        "error": Severity.HIGH,
        "high": Severity.HIGH,
        "warning": Severity.MEDIUM,
        "medium": Severity.MEDIUM,
        "low": Severity.LOW,
        "info": Severity.INFO,
        "informational": Severity.INFO,
        "unknown": Severity.LOW,
        "": Severity.LOW,
    }
    return mapping.get(str(sev or "").strip().lower(), Severity.LOW)


def _parse_semgrep_output(output: Any) -> list[SASTFinding]:
    """Parse Semgrep JSON output into a list of SASTFinding objects."""
    if isinstance(output, str):
        try:
            data = json.loads(output)
        except json.JSONDecodeError as exc:
            raise exc
    elif isinstance(output, dict):
        data = output
    else:
        raise ValueError("Semgrep output must be a JSON string or dictionary")

    findings: list[SASTFinding] = []
    for hit in data.get("results", []):
        extra = hit.get("extra", {})
        metadata = extra.get("metadata", {})
        cwe_value = None
        cwe_list = metadata.get("cwe", [])
        if cwe_list:
            cwe_match = re.search(r"CWE-\d+", str(cwe_list[0]))
            if cwe_match:
                cwe_value = cwe_match.group(0)

        severity = _normalize_severity(extra.get("severity", "low"))
        owasp = _map_cwe_to_owasp(cwe_value)
        finding = SASTFinding(
            rule_id=hit.get("check_id", "unknown"),
            tool="semgrep",
            severity=severity,
            category="owasp-top10" if owasp != "Unknown" else "security",
            owasp_category=owasp,
            cwe_id=cwe_value,
            check_id=hit.get("check_id", "unknown"),
            cwe=cwe_value,
            file=hit.get("path", ""),
            line=hit.get("start", {}).get("line", 1),
            column=hit.get("start", {}).get("col", 1),
            message=extra.get("message", ""),
            snippet=extra.get("lines", ""),
            remediation=metadata.get("fix") or metadata.get("remediation"),
        )
        findings.append(finding)

    return findings


def _run_semgrep(repo_path: Path) -> list[SASTFinding]:
    """
    Execute Semgrep and parse JSON output into SASTFinding models.

    Args:
        repo_path: Path to the cloned repository.

    Returns:
        List of SASTFinding objects.
    """
    tool = TOOLS["semgrep"]
    if not tool.enabled:
        logger.info("Semgrep is disabled in configuration.")
        return []

    # Find source files to scan
    source_files = find_files(
        repo_path,
        patterns=("*.py", "*.js", "*.ts", "*.go", "*.java", "*.rb", "*.php"),
        exclude=("test_", "tests/", "*_test.py", "conftest.py", "venv/", ".venv/", "node_modules/"),
    )

    if not source_files:
        logger.info("No source files found for Semgrep scan.")
        return []

    # Build file list (limit to avoid command-line length issues)
    file_list_path = repo_path / ".codesec_semgrep_files.txt"
    with open(file_list_path, "w") as f:
        for sf in source_files[:5000]:
            f.write(str(sf) + "\n")

    cmd = [
        tool.executable,
        "--config=auto",
        "--json",
        "--quiet",
        f"--include={str(file_list_path)}",
        str(repo_path),
    ]

    try:
        result = run_subprocess(cmd, cwd=repo_path, timeout=tool.timeout_seconds)
    except ScannerError:
        # Clean up temp file
        file_list_path.unlink(missing_ok=True)
        return []
    finally:
        file_list_path.unlink(missing_ok=True)

    if result.returncode not in (0, 1):  # Semgrep returns 1 when findings exist
        logger.warning("Semgrep exited with code %d: %s", result.returncode, result.stderr)
        return []

    findings: list[SASTFinding] = []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse Semgrep JSON output: %s", exc)
        return []

    for hit in data.get("results", []):
        cwe_id = None
        extra = hit.get("extra", {})
        metadata = extra.get("metadata", {})

        # Extract CWE from metadata
        cwe_list = metadata.get("cwe", [])
        if cwe_list:
            cwe_match = re.search(r"CWE-\d+", str(cwe_list[0]))
            if cwe_match:
                cwe_id = cwe_match.group(0)

        owasp_cat = _map_cwe_to_owasp(cwe_id)
        severity = _normalize_severity(extra.get("severity", "low"))

        finding = SASTFinding(
            rule_id=hit.get("check_id", "unknown"),
            tool="semgrep",
            severity=severity,
            category="owasp-top10" if owasp_cat else "security",
            owasp_category=owasp_cat,
            cwe_id=cwe_id,
            file=hit.get("path", ""),
            line=hit.get("start", {}).get("line", 1),
            column=hit.get("start", {}).get("col", 1),
            message=extra.get("message", ""),
            snippet=extra.get("lines", ""),
            remediation=metadata.get("fix", None) or metadata.get("remediation", None),
        )
        findings.append(finding)

    logger.info("Semgrep found %d findings", len(findings))
    return findings


def _run_bandit(repo_path: Path) -> list[SASTFinding]:
    """
    Execute Bandit as fallback SAST for Python repositories.

    Args:
        repo_path: Path to the cloned repository.

    Returns:
        List of SASTFinding objects.
    """
    tool = TOOLS["bandit"]
    if not tool.enabled:
        return []

    cmd = [
        tool.executable,
        "-r",
        str(repo_path),
        "-f",
        "json",
        "-x",
        "tests,test,venv,.venv",
    ]

    try:
        result = run_subprocess(cmd, cwd=repo_path, timeout=tool.timeout_seconds)
    except ScannerError:
        return []

    findings: list[SASTFinding] = []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.error("Failed to parse Bandit JSON output")
        return []

    for hit in data.get("results", []):
        cwe_id = hit.get("cwe", None)
        if cwe_id:
            cwe_id = f"CWE-{cwe_id}"

        owasp_cat = _map_cwe_to_owasp(cwe_id)
        severity = _normalize_severity(hit.get("issue_severity", "low"))

        finding = SASTFinding(
            rule_id=hit.get("test_id", "bandit.unknown"),
            tool="bandit",
            severity=severity,
            category="owasp-top10" if owasp_cat else "security",
            owasp_category=owasp_cat,
            cwe_id=cwe_id,
            file=hit.get("filename", ""),
            line=hit.get("line_number", 1),
            column=1,
            message=hit.get("issue_text", ""),
            snippet=hit.get("code", ""),
            remediation=None,
        )
        findings.append(finding)

    logger.info("Bandit found %d findings", len(findings))
    return findings


def run_sast(repo_path: Path | str) -> list[SASTFinding]:
    """
    Run SAST scanning on a repository.

    Tries Semgrep first (multi-language, comprehensive), falls back to Bandit
    for Python-only repos if Semgrep fails or is disabled.

    Args:
        repo_path: Path to the cloned repository.

    Returns:
        SASTResult containing the combined findings and counts.
    """
    repo_path = Path(repo_path)
    all_findings: list[SASTFinding] = []
    scan_error: str | None = None

    # Try Semgrep
    try:
        semgrep_findings = _run_semgrep(repo_path)
        all_findings.extend(semgrep_findings)
    except ScannerError as exc:
        scan_error = str(exc)
        logger.warning("Semgrep failed: %s. Will attempt Bandit fallback.", exc)

    # Fallback to Bandit if Semgrep produced no findings or failed
    if not all_findings:
        try:
            bandit_findings = _run_bandit(repo_path)
            all_findings.extend(bandit_findings)
        except ScannerError as exc:
            scan_error = scan_error or str(exc)
            logger.warning("Bandit fallback also failed: %s", exc)

    # Deduplicate by (file, line, rule_id)
    seen: set[tuple[str, int, str | None]] = set()
    deduped: list[SASTFinding] = []
    for f in all_findings:
        key = (f.file, f.line, f.rule_id)
        if key not in seen:
            seen.add(key)
            deduped.append(f)

    logger.info("SAST scan complete: %d unique findings", len(deduped))
    return deduped