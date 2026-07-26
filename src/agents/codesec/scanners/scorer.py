
"""
CodeSec Security Score Calculator
===================================
Calculates a 0-100 security score with letter grade (A-F), severity counts,
per-category breakdown, and prioritized recommendations.

US-1.1.5: As a tech lead, I want a security score so that I can prioritize fixes.

Design Decisions:
- Weighted scoring: SAST (25) > Secrets (20) > Dependencies (20) > Dockerfile (15)
  > SBOM (10) > Stack Detection (10). Sums to 100.
- Penalty curve: exponential decay per additional finding to avoid single-repo
  with 1000 low-severity issues scoring 0 (which would be misleading).
- Grade thresholds: A>=90, B>=80, C>=70, D>=60, E>=50, F<50.
- Recommendations are generated from findings sorted by severity * exploitability.

"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..config import (
    GRADE_THRESHOLDS,
    PENALTY_BASE,
    PENALTY_DECAY,
    SCORING_WEIGHTS,
    SEVERITY_MULTIPLIERS,
)
from ..models import (
    DockerfileFinding,
    Grade,
    SASTFinding,
    SBOM,
    ScoreBreakdown,
    Secret,
    SecurityScore,
    Severity,
    SeverityCounts,
    StackDetection,
    VulnerablePackage,
)

logger = logging.getLogger(__name__)


def _calculate_category_score(
    findings: list[Any],
    severity_attr: str = "severity",
    max_score: int = 100,
) -> int:
    """
    Calculate a category score starting from max_score and applying penalties.

    Args:
        findings: List of finding objects with a severity attribute.
        severity_attr: Attribute name to access severity on each finding.
        max_score: Maximum possible score for this category.

    Returns:
        Integer score clamped to [0, max_score].
    """
    if not findings:
        return max_score

    # Count findings by severity
    severity_counts: dict[str, int] = {}
    for finding in findings:
        sev = getattr(finding, severity_attr, Severity.LOW)
        if isinstance(sev, Severity):
            sev = sev.value
        severity_counts[sev] = severity_counts.get(sev, 0) + 1

    total_penalty = 0.0
    for severity, count in severity_counts.items():
        base = PENALTY_BASE.get(severity, 1.0)
        multiplier = SEVERITY_MULTIPLIERS.get(severity, 1.0)
        # Apply decay: first finding = full penalty, subsequent = decayed
        for i in range(count):
            penalty = base * multiplier * (PENALTY_DECAY ** i)
            total_penalty += penalty

    score = max_score - total_penalty
    return max(0, int(score))


def _calculate_stack_detection_score(stack: StackDetection) -> int:
    """Score stack detection based on confidence and completeness."""
    if not stack or stack.confidence <= 0:
        return 0

    base = int(stack.confidence * 100)
    # Penalize if critical fields are missing
    missing = sum(
        1 for field in [stack.primary_language, stack.frameworks, stack.database, stack.build_tool]
        if not field or field == "unknown"
    )
    penalty = missing * 8
    return max(0, base - penalty)


def _calculate_sbom_score(sbom: SBOM) -> int:
    """Score SBOM quality based on component count and license coverage."""
    if not sbom or sbom.components_count == 0:
        return 0

    # Base score: full if we have components
    score = 100

    # Penalty for missing license info
    components_without_license = sum(
        1 for c in sbom.components if not c.licenses
    )
    if sbom.components_count > 0:
        missing_ratio = components_without_license / sbom.components_count
        score -= int(missing_ratio * 30)

    return max(0, score)


def _generate_recommendations(
    sast_findings: list[SASTFinding],
    secrets: list[Secret],
    vulnerable_packages: list[VulnerablePackage],
    dockerfile_findings: list[DockerfileFinding],
) -> list[str]:
    """
    Generate prioritized, human-readable recommendations.

    Sorts by severity (critical first) and groups by type for clarity.
    """
    recommendations: list[str] = []

    # SAST recommendations
    critical_sast = [f for f in sast_findings if f.severity == Severity.CRITICAL]
    high_sast = [f for f in sast_findings if f.severity == Severity.HIGH]
    if critical_sast:
        by_rule: dict[str, list[SASTFinding]] = {}
        for f in critical_sast:
            by_rule.setdefault(f.rule_id, []).append(f)
        for rule_id, findings in by_rule.items():
            locations = ", ".join(f"{f.file}:{f.line}" for f in findings[:3])
            if len(findings) > 3:
                locations += f" (+{len(findings) - 3} more)"
            recommendations.append(f"Fix {len(findings)} critical {rule_id} issue(s) at {locations}")
    if high_sast:
        by_rule = {}
        for f in high_sast:
            by_rule.setdefault(f.rule_id, []).append(f)
        for rule_id, findings in by_rule.items():
            locations = ", ".join(f"{f.file}:{f.line}" for f in findings[:3])
            if len(findings) > 3:
                locations += f" (+{len(findings) - 3} more)"
            recommendations.append(f"Fix {len(findings)} high {rule_id} issue(s) at {locations}")

    # Secrets recommendations
    critical_secrets = [s for s in secrets if s.type and "password" in s.type.lower()]
    high_secrets = [s for s in secrets if s not in critical_secrets]
    if critical_secrets:
        files = ", ".join(set(s.file for s in critical_secrets[:3]))
        recommendations.append(f"Remove {len(critical_secrets)} hardcoded password(s) from {files}")
    if high_secrets:
        files = ", ".join(set(s.file for s in high_secrets[:3]))
        recommendations.append(f"Remove {len(high_secrets)} hardcoded secret(s) from {files}")

    # Dependency recommendations
    critical_deps = [d for d in vulnerable_packages if d.severity == Severity.CRITICAL]
    high_deps = [d for d in vulnerable_packages if d.severity == Severity.HIGH]
    if critical_deps:
        pkgs = ", ".join(f"{d.package} (CVE: {d.cve_id})" for d in critical_deps[:3])
        recommendations.append(f"Update critical vulnerable packages: {pkgs}")
    if high_deps:
        pkgs = ", ".join(f"{d.package} (CVE: {d.cve_id})" for d in high_deps[:3])
        recommendations.append(f"Update high-risk vulnerable packages: {pkgs}")

    # Dockerfile recommendations
    critical_df = [d for d in dockerfile_findings if d.severity == Severity.CRITICAL]
    high_df = [d for d in dockerfile_findings if d.severity == Severity.HIGH]
    if critical_df:
        files = ", ".join(set(f"{d.file}:{d.line}" for d in critical_df[:3]))
        recommendations.append(f"Fix {len(critical_df)} critical Dockerfile issue(s) at {files}")
    if high_df:
        files = ", ".join(set(f"{d.file}:{d.line}" for d in high_df[:3]))
        recommendations.append(f"Fix {len(high_df)} high Dockerfile issue(s) at {files}")

    # Cap recommendations at 10 for readability
    return recommendations[:10]


def calculate_score(
    sast_findings: list[SASTFinding],
    secrets: list[Secret],
    vulnerable_packages: list[VulnerablePackage],
    dockerfile_findings: list[DockerfileFinding],
    sbom: SBOM,
    stack_detection: StackDetection,
) -> SecurityScore:
    """
    Calculate the overall security score and grade.

    Args:
        sast_findings: Results from SAST scanner.
        secrets: Results from secrets scanner.
        vulnerable_packages: Results from dependency scanner.
        dockerfile_findings: Results from Dockerfile scanner.
        sbom: Generated SBOM.
        stack_detection: Stack detection result.

    Returns:
        SecurityScore with score, grade, breakdown, severity counts, and recommendations.
    """
    # Calculate per-category scores (0-100 each)
    sast_score = _calculate_category_score(sast_findings, max_score=100)
    secrets_score = _calculate_category_score(secrets, max_score=100)
    deps_score = _calculate_category_score(vulnerable_packages, max_score=100)
    dockerfile_score = _calculate_category_score(dockerfile_findings, max_score=100)
    sbom_score = _calculate_sbom_score(sbom)
    stack_score = _calculate_stack_detection_score(stack_detection)

    # Apply weights
    breakdown = ScoreBreakdown(
        sast=int(sast_score * SCORING_WEIGHTS["sast"] / 100),
        secrets=int(secrets_score * SCORING_WEIGHTS["secrets"] / 100),
        dependencies=int(deps_score * SCORING_WEIGHTS["dependencies"] / 100),
        dockerfile=int(dockerfile_score * SCORING_WEIGHTS["dockerfile"] / 100),
        sbom=int(sbom_score * SCORING_WEIGHTS["sbom"] / 100),
        stack_detection=int(stack_score * SCORING_WEIGHTS["stack_detection"] / 100),
    )

    total_score = (
        breakdown.sast
        + breakdown.secrets
        + breakdown.dependencies
        + breakdown.dockerfile
        + breakdown.sbom
        + breakdown.stack_detection
    )

    has_no_findings = not (sast_findings or secrets or vulnerable_packages or dockerfile_findings)
    has_complete_stack = (
        bool(stack_detection.primary_language and stack_detection.primary_language != "unknown")
        and stack_detection.confidence >= 0.9
    )
    if has_no_findings and has_complete_stack:
        total_score = 100

    # Determine grade
    grade = Grade.F
    for threshold, letter in GRADE_THRESHOLDS:
        if total_score >= threshold:
            grade = Grade(letter)
            break

    # Severity counts across all findings
    severity_counts = SeverityCounts()
    all_findings = (
        [(f.severity.value if isinstance(f.severity, Severity) else f.severity) for f in sast_findings]
        + [(s.severity.value if isinstance(s.severity, Severity) else s.severity) for s in secrets]
        + [(d.severity.value if isinstance(d.severity, Severity) else d.severity) for d in vulnerable_packages]
        + [(df.severity.value if isinstance(df.severity, Severity) else df.severity) for df in dockerfile_findings]
    )
    for sev in all_findings:
        if sev == "critical":
            severity_counts.critical += 1
        elif sev == "high":
            severity_counts.high += 1
        elif sev == "medium":
            severity_counts.medium += 1
        elif sev == "low":
            severity_counts.low += 1
        elif sev == "info":
            severity_counts.info += 1

    recommendations = _generate_recommendations(
        sast_findings, secrets, vulnerable_packages, dockerfile_findings
    )

    score = SecurityScore(
        score=total_score,
        grade=grade,
        max_score=100,
        breakdown=breakdown,
        severity_counts=severity_counts,
        recommendations=recommendations,
    )

    logger.info(
        "Security score calculated: %d/100 (Grade %s), Critical=%d, High=%d, Medium=%d, Low=%d",
        score.score,
        score.grade.value,
        score.severity_counts.critical,
        score.severity_counts.high,
        score.severity_counts.medium,
        score.severity_counts.low,
    )
    return score