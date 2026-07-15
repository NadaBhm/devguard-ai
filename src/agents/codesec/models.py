"""
CodeSec Agent - Pydantic Models
================================
Defines all data models for the CodeSec security analysis pipeline.
Maps to the JSON schema mockup (job_id 550e...) and spec book US-1.1.1 through US-1.1.6.

Author: Nada 
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    """Severity levels aligned with CVSS and OWASP risk rating."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Grade(str, Enum):
    """Letter grade for security posture (A=excellent, F=critical)."""

    A = "A"
    B = "B"
    C = "C"
    D = "D"
    E = "E"
    F = "F"


class PhaseStatus(str, Enum):
    """Status of an analysis phase."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class SbomFormat(str, Enum):
    """Supported SBOM output formats."""

    CYCLONE_DX = "CycloneDX"
    SPDX = "SPDX"


# ---------------------------------------------------------------------------
# Phase Tracking
# ---------------------------------------------------------------------------

class PhaseInfo(BaseModel):
    """Tracks the execution status of a single analysis phase."""

    name: str = Field(..., description="Phase identifier (e.g., 'sast', 'secrets')")
    status: PhaseStatus = Field(default=PhaseStatus.PENDING)
    started_at: datetime | None = Field(default=None)
    completed_at: datetime | None = Field(default=None)
    error_message: str | None = Field(default=None)


# ---------------------------------------------------------------------------
# Stack Detection Models
# ---------------------------------------------------------------------------

class ContainerInfo(BaseModel):
    """Container/Docker metadata detected from repository."""

    detected: bool = Field(default=False)
    base_image: str | None = Field(default=None)
    dockerfile_path: str | None = Field(default=None)
    compose_detected: bool = Field(default=False)


class StackDetection(BaseModel):
    """
    Technology stack identification result.
    US-1.1.2: Detect language, framework, database with >=80% accuracy.
    """

    primary_language: str = Field(..., description="Dominant programming language")
    languages: list[str] = Field(default_factory=list, description="All detected languages")
    frameworks: list[str] = Field(default_factory=list)
    database: str | None = Field(default=None)
    build_tool: str | None = Field(default=None)
    container: ContainerInfo = Field(default_factory=ContainerInfo)
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Detection confidence score"
    )
    detected_files: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# SAST Models
# ---------------------------------------------------------------------------

class SASTFinding(BaseModel):
    """
    Single static analysis security finding.
    US-1.1.3: Identify SQL injection, XSS, and other OWASP Top 10 risks.
    """

    model_config = {"extra": "allow"}

    rule_id: str | None = Field(default=None, description="Scanner rule identifier")
    tool: str = Field(default="unknown", description="Tool that produced the finding (e.g., semgrep)")
    severity: Severity | str = Field(default="low")
    category: str = Field(default="security", description="High-level category (e.g., 'owasp-top10')")
    owasp_category: str | None = Field(default=None)
    cwe_id: str | None = Field(default=None, description="CWE identifier, e.g., CWE-89")
    check_id: str | None = Field(default=None)
    cwe: str | None = Field(default=None)
    file: str = Field(default="")
    line: int = Field(default=1, ge=1)
    column: int = Field(default=1, ge=1)
    message: str = Field(default="")
    snippet: str | None = Field(default=None)
    remediation: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def normalize_inputs(cls, values: Any) -> Any:
        if isinstance(values, dict):
            if "rule_id" not in values and "check_id" in values:
                values["rule_id"] = values["check_id"]
            if "cwe_id" not in values and "cwe" in values:
                values["cwe_id"] = values["cwe"]
            if "severity" in values and isinstance(values["severity"], str):
                mapping = {
                    "critical": Severity.CRITICAL,
                    "error": Severity.HIGH,
                    "high": Severity.HIGH,
                    "warning": Severity.MEDIUM,
                    "medium": Severity.MEDIUM,
                    "low": Severity.LOW,
                    "info": Severity.LOW,
                    "informational": Severity.LOW,
                    "unknown": Severity.LOW,
                }
                values["severity"] = mapping.get(values["severity"].strip().lower(), Severity.LOW)
        return values


class SASTResult(BaseModel):
    """Compatibility result wrapper for SAST scanning."""

    findings: list[SASTFinding] = Field(default_factory=list)
    total_findings: int = Field(default=0)
    scan_error: str | None = Field(default=None)
    medium_count: int = Field(default=0)
    high_count: int = Field(default=0)
    low_count: int = Field(default=0)
    info_count: int = Field(default=0)
    critical_count: int = Field(default=0)

    # NOTE: Do NOT override __iter__ on a Pydantic BaseModel.
    # BaseModel.__iter__ yields (field_name, value) tuples for serialization.
    # Iterate over .findings directly instead.

    def __len__(self) -> int:
        return len(self.findings)

    def __getitem__(self, index: int) -> SASTFinding | list[SASTFinding]:
        return self.findings[index]

    def __bool__(self) -> bool:
        return bool(self.findings)


# ---------------------------------------------------------------------------
# Secrets Detection Models
# ---------------------------------------------------------------------------

class Secret(BaseModel):
    """
    Hardcoded credential or sensitive token found in source.
    US-1.1.4: Detect API keys, tokens, passwords with >80% recall.
    """

    type: str = Field(..., description="Secret type (e.g., 'aws_access_key_id')")
    tool: str = Field(..., description="Detection tool (e.g., 'gitleaks')")
    file: str = Field(...)
    line: int = Field(..., ge=1)
    column: int = Field(default=1, ge=1)
    value_preview: str | None = Field(
        default=None, description="Masked preview of the secret value"
    )
    severity: Severity = Field(default=Severity.HIGH)
    commit_sha: str | None = Field(default=None)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    remediation: str | None = Field(default=None)


# ---------------------------------------------------------------------------
# Dependency Vulnerability Models
# ---------------------------------------------------------------------------

class VulnerablePackage(BaseModel):
    """A dependency with known CVEs."""

    package: str = Field(...)
    installed_version: str = Field(...)
    fixed_version: str | None = Field(default=None)
    cve_id: str | None = Field(default=None)
    severity: Severity = Field(...)
    cvss_score: float | None = Field(default=None, ge=0.0, le=10.0)
    description: str | None = Field(default=None)


class DependenciesResult(BaseModel):
    """Aggregated dependency vulnerability scan result."""

    total_packages: int = Field(default=0)
    direct: int = Field(default=0)
    transitive: int = Field(default=0)
    vulnerable_packages: list[VulnerablePackage] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Dockerfile Scan Models
# ---------------------------------------------------------------------------

class DockerfileFinding(BaseModel):
    """Security issue found in a Dockerfile."""

    rule_id: str = Field(...)
    tool: str = Field(...)
    severity: Severity = Field(...)
    category: str = Field(default="dockerfile")
    file: str = Field(...)
    line: int = Field(..., ge=1)
    message: str = Field(...)
    snippet: str | None = Field(default=None)
    remediation: str | None = Field(default=None)


# ---------------------------------------------------------------------------
# SBOM Models
# ---------------------------------------------------------------------------

class LicenseInfo(BaseModel):
    """Software license declaration."""

    id: str | None = Field(default=None)
    name: str | None = Field(default=None)


class SbomComponent(BaseModel):
    """Individual component in an SBOM."""

    type: str = Field(default="library")
    name: str = Field(...)
    version: str = Field(...)
    purl: str | None = Field(default=None, description="Package URL")
    licenses: list[LicenseInfo] = Field(default_factory=list)
    source_file: str | None = Field(default=None)


class SBOM(BaseModel):
    """
    Software Bill of Materials output.
    US-1.1.6: Produce valid CycloneDX/SPDX format SBOM.
    """

    format: SbomFormat = Field(default=SbomFormat.CYCLONE_DX)
    spec_version: str = Field(default="1.5")
    serial_number: str = Field(...)
    version: int = Field(default=1)
    components_count: int = Field(default=0)
    components: list[SbomComponent] = Field(default_factory=list)
    download_url: str | None = Field(default=None)


# ---------------------------------------------------------------------------
# Security Score Models
# ---------------------------------------------------------------------------

class ScoreBreakdown(BaseModel):
    """Per-category score contribution (0-100 scale per category)."""

    sast: int = Field(default=0, ge=0, le=100)
    secrets: int = Field(default=0, ge=0, le=100)
    dependencies: int = Field(default=0, ge=0, le=100)
    dockerfile: int = Field(default=0, ge=0, le=100)
    sbom: int = Field(default=0, ge=0, le=100)
    stack_detection: int = Field(default=0, ge=0, le=100)


class SeverityCounts(BaseModel):
    """Aggregate severity tally across all findings."""

    critical: int = Field(default=0, ge=0)
    high: int = Field(default=0, ge=0)
    medium: int = Field(default=0, ge=0)
    low: int = Field(default=0, ge=0)
    info: int = Field(default=0, ge=0)


class SecurityScore(BaseModel):
    """
    Overall security posture score and grade.
    US-1.1.5: 0-100 score with Critical/High/Medium/Low prioritization.
    """

    score: int = Field(..., ge=0, le=100)
    grade: Grade = Field(...)
    max_score: int = Field(default=100)
    breakdown: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    severity_counts: SeverityCounts = Field(default_factory=SeverityCounts)
    recommendations: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Repository Metadata
# ---------------------------------------------------------------------------

class LanguageBreakdown(BaseModel):
    """Lines of code per language."""

    model_config = {"extra": "allow"}  # Allow dynamic language keys


class RepoMetadata(BaseModel):
    """High-level repository statistics."""

    name: str = Field(...)
    branch: str = Field(default="main")
    commit_sha: str | None = Field(default=None)
    total_files: int = Field(default=0, ge=0)
    loc: int = Field(default=0, ge=0)
    language_breakdown: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

class Summary(BaseModel):
    """Executive summary of the security scan."""

    files_scanned: int = Field(default=0, ge=0)
    sast_findings_count: int = Field(default=0, ge=0)
    secrets_found_count: int = Field(default=0, ge=0)
    vulnerable_dependencies_count: int = Field(default=0, ge=0)
    dockerfile_issues_count: int = Field(default=0, ge=0)
    total_critical: int = Field(default=0, ge=0)
    total_high: int = Field(default=0, ge=0)
    total_medium: int = Field(default=0, ge=0)
    total_low: int = Field(default=0, ge=0)
    total_info: int = Field(default=0, ge=0)


# ---------------------------------------------------------------------------
# Top-Level Result
# ---------------------------------------------------------------------------

class CodeSecResult(BaseModel):
    """
    Complete CodeSec analysis result — the canonical output schema.
    Matches the mock JSON schema exactly for downstream consumers
    (InfraCost, Orchestrator, Dashboard, Report Generator).
    """

    job_id: str = Field(...)
    status: str = Field(default="completed")
    error: str | None = Field(default=None)
    repo_url: str = Field(...)
    repo_metadata: RepoMetadata = Field(...)
    phases: list[PhaseInfo] = Field(default_factory=list)
    summary: Summary = Field(default_factory=Summary)
    stack_detection: StackDetection = Field(...)
    sast_findings: list[SASTFinding] = Field(default_factory=list)
    secrets: list[Secret] = Field(default_factory=list)
    dependencies: DependenciesResult = Field(default_factory=DependenciesResult)
    dockerfile_findings: list[DockerfileFinding] = Field(default_factory=list)
    sbom: SBOM = Field(default_factory=lambda: SBOM(serial_number=""))
    security_score: SecurityScore = Field(...)