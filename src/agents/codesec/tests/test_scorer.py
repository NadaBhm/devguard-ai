"""Tests for Security Score Calculator."""
import pytest

from codesec.scanners.scorer import (
    calculate_score,
    _calculate_category_score,
    _calculate_stack_detection_score,
    _calculate_sbom_score,
    _generate_recommendations,
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


class TestCalculateCategoryScore:
    """Test per-category score calculation."""

    def test_no_findings(self):
        assert _calculate_category_score([]) == 100

    def test_single_low_finding(self):
        findings = [
            SASTFinding(rule_id="R1", tool="t", severity=Severity.LOW, category="c", file="f", line=1, message="m"),
        ]
        score = _calculate_category_score(findings)
        assert score < 100
        assert score > 0

    def test_single_critical_finding(self):
        findings = [
            SASTFinding(rule_id="R1", tool="t", severity=Severity.CRITICAL, category="c", file="f", line=1, message="m"),
        ]
        score = _calculate_category_score(findings)
        assert score < 50  # Critical should heavily penalize

    def test_multiple_findings_decay(self):
        """Test that additional findings of same severity decay."""
        findings = [
            SASTFinding(rule_id="R1", tool="t", severity=Severity.HIGH, category="c", file="f", line=1, message="m"),
            SASTFinding(rule_id="R1", tool="t", severity=Severity.HIGH, category="c", file="f", line=2, message="m"),
            SASTFinding(rule_id="R1", tool="t", severity=Severity.HIGH, category="c", file="f", line=3, message="m"),
        ]
        score = _calculate_category_score(findings)
        assert score < 100
        assert score >= 0

    def test_score_never_negative(self):
        findings = [
            SASTFinding(rule_id="R1", tool="t", severity=Severity.CRITICAL, category="c", file="f", line=i, message="m")
            for i in range(1, 101) 
        ]
        score = _calculate_category_score(findings)
        assert score == 0


class TestCalculateStackDetectionScore:
    """Test stack detection scoring."""

    def test_complete_stack(self):
        stack = StackDetection(
            primary_language="python",
            frameworks=["fastapi"],
            database="postgresql",
            build_tool="pip",
            container=ContainerInfo(detected=True, base_image="python:3.12-slim"),
            confidence=0.92,
        )
        score = _calculate_stack_detection_score(stack)
        assert score > 80

    def test_incomplete_stack(self):
        stack = StackDetection(
            primary_language="unknown",
            confidence=0.1,
        )
        score = _calculate_stack_detection_score(stack)
        assert score < 50

    def test_zero_confidence(self):
        stack = StackDetection(primary_language="python", confidence=0.0)
        score = _calculate_stack_detection_score(stack)
        assert score == 0


class TestCalculateSBOMScore:
    """Test SBOM quality scoring."""

    def test_complete_sbom(self):
        sbom = SBOM(
            serial_number="urn:uuid:test",
            components_count=3,
            components=[
                SbomComponent(name="lib1", version="1.0", licenses=[LicenseInfo(id="MIT")]),
                SbomComponent(name="lib2", version="2.0", licenses=[LicenseInfo(id="Apache-2.0")]),
                SbomComponent(name="lib3", version="3.0", licenses=[LicenseInfo(id="BSD")]),
            ],
        )
        score = _calculate_sbom_score(sbom)
        assert score == 100

    def test_sbom_missing_licenses(self):
        sbom = SBOM(
            serial_number="urn:uuid:test",
            components_count=2,
            components=[
                SbomComponent(name="lib1", version="1.0", licenses=[]),
                SbomComponent(name="lib2", version="2.0", licenses=[LicenseInfo(id="MIT")]),
            ],
        )
        score = _calculate_sbom_score(sbom)
        assert score < 100
        assert score > 0

    def test_empty_sbom(self):
        sbom = SBOM(serial_number="urn:uuid:test", components_count=0)
        score = _calculate_sbom_score(sbom)
        assert score == 0

    def test_none_sbom(self):
        score = _calculate_sbom_score(None)  # type: ignore[arg-type]
        assert score == 0


class TestGenerateRecommendations:
    """Test recommendation generation."""

    def test_critical_sast_recommendation(self):
        sast = [
            SASTFinding(rule_id="sql-injection", tool="t", severity=Severity.CRITICAL, category="c", file="db.py", line=10, message="SQLi"),
        ]
        recs = _generate_recommendations(sast, [], [], [])
        assert len(recs) > 0
        assert "critical" in recs[0].lower()

    def test_secrets_recommendation(self):
        secrets = [
            Secret(type="aws_access_key_id", tool="t", file=".env", line=1, confidence=0.9),
        ]
        recs = _generate_recommendations([], secrets, [], [])
        assert len(recs) > 0

    def test_dependency_recommendation(self):
        deps = [
            VulnerablePackage(package="requests", installed_version="2.25.0", cve_id="CVE-2023-1", severity=Severity.HIGH),
        ]
        recs = _generate_recommendations([], [], deps, [])
        assert len(recs) > 0
        assert "requests" in recs[0]

    def test_dockerfile_recommendation(self):
        docker = [
            DockerfileFinding(rule_id="DS001", tool="t", severity=Severity.HIGH, file="Dockerfile", line=5, message="root user"),
        ]
        recs = _generate_recommendations([], [], [], docker)
        assert len(recs) > 0

    def test_no_findings_no_recommendations(self):
        recs = _generate_recommendations([], [], [], [])
        assert recs == []

    def test_cap_at_10(self):
        sast = [
            SASTFinding(rule_id=f"R{i}", tool="t", severity=Severity.CRITICAL, category="c", file="f", line=i, message="m")
            for i in range(1, 21)    
        ]
        recs = _generate_recommendations(sast, [], [], [])
        assert len(recs) <= 10


class TestCalculateScore:
    """Test overall score calculation."""

    def test_perfect_score(self):
        score = calculate_score(
            sast_findings=[],
            secrets=[],
            vulnerable_packages=[],
            dockerfile_findings=[],
            sbom=SBOM(serial_number="urn:uuid:test", components_count=5, components=[SbomComponent(name="lib", version="1.0")]),
            stack_detection=StackDetection(primary_language="python", frameworks=["fastapi"], database="pg", build_tool="pip", confidence=0.95),
        )
        assert isinstance(score, SecurityScore)
        assert score.score == 100
        assert score.grade == Grade.A

    def test_zero_score(self):
        score = calculate_score(
            sast_findings=[
                SASTFinding(rule_id="R1", tool="t", severity=Severity.CRITICAL, category="c", file="a.py", line=1, message="m")
                for _ in range(50)
            ],
            secrets=[
                Secret(type="api_key", tool="t", file=".env", line=1, confidence=0.9)
                for _ in range(20)
            ],
            vulnerable_packages=[
                VulnerablePackage(package="x", installed_version="1.0", cve_id="CVE-1", severity=Severity.CRITICAL)
                for _ in range(30)
            ],
            dockerfile_findings=[
                DockerfileFinding(rule_id="DS1", tool="t", severity=Severity.CRITICAL, file="Dockerfile", line=1, message="m")
                for _ in range(20)
            ],
            sbom=SBOM(serial_number="urn:uuid:test", components_count=0),
            stack_detection=StackDetection(primary_language="unknown", confidence=0.1),
        )
        assert isinstance(score, SecurityScore)
        assert score.score == 0

    def test_score_with_issues(self):
        score = calculate_score(
            sast_findings=[
                SASTFinding(rule_id="R1", tool="t", severity=Severity.HIGH, category="c", file="a.py", line=1, message="m"),
            ],
            secrets=[
                Secret(type="api_key", tool="t", file=".env", line=1, confidence=0.9),
            ],
            vulnerable_packages=[
                VulnerablePackage(package="x", installed_version="1.0", cve_id="CVE-1", severity=Severity.HIGH),
            ],
            dockerfile_findings=[
                DockerfileFinding(rule_id="DS1", tool="t", severity=Severity.MEDIUM, file="Dockerfile", line=1, message="m"),
            ],
            sbom=SBOM(serial_number="urn:uuid:test", components_count=3),
            stack_detection=StackDetection(primary_language="python", confidence=0.9),
        )
        assert isinstance(score, SecurityScore)
        assert 0 < score.score < 100
        assert score.severity_counts.high >= 2
        assert len(score.recommendations) > 0

    def test_grade_boundaries(self):
        # Test that grades map correctly
        assert Grade.A == Grade("A")
        assert Grade.F == Grade("F")

    def test_breakdown_sums_to_total(self):
        score = calculate_score(
            sast_findings=[],
            secrets=[],
            vulnerable_packages=[],
            dockerfile_findings=[],
            sbom=SBOM(serial_number="urn:uuid:test", components_count=3),
            stack_detection=StackDetection(primary_language="python", confidence=0.9),
        )
        breakdown_total = (
            score.breakdown.sast
            + score.breakdown.secrets
            + score.breakdown.dependencies
            + score.breakdown.dockerfile
            + score.breakdown.sbom
            + score.breakdown.stack_detection
        )
        assert breakdown_total == score.score

    def test_severity_counts(self):
        score = calculate_score(
            sast_findings=[
                SASTFinding(rule_id="R1", tool="t", severity=Severity.CRITICAL, category="c", file="a.py", line=1, message="m"),
                SASTFinding(rule_id="R2", tool="t", severity=Severity.HIGH, category="c", file="a.py", line=2, message="m"),
            ],
            secrets=[
                Secret(type="key", tool="t", file=".env", line=1, severity=Severity.HIGH, confidence=0.9),
            ],
            vulnerable_packages=[],
            dockerfile_findings=[],
            sbom=SBOM(serial_number="urn:uuid:test"),
            stack_detection=StackDetection(primary_language="python", confidence=0.9),
        )
        assert score.severity_counts.critical == 1
        assert score.severity_counts.high == 2


class TestSecurityScoreModel:
    """Test SecurityScore model."""

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