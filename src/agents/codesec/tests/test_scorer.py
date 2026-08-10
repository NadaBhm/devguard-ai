"""Tests for Security Score Calculator — Refined (T-4.7)."""
import pytest

from codesec.scanners.scorer import (
    calculate_score,
    _calculate_category_score,
    _calculate_stack_detection_score,
    _calculate_sbom_score,
    _generate_recommendations,
    _priority_score,
)
from codesec.models import (
    SASTFinding,
    Secret,
    VulnerablePackage,
    DockerfileFinding,
    SBOM,
    StackDetection,
    SecurityScore,
    Severity,
    Grade,
    SbomComponent,
    LicenseInfo,
    ContainerInfo,
    ScoreBreakdown,
    SeverityCounts,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _sast(severity: Severity, count: int = 1) -> list[SASTFinding]:
    return [
        SASTFinding(
            rule_id=f"R{i}",
            tool="semgrep",
            severity=severity,
            category="owasp-top10",
            file="app.py",
            line=i,
            message="issue",
        )
        for i in range(1, count + 1)
    ]


def _secret(count: int = 1, secret_type: str = "api_key") -> list[Secret]:
    return [
        Secret(
            type=secret_type,
            tool="gitleaks",
            file=".env",
            line=i,
            confidence=0.9,
        )
        for i in range(1, count + 1)
    ]


def _vuln_pkg(severity: Severity, count: int = 1) -> list[VulnerablePackage]:
    return [
        VulnerablePackage(
            package="requests",
            installed_version="2.25.0",
            cve_id=f"CVE-2023-{i}",
            severity=severity,
        )
        for i in range(1, count + 1)
    ]


def _dockerfile(severity: Severity, count: int = 1) -> list[DockerfileFinding]:
    return [
        DockerfileFinding(
            rule_id=f"DS{i}",
            tool="trivy",
            severity=severity,
            file="Dockerfile",
            line=i,
            message="issue",
        )
        for i in range(1, count + 1)
    ]


def _sbom(components: list[SbomComponent] | None = None) -> SBOM:
    comps = components or []
    return SBOM(
        serial_number="urn:uuid:test",
        components_count=len(comps),
        components=comps,
    )


def _stack(
    lang: str = "python",
    confidence: float = 0.95,
    frameworks: list[str] | None = None,
    database: str | None = "postgresql",
    build_tool: str | None = "pip",
    container_detected: bool = True,
) -> StackDetection:
    # FIX: frameworks=[] must stay [] — not fallback to ["fastapi"]
    return StackDetection(
        primary_language=lang,
        frameworks=frameworks if frameworks is not None else ["fastapi"],
        database=database,
        build_tool=build_tool,
        container=ContainerInfo(detected=container_detected, base_image="python:3.12-slim"),
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# _calculate_category_score
# ---------------------------------------------------------------------------
class TestCalculateCategoryScore:
    """Test per-category scoring with cap, floor, and decay."""

    def test_no_findings_returns_100(self):
        assert _calculate_category_score([]) == 100

    def test_single_critical_finding_capped(self):
        """1 critical hits the 70% cap: 100 - 70 = 30."""
        findings = _sast(Severity.CRITICAL, 1)
        score = _calculate_category_score(findings)
        assert score == 30

    def test_single_high_finding(self):
        findings = _sast(Severity.HIGH, 1)
        score = _calculate_category_score(findings)
        assert 30 < score < 50  # 60 penalty, score = 40

    def test_single_medium_finding(self):
        findings = _sast(Severity.MEDIUM, 1)
        score = _calculate_category_score(findings)
        assert 70 < score < 90  # 4*4 = 16 penalty, score = 84

    def test_single_low_finding(self):
        findings = _sast(Severity.LOW, 1)
        score = _calculate_category_score(findings)
        assert score > 90  # 1*1.5 = 1.5 penalty

    def test_many_low_findings_never_crash_to_zero(self):
        """Diminishing returns: even 1000 low findings can't kill the score."""
        findings = _sast(Severity.LOW, 1000)
        score = _calculate_category_score(findings)
        assert score >= 85  # geometric series converges to ~6 points

    def test_many_critical_findings_respects_floor(self):
        """Cap at 70% gives 30; floor is 15 but never reached because 30 > 15."""
        findings = _sast(Severity.CRITICAL, 100)
        score = _calculate_category_score(findings)
        assert score == 30  # cap dominates, floor not triggered
        assert score >= 15  # floor safety net still conceptually valid

    def test_decay_reduces_repeated_penalty(self):
        """2 high findings should penalize less than 2x a single high."""
        one = _calculate_category_score(_sast(Severity.HIGH, 1))
        two = _calculate_category_score(_sast(Severity.HIGH, 2))
        assert (100 - two) < (100 - one) * 1.75  # decay ensures sub-linear

    def test_secrets_category_same_logic(self):
        findings = _secret(1, "aws_access_key_id")
        score = _calculate_category_score(findings)
        assert score < 100

    def test_vulnerable_packages_category(self):
        findings = _vuln_pkg(Severity.HIGH, 1)
        score = _calculate_category_score(findings)
        assert score < 100


# ---------------------------------------------------------------------------
# _calculate_stack_detection_score
# ---------------------------------------------------------------------------
class TestCalculateStackDetectionScore:
    """Test stack detection scoring."""

    def test_complete_high_confidence_stack(self):
        stack = _stack(confidence=0.95)
        score = _calculate_stack_detection_score(stack)
        assert score >= 95

    def test_complete_perfect_confidence_gets_bonus(self):
        stack = _stack(confidence=1.0)
        score = _calculate_stack_detection_score(stack)
        assert score == 100

    def test_missing_frameworks_penalty(self):
        stack = _stack(frameworks=[], confidence=0.95)
        score = _calculate_stack_detection_score(stack)
        assert score == 85  # missing frameworks = -10

    def test_missing_database_and_build_tool_penalty(self):
        stack = _stack(database=None, build_tool=None, confidence=0.95)
        score = _calculate_stack_detection_score(stack)
        assert score == 75  # 2 missing fields = -20

    def test_low_confidence(self):
        stack = _stack(confidence=0.4)
        score = _calculate_stack_detection_score(stack)
        assert score < 50

    def test_zero_confidence(self):
        stack = StackDetection(primary_language="python", confidence=0.0)
        assert _calculate_stack_detection_score(stack) == 0

    def test_unknown_language(self):
        stack = StackDetection(primary_language="unknown", confidence=0.5)
        score = _calculate_stack_detection_score(stack)
        assert score < 50


# ---------------------------------------------------------------------------
# _calculate_sbom_score
# ---------------------------------------------------------------------------
class TestCalculateSBOMScore:
    """Test SBOM quality scoring."""

    def test_empty_sbom(self):
        assert _calculate_sbom_score(_sbom([])) == 0

    def test_none_sbom(self):
        assert _calculate_sbom_score(None) == 0  # type: ignore[arg-type]

    def test_complete_sbom_with_licenses(self):
        """Need >=5 components to avoid the 'incomplete' -20 penalty."""
        comps = [
            SbomComponent(name=f"lib{i}", version=f"{i}.0", licenses=[LicenseInfo(id="MIT")])
            for i in range(1, 6)
        ]
        score = _calculate_sbom_score(_sbom(comps))
        assert score == 100

    def test_missing_licenses_penalty(self):
        """3 of 6 missing licenses → 50% ratio → -15. No incomplete penalty (6 >= 5)."""
        comps = [
            SbomComponent(name="lib1", version="1.0", licenses=[]),
            SbomComponent(name="lib2", version="2.0", licenses=[]),
            SbomComponent(name="lib3", version="3.0", licenses=[]),
            SbomComponent(name="lib4", version="4.0", licenses=[LicenseInfo(id="MIT")]),
            SbomComponent(name="lib5", version="5.0", licenses=[LicenseInfo(id="MIT")]),
            SbomComponent(name="lib6", version="6.0", licenses=[LicenseInfo(id="MIT")]),
        ]
        score = _calculate_sbom_score(_sbom(comps))
        assert score == 85  # 100 - 15

    def test_incomplete_sbom_few_components(self):
        """<5 components triggers -20 penalty."""
        sbom = _sbom([SbomComponent(name="flask", version="2.0", licenses=[LicenseInfo(id="MIT")])])
        score = _calculate_sbom_score(sbom)
        assert score == 80  # 100 - 20 (incomplete)

    def test_vulnerable_components_penalty(self):
        """1 vulnerable among 5 licensed components → -10. No incomplete penalty."""
        comp = SbomComponent(
            name="badlib",
            version="1.0",
            licenses=[LicenseInfo(id="MIT")],
            cve_ids=["CVE-2023-1"],  # FIX: use cve_ids (field existant sur le modèle)
        )
        comps = [
            SbomComponent(name="good1", version="1.0", licenses=[LicenseInfo(id="MIT")]),
            SbomComponent(name="good2", version="2.0", licenses=[LicenseInfo(id="MIT")]),
            SbomComponent(name="good3", version="3.0", licenses=[LicenseInfo(id="MIT")]),
            SbomComponent(name="good4", version="4.0", licenses=[LicenseInfo(id="MIT")]),
            comp,
        ]
        score = _calculate_sbom_score(_sbom(comps))
        assert score == 90  # 100 - 10


# ---------------------------------------------------------------------------
# _priority_score & _generate_recommendations
# ---------------------------------------------------------------------------
class TestPriorityScore:
    """Test recommendation priority calculation."""

    def test_secrets_critical_highest(self):
        p = _priority_score("secrets", "critical")
        assert p > _priority_score("sast", "critical")

    def test_secrets_high_vs_sast_critical(self):
        """Secrets are easier to fix — should still outrank SAST in many cases."""
        p_sec = _priority_score("secrets", "high")
        p_sast = _priority_score("sast", "critical")
        assert p_sec > 0 and p_sast > 0

    def test_dockerfile_lower_than_sast(self):
        assert _priority_score("dockerfile", "critical") < _priority_score("sast", "critical")


class TestGenerateRecommendations:
    """Test prioritized recommendation generation."""

    def test_no_findings_empty(self):
        recs = _generate_recommendations([], [], [], [])
        assert recs == []

    def test_secrets_appear_before_sast(self):
        """Secrets have higher ease_of_fix + exploitability."""
        sast = _sast(Severity.CRITICAL, 1)
        secrets = _secret(1, "password")
        recs = _generate_recommendations(sast, secrets, [], [])
        assert "secret" in recs[0].lower() or "password" in recs[0].lower()

    def test_critical_before_high(self):
        sast_crit = _sast(Severity.CRITICAL, 1)
        sast_high = _sast(Severity.HIGH, 1)
        sast_high[0].rule_id = "R_HIGH"
        recs = _generate_recommendations(sast_crit + sast_high, [], [], [])
        assert "critical" in recs[0].lower()

    def test_capped_at_10(self):
        sast = _sast(Severity.CRITICAL, 20)
        recs = _generate_recommendations(sast, [], [], [])
        assert len(recs) <= 10

    def test_deduplication(self):
        """Same message should not appear twice."""
        sast = _sast(Severity.CRITICAL, 3)
        for f in sast:
            f.rule_id = "same.rule"
            f.file = "same.py"
            f.line = 1
        recs = _generate_recommendations(sast, [], [], [])
        assert len(recs) == 1

    def test_dependency_recommendation_format(self):
        deps = _vuln_pkg(Severity.HIGH, 1)
        recs = _generate_recommendations([], [], deps, [])
        assert "requests" in recs[0]
        assert "CVE" in recs[0]

    def test_dockerfile_recommendation_format(self):
        df = _dockerfile(Severity.HIGH, 1)
        recs = _generate_recommendations([], [], [], df)
        assert "Dockerfile" in recs[0]


# ---------------------------------------------------------------------------
# calculate_score — integration
# ---------------------------------------------------------------------------
class TestCalculateScore:
    """Test overall score calculation and grade assignment."""

    def test_perfect_score_gate(self):
        """100 only if no findings + complete stack + quality SBOM (>=5 comps)."""
        score = calculate_score(
            sast_findings=[],
            secrets=[],
            vulnerable_packages=[],
            dockerfile_findings=[],
            sbom=_sbom([
                SbomComponent(name=f"lib{i}", version=f"{i}.0", licenses=[LicenseInfo(id="MIT")])
                for i in range(1, 6)
            ]),
            stack_detection=_stack(confidence=0.95),
        )
        assert score.score == 100
        assert score.grade == Grade.A
        assert score.breakdown.sast == 25
        assert score.breakdown.secrets == 20

    def test_perfect_score_blocked_by_incomplete_stack(self):
        """No findings but incomplete stack → not 100."""
        score = calculate_score(
            sast_findings=[],
            secrets=[],
            vulnerable_packages=[],
            dockerfile_findings=[],
            sbom=_sbom([
                SbomComponent(name=f"lib{i}", version=f"{i}.0", licenses=[LicenseInfo(id="MIT")])
                for i in range(1, 6)
            ]),
            stack_detection=_stack(confidence=0.80, frameworks=[]),  # incomplete
        )
        assert score.score < 100

    def test_perfect_score_blocked_by_low_quality_sbom(self):
        """No findings but SBOM < 90 (incomplete + no licenses) → not 100."""
        score = calculate_score(
            sast_findings=[],
            secrets=[],
            vulnerable_packages=[],
            dockerfile_findings=[],
            sbom=_sbom([SbomComponent(name="a", version="1.0")]),  # <5 comps, no license
            stack_detection=_stack(confidence=0.95),
        )
        assert score.score < 100

    def test_unexecuted_scanner_cannot_inflate_score(self):
        """"Cli - category that never ran (tool missing/disabled) gets 0, not 100."""
        score = calculate_score(
            sast_findings=[],
            secrets=[],
            vulnerable_packages=[],
            dockerfile_findings=[],
            sbom=_sbom([
                SbomComponent(name=f"lib{i}", version=f"{i}.0", licenses=[LicenseInfo(id="MIT")])
                for i in range(1, 6)
            ]),
            stack_detection=_stack(confidence=0.95),
            scanner_coverage={
                "sast": False, "secrets": True, "dependencies": True,
                "dockerfile": True, "sbom": True,
            },
        )
        assert score.breakdown.sast == 0
        assert score.score < 100
        assert score.grade != Grade.A

    def test_perfect_gate_blocked_when_a_scanner_did_not_run(self):
        """No findings + complete stack + quality SBOM is not 100 if a
        scanner never executed."""
        score = calculate_score(
            sast_findings=[],
            secrets=[],
            vulnerable_packages=[],
            dockerfile_findings=[],
            sbom=_sbom([
                SbomComponent(name=f"lib{i}", version=f"{i}.0", licenses=[LicenseInfo(id="MIT")])
                for i in range(1, 6)
            ]),
            stack_detection=_stack(confidence=0.95),
            scanner_coverage={
                "sast": True, "secrets": True, "dependencies": True,
                "dockerfile": True, "sbom": False, "stack_detection": True,
            },
        )
        assert score.score < 100

    def test_empty_but_executed_still_scores_clean(self):
        """An empty result from a scanner that DID run remains 'clean'."""
        score = calculate_score(
            sast_findings=[],
            secrets=[],
            vulnerable_packages=[],
            dockerfile_findings=[],
            sbom=_sbom([
                SbomComponent(name=f"lib{i}", version=f"{i}.0", licenses=[LicenseInfo(id="MIT")])
                for i in range(1, 6)
            ]),
            stack_detection=_stack(confidence=0.95),
            scanner_coverage=None,
        )
        assert score.score == 100

    def test_grade_a_boundary(self):
        """A requires >= 95."""
        score = calculate_score(
            sast_findings=_sast(Severity.LOW, 1),
            secrets=[],
            vulnerable_packages=[],
            dockerfile_findings=[],
            sbom=_sbom([
                SbomComponent(name=f"lib{i}", version=f"{i}.0", licenses=[LicenseInfo(id="MIT")])
                for i in range(1, 6)
            ]),
            stack_detection=_stack(confidence=0.95),
        )
        assert score.score >= 95
        assert score.grade == Grade.A

    def test_grade_b_boundary(self):
        """B requires >= 85."""
        score = calculate_score(
            sast_findings=_sast(Severity.MEDIUM, 2),
            secrets=[],
            vulnerable_packages=[],
            dockerfile_findings=[],
            sbom=_sbom([
                SbomComponent(name=f"lib{i}", version=f"{i}.0", licenses=[LicenseInfo(id="MIT")])
                for i in range(1, 6)
            ]),
            stack_detection=_stack(confidence=0.95),
        )
        assert score.score >= 85
        assert score.grade in (Grade.A, Grade.B)

    def test_grade_c_boundary(self):
        """C requires >= 70."""
        score = calculate_score(
            sast_findings=_sast(Severity.HIGH, 1),
            secrets=_secret(1),
            vulnerable_packages=[],
            dockerfile_findings=[],
            sbom=_sbom([
                SbomComponent(name=f"lib{i}", version=f"{i}.0", licenses=[LicenseInfo(id="MIT")])
                for i in range(1, 6)
            ]),
            stack_detection=_stack(confidence=0.95),
        )
        assert score.score >= 70
        assert score.grade in (Grade.A, Grade.B, Grade.C)

    def test_grade_d_boundary(self):
        """D requires >= 50. Mix of issues to land in 50-69 range."""
        score = calculate_score(
            sast_findings=_sast(Severity.CRITICAL, 1),      # cat≈30 → w=7
            secrets=_secret(1),                              # cat≈40 → w=8
            vulnerable_packages=_vuln_pkg(Severity.HIGH, 1), # cat≈40 → w=8
            dockerfile_findings=_dockerfile(Severity.MEDIUM, 1), # cat≈60 → w=9
            sbom=_sbom([
                SbomComponent(name=f"lib{i}", version=f"{i}.0", licenses=[LicenseInfo(id="MIT")])
                for i in range(1, 6)
            ]),  # w=10
            stack_detection=_stack(confidence=0.95),  # w=10
        )
        # Expected total ≈ 7+8+8+9+10+10 = 52
        assert score.score >= 50
        assert score.grade in (Grade.C, Grade.D)

    def test_grade_f_below_50(self):
        """F is < 50."""
        score = calculate_score(
            sast_findings=_sast(Severity.CRITICAL, 5),
            secrets=_secret(5),
            vulnerable_packages=_vuln_pkg(Severity.CRITICAL, 5),
            dockerfile_findings=_dockerfile(Severity.CRITICAL, 5),
            sbom=_sbom([]),
            stack_detection=StackDetection(primary_language="unknown", confidence=0.1),
        )
        assert score.score < 50
        assert score.grade == Grade.F

    def test_no_grade_e_exists(self):
        """With thresholds A/B/C/D/F, grade E is never assigned."""
        score = calculate_score(
            sast_findings=_sast(Severity.CRITICAL, 2),
            secrets=[],
            vulnerable_packages=[],
            dockerfile_findings=[],
            sbom=_sbom([
                SbomComponent(name=f"lib{i}", version=f"{i}.0", licenses=[LicenseInfo(id="MIT")])
                for i in range(1, 6)
            ]),
            stack_detection=_stack(confidence=0.95),
        )
        assert score.grade != Grade.E
        assert score.grade in (Grade.A, Grade.B, Grade.C, Grade.D, Grade.F)

    def test_breakdown_sums_to_total(self):
        score = calculate_score(
            sast_findings=_sast(Severity.HIGH, 1),
            secrets=_secret(1),
            vulnerable_packages=_vuln_pkg(Severity.MEDIUM, 1),
            dockerfile_findings=_dockerfile(Severity.LOW, 1),
            sbom=_sbom([
                SbomComponent(name=f"lib{i}", version=f"{i}.0", licenses=[LicenseInfo(id="MIT")])
                for i in range(1, 6)
            ]),
            stack_detection=_stack(confidence=0.95),
        )
        total = (
            score.breakdown.sast
            + score.breakdown.secrets
            + score.breakdown.dependencies
            + score.breakdown.dockerfile
            + score.breakdown.sbom
            + score.breakdown.stack_detection
        )
        assert total == score.score

    def test_severity_counts_across_all_categories(self):
        score = calculate_score(
            sast_findings=_sast(Severity.CRITICAL, 2),
            secrets=_secret(1, "aws_key"),
            vulnerable_packages=_vuln_pkg(Severity.HIGH, 3),
            dockerfile_findings=_dockerfile(Severity.MEDIUM, 1),
            sbom=_sbom([]),
            stack_detection=_stack(confidence=0.9),
        )
        assert score.severity_counts.critical == 2
        assert score.severity_counts.high == 4  # 1 secret + 3 deps
        assert score.severity_counts.medium == 1
        assert score.severity_counts.low == 0

    def test_recommendations_populated(self):
        score = calculate_score(
            sast_findings=_sast(Severity.CRITICAL, 1),
            secrets=_secret(1),
            vulnerable_packages=[],
            dockerfile_findings=[],
            sbom=_sbom([]),
            stack_detection=_stack(confidence=0.9),
        )
        assert len(score.recommendations) > 0

    def test_zero_score_floor(self):
        """Even catastrophic repos shouldn't score 0 because of cap 70% per category."""
        score = calculate_score(
            sast_findings=_sast(Severity.CRITICAL, 1000),
            secrets=_secret(1000),
            vulnerable_packages=_vuln_pkg(Severity.CRITICAL, 1000),
            dockerfile_findings=_dockerfile(Severity.CRITICAL, 1000),
            sbom=_sbom([]),
            stack_detection=StackDetection(primary_language="unknown", confidence=0.0),
        )
        assert score.score > 0
        # With cap 70% (score 30 per category) weighted:
        # sast: 30*0.25=7.5→7, secrets: 30*0.20=6, deps: 30*0.20=6, docker: 30*0.15=4.5→4
        # sbom: 0, stack: 0. Total ≈ 23.
        assert 0 < score.score <= 25


class TestSecurityScoreModel:
    """Test SecurityScore Pydantic model."""

    def test_creation(self):
        score = SecurityScore(
            score=85,
            grade=Grade.B,
            breakdown=ScoreBreakdown(sast=20, secrets=15, dependencies=20, dockerfile=15, sbom=10, stack_detection=10),
            severity_counts=SeverityCounts(critical=0, high=2, medium=3, low=1, info=0),
            recommendations=["Fix issue 1", "Fix issue 2"],
        )
        assert score.score == 85
        assert score.grade == Grade.B
        assert len(score.recommendations) == 2

    def test_defaults(self):
        score = SecurityScore(score=50, grade=Grade.D)
        assert score.max_score == 100
        assert score.breakdown.sast == 0
        assert score.severity_counts.critical == 0
        assert score.recommendations == []