"""Pydantic models for the output contract sent to the deployment agent (Agent 3).

`aws_config` and `deployment_config` each carry three sub-blocks, one per
compute type ("ecs", "lambda", "ec2"). All three keys are always present in
the serialized JSON; exactly one is populated and the other two are
explicitly null, matching the sibling root-level `compute_type` field.

This is deliberately NOT a Pydantic discriminated union (Union + Field(
discriminator=...)): a real discriminated union would make the two
non-matching variants' fields absent from the JSON entirely instead of
present-but-null, which conflicts with the "always present" requirement.
Instead, consistency between `compute_type` and the populated sub-block is
enforced by a model validator on `InfraCostOutput`, which makes an
inconsistent instance (e.g. compute_type="lambda" with a non-null `ecs`
block) impossible to construct — `InfraCostOutput(...)` raises a
`ValidationError` before such an object can exist.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .common import ComputeType

# --- aws_config sub-blocks -------------------------------------------------


class EcsAwsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster: str
    service_name: str
    task_cpu: str
    task_memory: str


class LambdaAwsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    function_name: str
    runtime: str
    memory_mb: int
    timeout_seconds: int
    handler: str


class Ec2AwsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_type: str
    ami_id: str
    instance_count: int
    key_pair_name: str


class EstimatedMonthlyCost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: float
    currency: str = "USD"
    range_min: float
    range_max: float


class AwsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    region: str
    ecs: Optional[EcsAwsConfig] = None
    lambda_: Optional[LambdaAwsConfig] = Field(default=None, alias="lambda")
    ec2: Optional[Ec2AwsConfig] = None
    estimated_monthly_cost: EstimatedMonthlyCost


# --- deployment_config sub-blocks ------------------------------------------


class EcsDeploymentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str = "rolling"
    health_check_path: str
    health_check_port: int
    timeout_minutes: int
    min_healthy_percent: int
    max_percent: int


class LambdaDeploymentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str = "rolling"
    reserved_concurrency: Optional[int] = None


class Ec2DeploymentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str = "rolling"
    health_check_path: str
    health_check_port: int
    timeout_minutes: int


class DeploymentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    ecs: Optional[EcsDeploymentConfig] = None
    lambda_: Optional[LambdaDeploymentConfig] = Field(default=None, alias="lambda")
    ec2: Optional[Ec2DeploymentConfig] = None


# --- artifacts ---------------------------------------------------------------


class TerraformArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: dict[str, str]
    variables: dict[str, str | int | float | bool]


class DockerImage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    tag: str


class Artifacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    terraform: TerraformArtifacts
    dockerfile: Optional[str] = None
    docker_image: Optional[DockerImage] = None
    source_code: str


# --- approval / enrichment ---------------------------------------------------


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["pending", "approved", "rejected"] = "pending"
    approved_by: Optional[str] = None


class Enrichment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    architecture_explanation: str
    cost_summary: str
    finops_justification: str
    enrichment_source: Literal["gemini", "fallback"]


# --- root ---------------------------------------------------------------------


class InfraCostOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: str = "1.0"
    job_id: str
    compute_type: ComputeType
    artifacts: Artifacts
    aws_config: AwsConfig
    deployment_config: DeploymentConfig
    approval: Approval
    enrichment: Enrichment

    @model_validator(mode="after")
    def _check_compute_type_consistency(self) -> "InfraCostOutput":
        """Makes it impossible to construct a JSON output where compute_type
        disagrees with which aws_config/deployment_config sub-block is set."""
        variants: dict[ComputeType, tuple[object | None, object | None]] = {
            "ecs": (self.aws_config.ecs, self.deployment_config.ecs),
            "lambda": (self.aws_config.lambda_, self.deployment_config.lambda_),
            "ec2": (self.aws_config.ec2, self.deployment_config.ec2),
        }
        for name, (aws_block, deploy_block) in variants.items():
            should_be_set = name == self.compute_type
            is_fully_set = aws_block is not None and deploy_block is not None
            is_fully_unset = aws_block is None and deploy_block is None

            if should_be_set and not is_fully_set:
                raise ValueError(
                    f"compute_type='{self.compute_type}' requires both "
                    f"aws_config.{name} and deployment_config.{name} to be set"
                )
            if not should_be_set and not is_fully_unset:
                raise ValueError(
                    f"aws_config.{name} and deployment_config.{name} must both "
                    f"be null when compute_type='{self.compute_type}' "
                    f"(got aws={aws_block!r}, deployment={deploy_block!r})"
                )
        return self
