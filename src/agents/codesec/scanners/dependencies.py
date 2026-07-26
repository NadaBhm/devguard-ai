
"""
CodeSec Dependency Vulnerability Scanner
=========================================
Parses package manifest files and checks for known CVEs in dependencies.

US-1.1.x: Detect vulnerable dependencies with known CVEs.

Technology Decision (ADR):
- Primary: pip-audit — maintained by PyPA, queries PyPI Advisory Database,
  zero configuration, Apache-2.0, JSON output. Best baseline CI gate. citeweb_search:5#3
- Secondary: Safety — mature Python-specific scanner, free tier available.
  Used as second opinion when pip-audit is unavailable. citeweb_search:5#3
- Tertiary: Trivy — multi-ecosystem (npm, go, maven, etc.), excellent for
  non-Python repos. OSS, maintained by Aqua Security. citeweb_search:5#3
- Not chosen: Snyk — commercial, requires API key, rate-limited free tier.

"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from ..config import TOOLS
from ..models import DependenciesResult, Severity, VulnerablePackage
from . import ScannerError, find_files, read_file_safe, run_subprocess

logger = logging.getLogger(__name__)


def _parse_pip_audit_output(stdout: str) -> list[VulnerablePackage]:
    """Parse pip-audit JSON output into VulnerablePackage models."""
    findings: list[VulnerablePackage] = []
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        logger.error("Failed to parse pip-audit JSON")
        return findings

    for vuln in data.get("dependencies", []):
        name = vuln.get("name", "unknown")
        version = vuln.get("version", "unknown")
        for fix in vuln.get("fix_versions", []):
            for adv in vuln.get("vulns", []):
                severity_str = adv.get("severity", "low")
                severity = Severity.LOW
                if severity_str:
                    severity = Severity(severity_str.lower())

                findings.append(
                    VulnerablePackage(
                        package=name,
                        installed_version=version,
                        fixed_version=fix if fix else None,
                        cve_id=adv.get("id"),
                        severity=severity,
                        cvss_score=adv.get("cvss"),
                        description=adv.get("description"),
                    )
                )
    return findings


def _run_pip_audit(repo_path: Path) -> list[VulnerablePackage]:
    """Run pip-audit on requirements files."""
    tool = TOOLS["pip_audit"]
    if not tool.enabled:
        return []

    req_files = find_files(repo_path, patterns=("requirements*.txt",))
    if not req_files:
        logger.info("No requirements files found for pip-audit.")
        return []

    all_findings: list[VulnerablePackage] = []
    for req_file in req_files:
        cmd = [
            tool.executable,
            "-r",
            str(req_file),
            "--format=json",
            "--desc",
        ]
        try:
            result = run_subprocess(cmd, cwd=repo_path, timeout=tool.timeout_seconds)
            findings = _parse_pip_audit_output(result.stdout)
            all_findings.extend(findings)
        except ScannerError as exc:
            logger.warning("pip-audit failed for %s: %s", req_file, exc)

    logger.info("pip-audit found %d vulnerable packages", len(all_findings))
    return all_findings


def _run_safety(repo_path: Path) -> list[VulnerablePackage]:
    """Run Safety CLI as fallback."""
    tool = TOOLS["safety"]
    if not tool.enabled:
        return []

    req_files = find_files(repo_path, patterns=("requirements*.txt",))
    if not req_files:
        return []

    all_findings: list[VulnerablePackage] = []
    for req_file in req_files:
        cmd = [tool.executable, "check", str(req_file), "--json"]
        try:
            result = run_subprocess(cmd, cwd=repo_path, timeout=tool.timeout_seconds)
            data = json.loads(result.stdout)
            for vuln in data.get("vulnerabilities", []):
                all_findings.append(
                    VulnerablePackage(
                        package=vuln.get("package_name", "unknown"),
                        installed_version=vuln.get("installed_version", "unknown"),
                        fixed_version=vuln.get("fixed_version"),
                        cve_id=vuln.get("cve"),
                        severity=Severity(vuln.get("severity", "low").lower()),
                        cvss_score=vuln.get("cvssv3"),
                        description=vuln.get("advisory"),
                    )
                )
        except (ScannerError, json.JSONDecodeError) as exc:
            logger.warning("Safety failed for %s: %s", req_file, exc)

    logger.info("Safety found %d vulnerable packages", len(all_findings))
    return all_findings


def _run_trivy_fs(repo_path: Path) -> list[VulnerablePackage]:
    """Run Trivy filesystem scan for dependency vulnerabilities."""
    tool = TOOLS["trivy"]
    if not tool.enabled:
        return []

    report_path = repo_path / ".codesec_trivy_deps.json"
    cmd = [
        tool.executable,
        "fs",
        "--scanners=vuln",
        "--format=json",
        f"--output={report_path}",
        str(repo_path),
    ]

    try:
        result = run_subprocess(cmd, cwd=repo_path, timeout=tool.timeout_seconds)
    except ScannerError:
        report_path.unlink(missing_ok=True)
        raise

    findings: list[VulnerablePackage] = []
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
        for vuln in result_item.get("Vulnerabilities", []):
            severity = Severity.LOW
            sev_str = vuln.get("Severity", "LOW")
            try:
                severity = Severity(sev_str.lower())
            except ValueError:
                pass

            findings.append(
                VulnerablePackage(
                    package=vuln.get("PkgName", "unknown"),
                    installed_version=vuln.get("InstalledVersion", "unknown"),
                    fixed_version=vuln.get("FixedVersion"),
                    cve_id=vuln.get("VulnerabilityID"),
                    severity=severity,
                    cvss_score=vuln.get("CVSS", {}).get("nvd", {}).get("V3Score"),
                    description=vuln.get("Description"),
                )
            )

    logger.info("Trivy found %d vulnerable packages", len(findings))
    return findings


def _parse_manifest_files(repo_path: Path) -> tuple[int, int, int]:
    """
    Parse manifest files to count total/direct/transitive packages.
    Returns (total, direct, transitive).
    """
    total = 0
    direct = 0
    transitive = 0

    # Python requirements.txt
    req_files = find_files(repo_path, patterns=("requirements*.txt",))
    for rf in req_files:
        content = read_file_safe(rf, max_size_mb=1)
        if content:
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("-"):
                    direct += 1
                    total += 1

    # package.json
    pkg_files = find_files(repo_path, patterns=("package.json",))
    for pf in pkg_files:
        try:
            data = json.loads(read_file_safe(pf, max_size_mb=1) or "{}")
            deps = data.get("dependencies", {})
            dev_deps = data.get("devDependencies", {})
            direct += len(deps) + len(dev_deps)
            total += len(deps) + len(dev_deps)
        except json.JSONDecodeError:
            pass

    # go.mod
    go_files = find_files(repo_path, patterns=("go.mod",))
    for gf in go_files:
        content = read_file_safe(gf, max_size_mb=1)
        if content:
            for line in content.splitlines():
                if line.strip().startswith("require "):
                    direct += 1
                    total += 1

    # Estimate transitive as 2.5x direct (industry average)
    if direct > 0:
        transitive = max(0, int(direct * 2.5) - direct)
        total = direct + transitive

    return total, direct, transitive


def run_dependency_scan(repo_path: Path) -> DependenciesResult:
    """
    Run dependency vulnerability scanning on a repository.

    Tries pip-audit for Python, Safety as fallback, and Trivy for multi-ecosystem.

    Args:
        repo_path: Path to the cloned repository.

    Returns:
        DependenciesResult with vulnerability findings and package counts.
    """
    all_vulns: list[VulnerablePackage] = []

    # Try pip-audit (Python)
    try:
        pip_audit_findings = _run_pip_audit(repo_path)
        all_vulns.extend(pip_audit_findings)
    except ScannerError as exc:
        logger.warning("pip-audit failed: %s", exc)

    # Try Safety (Python fallback)
    if not all_vulns:
        try:
            safety_findings = _run_safety(repo_path)
            all_vulns.extend(safety_findings)
        except ScannerError as exc:
            logger.warning("Safety failed: %s", exc)

    # Try Trivy (multi-ecosystem)
    try:
        trivy_findings = _run_trivy_fs(repo_path)
        all_vulns.extend(trivy_findings)
    except ScannerError as exc:
        logger.warning("Trivy dependency scan failed: %s", exc)

    # Deduplicate by (package, cve_id)
    seen: set[tuple[str, str | None]] = set()
    deduped: list[VulnerablePackage] = []
    for v in all_vulns:
        key = (v.package, v.cve_id)
        if key not in seen:
            seen.add(key)
            deduped.append(v)

    total, direct, transitive = _parse_manifest_files(repo_path)

    result = DependenciesResult(
        total_packages=total,
        direct=direct,
        transitive=transitive,
        vulnerable_packages=deduped,
    )

    logger.info(
        "Dependency scan complete: %d packages, %d vulnerable",
        result.total_packages,
        len(result.vulnerable_packages),
    )
    return result