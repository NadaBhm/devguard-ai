"""Pydantic models passed between pipeline stages (modules 2, 4, 5, 6, 8).

These are NOT part of the wire contract with Agent 3 — see output_models.py
for that. They exist so each pipeline stage has a typed input/output and can
be tested in isolation.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .common import ComputeType

# --- decision_engine (module 2) ---------------------------------------------


class EcsSizing(BaseModel):
    """Full ECS configuration decided by decision_engine: identity, size and
    rollout behaviour. output_builder splits this into aws_config.ecs /
    deployment_config.ecs without adding any logic of its own."""

    model_config = ConfigDict(extra="forbid")

    cluster: str
    service_name: str
    task_cpu: int = Field(gt=0)
    task_memory: int = Field(gt=0)
    health_check_path: str
    health_check_port: int
    timeout_minutes: int
    min_healthy_percent: int
    max_percent: int


class LambdaSizing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    function_name: str
    runtime: str
    memory_mb: int = Field(gt=0)
    timeout_seconds: int = Field(gt=0)
    handler: str
    reserved_concurrency: Optional[int] = None


class Ec2Sizing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_type: str
    ami_id: str
    instance_count: int = Field(gt=0)
    key_pair_name: str
    health_check_path: str
    health_check_port: int
    timeout_minutes: int


class ScoreBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ecs_score: float
    lambda_score: float
    ec2_score: float
    signals: dict[str, bool | str | float]


class DecisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    compute_type: ComputeType
    ecs: Optional[EcsSizing] = None
    lambda_: Optional[LambdaSizing] = Field(default=None, alias="lambda")
    ec2: Optional[Ec2Sizing] = None
    score_breakdown: ScoreBreakdown
    reasoning: list[str] = Field(default_factory=list)


# --- terraform_generator (module 3) -----------------------------------------


class TerraformGenerationResult(BaseModel):
    """Mirrors output_models.TerraformArtifacts' shape, kept separate so
    terraform_generator does not need to import from output_models."""

    model_config = ConfigDict(extra="forbid")

    files: dict[str, str]
    variables: dict[str, str | int | float | bool]


# --- cost_estimator (module 4) / scenario_simulator (module 5) -------------


class CostEstimateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: float
    currency: str = "USD"
    range_min: float
    range_max: float


class ScenarioResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_load: int
    compute_units: int
    cost: CostEstimateResult


# --- finops_optimizer (module 6) --------------------------------------------


class FinOpsOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Literal["spot", "reserved", "graviton", "on_demand"]
    recommended: bool
    reason: str


class FinOpsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommended_option: str
    discarded_options: list[FinOpsOption]
    context_used: dict[str, bool | str]


# --- approval_manager (module 8) --------------------------------------------


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: Literal["pending", "approved", "rejected"] = "pending"
    approved_by: Optional[str] = None
