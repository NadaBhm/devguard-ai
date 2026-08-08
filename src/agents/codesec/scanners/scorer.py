"""
CodeSec Security Score Calculator — Refined (T-4.7)
====================================================
Calculates a 0-100 security score with letter grade (A-F), severity counts,
per-category breakdown, and prioritized recommendations.

Refinements Sprint 4:
- Grade thresholds aligned with industry standard (A-D + F, no E).
- Penalty curve capped at 70% per category with floor at 15/100.
- Diminishing returns for repeated findings of same severity.
- SBOM scoring includes vulnerable component detection.
- Stack detection rewards complete stack profiles.
- Recommendations ranked by priority score (severity × exploitability × ease_of_fix).

US-1.1.5: As a tech lead, I want a security score so that I can prioritize fixes.
"""

from __future__ import annotations

import logging
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

# ---------------------------------------------------------------------------
# Refined defaults (override via config.py if desired)
# ---------------------------------------------------------------------------
DEFAULT_MAX_PENALTY_RATIO = 0.70   # A category can lose at most 70% of its max
DEFAULT_MIN_CATEGORY_SCORE = 15    # Floor per category (avoid score=0 on lows)
DEFAULT_PENALTY_DECAY = 0.75       # Diminishing returns per repeated finding

# Priority weights for recommendations: severity × exploitability × ease_of_fix
EASE_OF_FIX = {
    "secrets": 1.0,      # Easy: delete or env-var
    "dockerfile": 0.9,   # Easy: edit Dockerfile
    "dependencies": 0.8, # Medium: bump version
    "sast": 0.6,         # Hard: refactor code
}

EXPLOITABILITY = {
    "secrets": 1.0,      # Always exploitable if leaked
    "sast": 0.9,         # Direct code vulnerability
    "dependencies": 0.8, # Known CVEs often have PoCs
    "dockerfile": 0.5,   # Config-level, needs access
}


def _calculate_category_score(
    findings: list[Any],
    severity_attr: str = "severity",
    max_score: int = 100,
    executed: bool = True,
) -> int:
    """
    Calculate a category score with capped penalties and diminishing returns.

    Logic:
        - Each finding applies a base penalty × severity multiplier.
        - Repeated findings of same severity decay by 25% each (0.75^n).
        - Total penalty is capped at 70% of max_score.
        - Score never drops below 15 (avoids score=0 from noise).

    `executed=False` means the scanner in question never actually ran (its
    tool is missing or disabled), so an empty finding list is NOT evidence of
    a clean category. Such a category contributes 0 rather than full marks,
    preventing a misleadingly high score when scanners are unavailable.
    """
    if not executed:
        return 0
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
        # Apply decay: first finding = full, subsequent = decayed
        for i in range(count):
            penalty = base * multiplier * (DEFAULT_PENALTY_DECAY ** i)
            total_penalty += penalty

    # Cap penalty to avoid score collapse from many low-severity findings
    max_penalty = max_score * DEFAULT_MAX_PENALTY_RATIO
    total_penalty = min(total_penalty, max_penalty)

    score = max_score - total_penalty
    # Hard floor: even a repo with many findings keeps some score
    score = max(score, DEFAULT_MIN_CATEGORY_SCORE)
    return int(score)


def _calculate_stack_detection_score(stack: StackDetection) -> int:
    """
    Score stack detection based on confidence and field completeness.

    Rewards:
        - High confidence (>0.9) with all fields detected = 100
        - Each missing critical field costs 10 points
        - Low confidence (<0.5) is heavily penalized
    """
    if not stack or stack.confidence <= 0:
        return 0

    base = int(stack.confidence * 100)

    # Critical fields for a complete stack profile
    critical_fields = [
        stack.primary_language,
        stack.frameworks,
        stack.database,
        stack.build_tool,
    ]
    missing = sum(1 for f in critical_fields if not f or f == "unknown")
    penalty = missing * 10

    score = max(0, base - penalty)

    # Bonus for complete, high-confidence detection
    if missing == 0 and stack.confidence >= 0.9:
        score = min(100, score + 5)

    return score


