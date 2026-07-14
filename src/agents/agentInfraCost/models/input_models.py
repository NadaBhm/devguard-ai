"""Pydantic models for the payload received from the repo-analysis agent (Agent 1).

Only the fields this agent actually consumes are modeled strictly. Every other
top-level field the upstream agent may send (phases, summary, sast_findings,
secrets, dependencies, dockerfile_findings, sbom, ...) is accepted and ignored
via ``extra="ignore"``, so this agent stays decoupled from Agent 1's internal
report format and never breaks when that format grows new fields.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ContainerInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    detected: bool
    base_image: Optional[str] = None
    dockerfile_path: Optional[str] = None
    compose_detected: Optional[bool] = None


class StackDetection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    primary_language: str
    frameworks: list[str] = Field(default_factory=list)
    database: Optional[str] = None
    build_tool: Optional[str] = None
    container: ContainerInfo
    confidence: float = Field(ge=0.0, le=1.0)
    detected_files: list[str] = Field(default_factory=list)


class RepoMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    branch: str
    commit_sha: Optional[str] = None
    total_files: int = Field(ge=0)
    loc: int = Field(ge=0)
    language_breakdown: dict[str, int] = Field(default_factory=dict)


class SecurityScore(BaseModel):
    """Informational only. Never read by decision_engine or cost_estimator."""

    model_config = ConfigDict(extra="ignore")

    score: Optional[int] = None
    grade: Optional[str] = None
    max_score: Optional[int] = None
    breakdown: Optional[dict[str, int]] = None
    severity_counts: Optional[dict[str, int]] = None
    recommendations: list[str] = Field(default_factory=list)


class InfraCostAgentInput(BaseModel):
    """Validated payload received from the repo-analysis agent (Agent 1)."""

    model_config = ConfigDict(extra="ignore")

    job_id: str
    status: str
    error: Optional[str] = None
    repo_url: str
    repo_metadata: RepoMetadata
    stack_detection: StackDetection
    security_score: Optional[SecurityScore] = None
