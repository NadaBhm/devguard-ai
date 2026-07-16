"""Pydantic models for the InfraCost Agent's input contract.

These models describe the *structural* shape of the payload produced by the
repo-analysis agent (Agent 1). They intentionally do not encode business
rules (e.g. "status must equal 'completed'", "confidence must be >= 0.5") —
those are fail-fast checks applied in ``core.input_validator`` so that the
reason for rejection is explicit and typed rather than a generic Pydantic
``ValidationError``.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ContainerInfo(BaseModel):
    """Container detection details for the analyzed repository."""

    model_config = ConfigDict(frozen=True)

    detected: bool
    base_image: Optional[str] = None
    dockerfile_path: Optional[str] = None
    compose_detected: bool = False


class StackDetection(BaseModel):
    """Detected technology stack and the confidence associated with it."""

    model_config = ConfigDict(frozen=True)

    primary_language: str
    frameworks: list[str] = Field(default_factory=list)
    database: Optional[str] = None
    build_tool: Optional[str] = None
    container: ContainerInfo
    confidence: float = Field(ge=0.0, le=1.0)
    detected_files: list[str] = Field(default_factory=list)


class RepoMetadata(BaseModel):
    """Metadata about the repository that was analyzed."""

    model_config = ConfigDict(frozen=True)

    name: str
    branch: str
    commit_sha: str
    total_files: int = Field(ge=0)
    loc: int = Field(ge=0)
    language_breakdown: dict[str, int] = Field(default_factory=dict)


class SecurityScore(BaseModel):
    """Security score from Agent 1. Informational only for this agent."""

    model_config = ConfigDict(frozen=True)

    score: int = Field(ge=0, le=100)
    grade: str
    recommendations: list[str] = Field(default_factory=list)


class RepoAnalysisInput(BaseModel):
    """Top-level input contract received from the repo-analysis agent."""

    model_config = ConfigDict(frozen=True)

    job_id: str
    status: str
    error: Optional[str] = None
    repo_url: str
    repo_metadata: RepoMetadata
    stack_detection: StackDetection
    security_score: Optional[SecurityScore] = None