def _calculate_sbom_score(sbom: SBOM) -> int:
    """
    Score SBOM quality based on component coverage, licenses, and known vulns.

    Penalties:
        - Missing license info: up to -30
        - Very few components (<5): -20 (likely incomplete scan)
        - Known vulnerable components: -10 each (capped at -30)
    """
    if not sbom or sbom.components_count == 0:
        return 0

    score = 100

    # Penalty for missing licenses
    components_without_license = sum(
        1 for c in sbom.components if not getattr(c, "licenses", None)
    )
    if sbom.components_count > 0:
        missing_ratio = components_without_license / sbom.components_count
        score -= int(missing_ratio * 30)

    # Penalty for incomplete SBOM (suspiciously few components)
    if sbom.components_count < 5:
        score -= 20

    # Penalty for known vulnerable components in SBOM
    vulnerable_components = sum(
        1 for c in sbom.components
        if getattr(c, "vulnerabilities", None) or getattr(c, "cve_ids", None)
    )
    score -= min(vulnerable_components * 10, 30)

    return max(0, score)


def _priority_score(category: str, severity: str) -> float:
    """Calculate recommendation priority: severity × exploitability × ease_of_fix."""
    sev_weights = {"critical": 5.0, "high": 3.0, "medium": 2.0, "low": 1.0, "info": 0.5}
    sev = sev_weights.get(severity.lower(), 1.0)
    exp = EXPLOITABILITY.get(category, 0.5)
    ease = EASE_OF_FIX.get(category, 0.5)
    return sev * exp * ease


def _generate_recommendations(
    sast_findings: list[SASTFinding],
    secrets: list[Secret],
    vulnerable_packages: list[VulnerablePackage],
    dockerfile_findings: list[DockerfileFinding],
) -> list[str]:
    """
    Generate prioritized, human-readable recommendations.

    Sorted by priority_score (descending) so the most impactful + easiest fixes
    appear first.
    """
    recs_with_priority: list[tuple[float, str]] = []

    # --- Secrets (highest priority: easy to fix, always exploitable) ---
    critical_secrets = [s for s in secrets if s.type and "password" in s.type.lower()]
    high_secrets = [s for s in secrets if s not in critical_secrets]

    if critical_secrets:
        files = ", ".join(set(s.file for s in critical_secrets[:3]))
        msg = f"Remove {len(critical_secrets)} hardcoded password(s) from {files}"
        recs_with_priority.append((_priority_score("secrets", "critical"), msg))
    if high_secrets:
        files = ", ".join(set(s.file for s in high_secrets[:3]))
        msg = f"Remove {len(high_secrets)} hardcoded secret(s) from {files}"
        recs_with_priority.append((_priority_score("secrets", "high"), msg))

    # --- SAST Critical ---
    critical_sast = [f for f in sast_findings if f.severity == Severity.CRITICAL]
    if critical_sast:
        by_rule: dict[str, list[SASTFinding]] = {}
        for f in critical_sast:
            by_rule.setdefault(f.rule_id or "unknown", []).append(f)
        for rule_id, findings in by_rule.items():
            locations = ", ".join(f"{f.file}:{f.line}" for f in findings[:3])
            if len(findings) > 3:
                locations += f" (+{len(findings) - 3} more)"
            msg = f"Fix {len(findings)} critical {rule_id} issue(s) at {locations}"
            recs_with_priority.append((_priority_score("sast", "critical"), msg))

    # --- SAST High ---
    high_sast = [f for f in sast_findings if f.severity == Severity.HIGH]
    if high_sast:
        by_rule = {}
        for f in high_sast:
            by_rule.setdefault(f.rule_id or "unknown", []).append(f)
        for rule_id, findings in by_rule.items():
            locations = ", ".join(f"{f.file}:{f.line}" for f in findings[:3])
            if len(findings) > 3:
                locations += f" (+{len(findings) - 3} more)"
            msg = f"Fix {len(findings)} high {rule_id} issue(s) at {locations}"
            recs_with_priority.append((_priority_score("sast", "high"), msg))

    # --- Dependencies ---
    critical_deps = [d for d in vulnerable_packages if d.severity == Severity.CRITICAL]
    high_deps = [d for d in vulnerable_packages if d.severity == Severity.HIGH]
    if critical_deps:
        pkgs = ", ".join(f"{d.package} (CVE: {d.cve_id})" for d in critical_deps[:3])
        msg = f"Update critical vulnerable packages: {pkgs}"
        recs_with_priority.append((_priority_score("dependencies", "critical"), msg))
    if high_deps:
        pkgs = ", ".join(f"{d.package} (CVE: {d.cve_id})" for d in high_deps[:3])
        msg = f"Update high-risk vulnerable packages: {pkgs}"
        recs_with_priority.append((_priority_score("dependencies", "high"), msg))

    # --- Dockerfile ---
    critical_df = [d for d in dockerfile_findings if d.severity == Severity.CRITICAL]
    high_df = [d for d in dockerfile_findings if d.severity == Severity.HIGH]
    if critical_df:
        files = ", ".join(set(f"{d.file}:{d.line}" for d in critical_df[:3]))
        msg = f"Fix {len(critical_df)} critical Dockerfile issue(s) at {files}"
        recs_with_priority.append((_priority_score("dockerfile", "critical"), msg))
    if high_df:
        files = ", ".join(set(f"{d.file}:{d.line}" for d in high_df[:3]))
        msg = f"Fix {len(high_df)} high Dockerfile issue(s) at {files}"
        recs_with_priority.append((_priority_score("dockerfile", "high"), msg))

    # Sort by priority score descending (highest impact/easiest first)
    recs_with_priority.sort(key=lambda x: x[0], reverse=True)

    # Return top 10, deduplicated
    seen: set[str] = set()
    result: list[str] = []
    for _, msg in recs_with_priority:
        if msg not in seen:
            seen.add(msg)
            result.append(msg)
        if len(result) >= 10:
            break
    return result


