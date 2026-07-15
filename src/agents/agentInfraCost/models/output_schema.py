"""Pydantic models for the InfraCost Agent's output contract.

The output is a **discriminated union** on ``compute_type``. Each variant
(``EcsInfraCostOutput`` / ``LambdaInfraCostOutput`` / ``Ec2InfraCostOutput``)
hard-types the ``aws_config`` and ``deployment_config`` sub-blocks so that
the two blocks that do *not* match ``compute_type`` can only ever be
``None`` — an inconsistent payload (e.g. ``compute_type="lambda"`` with a
non-null ``ecs`` block) cannot be constructed, not merely rejected after the
fact.

The literal key ``"lambda"`` collides with the Python keyword, so those
fields are declared as ``lambda_`` with ``alias="lambda"``; every model in
this module accepts both the field name and the alias on input
(``populate_by_name=True``) and always serializes using the alias via
``model_dump(by_alias=True)``.
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

# --------------------------------------------------------------------------
# Artifacts
# --------------------------------------------------------------------------


class TerraformFiles(BaseModel):
    """The three generated Terraform files, keyed by their real filename."""

    model_config = ConfigDict(populate_by_name=True)

    main_tf: str = Field(alias="main.tf")
    variables_tf: str = Field(alias="variables.tf")
    outputs_tf: str = Field(alias="outputs.tf")


class TerraformArtifacts(BaseModel):
    """Generated Terraform files plus the variable values used to render them."""

    files: TerraformFiles
    variables: dict[str, str]


class DockerImage(BaseModel):
    """Docker image identity to be built/pushed by Agent 3."""

    name: str
    tag: str


class Artifacts(BaseModel):
    """Deployable artifacts, shared shape across all compute types.

    ``dockerfile`` / ``docker_image`` are ``None`` for a Lambda deployed as
    a plain zip (no container detected upstream).
    """

    terraform: TerraformArtifacts
    dockerfile: Optional[str] = None
    docker_image: Optional[DockerImage] = None
    source_code: str


# --------------------------------------------------------------------------
# Cost
# --------------------------------------------------------------------------


class Money(BaseModel):
    """A monetary estimate with an uncertainty range."""

    amount: float = Field(ge=0)
    currency: str = "USD"
    range_min: float = Field(ge=0)
    range_max: float = Field(ge=0)

    @model_validator(mode="after")
    def _check_range(self) -> "Money":
        if not (self.range_min <= self.amount <= self.range_max):
            raise ValueError(
                "amount must lie within [range_min, range_max] "
                f"(got amount={self.amount}, range_min={self.range_min}, "
                f"range_max={self.range_max})"
            )
        return self


# --------------------------------------------------------------------------
# Per-compute-type aws_config blocks
# --------------------------------------------------------------------------


class EcsAwsConfig(BaseModel):
    cluster: str
    service_name: str
    task_cpu: str
    task_memory: str


class LambdaAwsConfig(BaseModel):
    function_name: str
    runtime: str
    memory_mb: int = Field(gt=0)
    timeout_seconds: int = Field(gt=0)
    handler: str


class Ec2AwsConfig(BaseModel):
    instance_type: str
    ami_id: str
    instance_count: int = Field(gt=0)
    key_pair_name: str


# --------------------------------------------------------------------------
# Per-compute-type deployment_config blocks
# --------------------------------------------------------------------------


class EcsDeploymentConfig(BaseModel):
    strategy: str
    health_check_path: str
    health_check_port: int = Field(gt=0, le=65535)
    timeout_minutes: int = Field(gt=0)
    min_healthy_percent: int = Field(ge=0)
    max_percent: int = Field(ge=0)


class LambdaDeploymentConfig(BaseModel):
    strategy: str
    reserved_concurrency: Optional[int] = Field(default=None, ge=0)


class Ec2DeploymentConfig(BaseModel):
    strategy: str
    health_check_path: str
    health_check_port: int = Field(gt=0, le=65535)
    timeout_minutes: int = Field(gt=0)


# --------------------------------------------------------------------------
# aws_config wrappers — exactly one block non-null, enforced by typing
# --------------------------------------------------------------------------


class AwsConfigEcs(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    region: str
    estimated_monthly_cost: Money
    ecs: EcsAwsConfig
    lambda_: None = Field(default=None, alias="lambda")
    ec2: None = None


class AwsConfigLambda(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    region: str
    estimated_monthly_cost: Money
    ecs: None = None
    lambda_: LambdaAwsConfig = Field(alias="lambda")
    ec2: None = None


class AwsConfigEc2(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    region: str
    estimated_monthly_cost: Money
    ecs: None = None
    lambda_: None = Field(default=None, alias="lambda")
    ec2: Ec2AwsConfig


# --------------------------------------------------------------------------
# deployment_config wrappers — exactly one block non-null, enforced by typing
# --------------------------------------------------------------------------


class DeploymentConfigEcs(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ecs: EcsDeploymentConfig
    lambda_: None = Field(default=None, alias="lambda")
    ec2: None = None


class DeploymentConfigLambda(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ecs: None = None
    lambda_: LambdaDeploymentConfig = Field(alias="lambda")
    ec2: None = None


class DeploymentConfigEc2(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ecs: None = None
    lambda_: None = Field(default=None, alias="lambda")
    ec2: Ec2DeploymentConfig


# --------------------------------------------------------------------------
# Approval & enrichment
# --------------------------------------------------------------------------


class Approval(BaseModel):
    """State of the human-approval gate that sits between this agent and Agent 3."""

    status: Literal["pending", "approved", "rejected"]
    approved_by: Optional[str] = None


class Enrichment(BaseModel):
    """LLM-generated explanatory text. Never influences decisions or numbers."""

    architecture_explanation: str
    cost_summary: str
    finops_justification: str
    enrichment_source: Literal["gemini", "fallback"]


# --------------------------------------------------------------------------
# Discriminated union on compute_type
# --------------------------------------------------------------------------


class EcsInfraCostOutput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: str = "1.1"
    job_id: str
    compute_type: Literal["ecs"] = "ecs"
    artifacts: Artifacts
    aws_config: AwsConfigEcs
    deployment_config: DeploymentConfigEcs
    approval: Approval
    enrichment: Enrichment


class LambdaInfraCostOutput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: str = "1.1"
    job_id: str
    compute_type: Literal["lambda"] = "lambda"
    artifacts: Artifacts
    aws_config: AwsConfigLambda
    deployment_config: DeploymentConfigLambda
    approval: Approval
    enrichment: Enrichment


class Ec2InfraCostOutput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_version: str = "1.1"
    job_id: str
    compute_type: Literal["ec2"] = "ec2"
    artifacts: Artifacts
    aws_config: AwsConfigEc2
    deployment_config: DeploymentConfigEc2
    approval: Approval
    enrichment: Enrichment


InfraCostOutput = Annotated[
    Union[EcsInfraCostOutput, LambdaInfraCostOutput, Ec2InfraCostOutput],
    Field(discriminator="compute_type"),
]