def calculate_score(
    sast_findings: list[SASTFinding],
    secrets: list[Secret],
    vulnerable_packages: list[VulnerablePackage],
    dockerfile_findings: list[DockerfileFinding],
    sbom: SBOM,
    stack_detection: StackDetection,
    scanner_coverage: dict[str, bool] | None = None,
) -> SecurityScore:
    """
    Calculate the overall security score and grade.

    Refined logic:
        - Category penalties are capped and floored (realistic scores).
        - Grade thresholds: A≥95, B≥85, C≥70, D≥50, F<50.
        - Perfect score (100) requires: zero findings + complete stack + quality SBOM.

    `scanner_coverage` reports whether each scanner actually executed (its
    tool was installed and enabled). Categories that did not run contribute
    0 — an empty result is not treated as 'clean' — and the perfect-score
    gate requires every scanner to have executed.
    """
    coverage = scanner_coverage or {}
    sast_executed = coverage.get("sast", True)
    secrets_executed = coverage.get("secrets", True)
    deps_executed = coverage.get("dependencies", True)
    dockerfile_executed = coverage.get("dockerfile", True)
    sbom_executed = coverage.get("sbom", True)

    # Per-category scores (0-100 each)
    sast_score = _calculate_category_score(sast_findings, max_score=100, executed=sast_executed)
    secrets_score = _calculate_category_score(secrets, max_score=100, executed=secrets_executed)
    deps_score = _calculate_category_score(vulnerable_packages, max_score=100, executed=deps_executed)
    dockerfile_score = _calculate_category_score(dockerfile_findings, max_score=100, executed=dockerfile_executed)
    sbom_score = _calculate_sbom_score(sbom) if sbom_executed else 0
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

    # Perfect score gate: must truly be clean AND well-detected
    has_no_findings = not (sast_findings or secrets or vulnerable_packages or dockerfile_findings)
    has_no_scan_unexecuted = (
        sast_executed and secrets_executed and deps_executed and dockerfile_executed
    )
    has_complete_stack = (
        bool(stack_detection.primary_language and stack_detection.primary_language != "unknown")
        and stack_detection.confidence >= 0.95
        and bool(stack_detection.frameworks and stack_detection.frameworks != "unknown")
    )
    has_quality_sbom = sbom_score >= 90 and sbom_executed

    if has_no_findings and has_no_scan_unexecuted and has_complete_stack and has_quality_sbom:
        total_score = 100
        breakdown = ScoreBreakdown(
            sast=SCORING_WEIGHTS["sast"],
            secrets=SCORING_WEIGHTS["secrets"],
            dependencies=SCORING_WEIGHTS["dependencies"],
            dockerfile=SCORING_WEIGHTS["dockerfile"],
            sbom=SCORING_WEIGHTS["sbom"],
            stack_detection=SCORING_WEIGHTS["stack_detection"],
        )

    # Determine grade (industry standard: A, B, C, D, F)
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
        "Security score calculated: %d/100 (Grade %s), Critical=%d, High=%d, Medium=%d, Low=%d, Info=%d",
        score.score,
        score.grade.value,
        score.severity_counts.critical,
        score.severity_counts.high,
        score.severity_counts.medium,
        score.severity_counts.low,
        score.severity_counts.info,
    )
    return score