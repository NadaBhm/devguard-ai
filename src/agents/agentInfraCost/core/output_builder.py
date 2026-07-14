"""Module 7: assembles the final InfraCostOutput contract sent to Agent 3.

Pure assembly of results already computed by decision_engine, cost_estimator,
terraform_generator, approval_manager and llm_enrichment. The single piece of
logic that lives here (rather than upstream) is the Docker image tag
fallback, since it depends only on values already known at assembly time and
the output contract explicitly calls it out as this module's job.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..models.internal_models import CostEstimateResult, DecisionResult
from ..models.output_models import (
    Approval,
    Artifacts,
    AwsConfig,
    DeploymentConfig,
    DockerImage,
    Ec2AwsConfig,
    Ec2DeploymentConfig,
    EcsAwsConfig,
    EcsDeploymentConfig,
    Enrichment,
    EstimatedMonthlyCost,
    InfraCostOutput,
    LambdaAwsConfig,
    LambdaDeploymentConfig,
    TerraformArtifacts,
)

logger = logging.getLogger(__name__)


def resolve_docker_image(*, repo_name: str, commit_sha: Optional[str]) -> DockerImage:
    """Picks the Docker image tag, preferring commit_sha over 'latest'.

    Falling back to 'latest' is accepted but logged as a warning, since it is
    not a safe production practice (see output contract notes).
    """
    if commit_sha:
        return DockerImage(name=repo_name, tag=commit_sha[:12])
    logger.warning(
        "No commit_sha available for repo '%s'; falling back to the 'latest' "
        "Docker tag, which is not recommended for production deployments.",
        repo_name,
    )
    return DockerImage(name=repo_name, tag="latest")


def _to_wire_cost(cost: CostEstimateResult) -> EstimatedMonthlyCost:
    """Converts cost_estimator's internal CostEstimateResult into the wire
    contract's EstimatedMonthlyCost. The two shapes are identical by design
    but are distinct Pydantic classes (internal vs. output_models), so
    Pydantic does not coerce one into the other automatically."""
    return EstimatedMonthlyCost(
        amount=cost.amount,
        currency=cost.currency,
        range_min=cost.range_min,
        range_max=cost.range_max,
    )


def _build_aws_config(
    decision: DecisionResult,
    region: str,
    estimated_monthly_cost: CostEstimateResult,
) -> AwsConfig:
    ecs_block: Optional[EcsAwsConfig] = None
    lambda_block: Optional[LambdaAwsConfig] = None
    ec2_block: Optional[Ec2AwsConfig] = None

    if decision.compute_type == "ecs" and decision.ecs is not None:
        ecs_block = EcsAwsConfig(
            cluster=decision.ecs.cluster,
            service_name=decision.ecs.service_name,
            task_cpu=str(decision.ecs.task_cpu),
            task_memory=str(decision.ecs.task_memory),
        )
    elif decision.compute_type == "lambda" and decision.lambda_ is not None:
        lambda_block = LambdaAwsConfig(
            function_name=decision.lambda_.function_name,
            runtime=decision.lambda_.runtime,
            memory_mb=decision.lambda_.memory_mb,
            timeout_seconds=decision.lambda_.timeout_seconds,
            handler=decision.lambda_.handler,
        )
    elif decision.compute_type == "ec2" and decision.ec2 is not None:
        ec2_block = Ec2AwsConfig(
            instance_type=decision.ec2.instance_type,
            ami_id=decision.ec2.ami_id,
            instance_count=decision.ec2.instance_count,
            key_pair_name=decision.ec2.key_pair_name,
        )
    else:
        raise ValueError(
            f"DecisionResult.compute_type='{decision.compute_type}' but the "
            f"matching sizing block is missing"
        )

    return AwsConfig(
        region=region,
        ecs=ecs_block,
        lambda_=lambda_block,
        ec2=ec2_block,
        estimated_monthly_cost=_to_wire_cost(estimated_monthly_cost),
    )


def _build_deployment_config(decision: DecisionResult) -> DeploymentConfig:
    ecs_block: Optional[EcsDeploymentConfig] = None
    lambda_block: Optional[LambdaDeploymentConfig] = None
    ec2_block: Optional[Ec2DeploymentConfig] = None

    if decision.compute_type == "ecs" and decision.ecs is not None:
        ecs_block = EcsDeploymentConfig(
            health_check_path=decision.ecs.health_check_path,
            health_check_port=decision.ecs.health_check_port,
            timeout_minutes=decision.ecs.timeout_minutes,
            min_healthy_percent=decision.ecs.min_healthy_percent,
            max_percent=decision.ecs.max_percent,
        )
    elif decision.compute_type == "lambda" and decision.lambda_ is not None:
        lambda_block = LambdaDeploymentConfig(
            reserved_concurrency=decision.lambda_.reserved_concurrency,
        )
    elif decision.compute_type == "ec2" and decision.ec2 is not None:
        ec2_block = Ec2DeploymentConfig(
            health_check_path=decision.ec2.health_check_path,
            health_check_port=decision.ec2.health_check_port,
            timeout_minutes=decision.ec2.timeout_minutes,
        )
    else:
        raise ValueError(
            f"DecisionResult.compute_type='{decision.compute_type}' but the "
            f"matching sizing block is missing"
        )

    return DeploymentConfig(ecs=ecs_block, lambda_=lambda_block, ec2=ec2_block)


def build_output(
    *,
    job_id: str,
    decision: DecisionResult,
    estimated_monthly_cost: CostEstimateResult,
    terraform_files: dict[str, str],
    terraform_variables: dict[str, str | int | float | bool],
    region: str,
    repo_name: str,
    commit_sha: Optional[str],
    container_detected: bool,
    dockerfile_content: Optional[str],
    source_code_path: str,
    approval: Approval,
    enrichment: Enrichment,
) -> InfraCostOutput:
    """Assembles the final InfraCostOutput sent to the deployment agent.

    :param decision: output of decision_engine (module 2)
    :param estimated_monthly_cost: output of cost_estimator (module 4)
    :param terraform_files: {"main.tf": ..., "variables.tf": ..., "outputs.tf": ...}
        from terraform_generator (module 3)
    :param container_detected: stack_detection.container.detected from the
        validated input; when False, dockerfile/docker_image are left null
        even for a "lambda" compute_type built without a container image.
    :param approval: output of approval_manager (module 8)
    :param enrichment: output of llm_enrichment (module 10)
    :raises ValueError: if decision.compute_type does not match a populated
        sizing block (indicates a bug upstream in decision_engine).
    """
    docker_image: Optional[DockerImage] = None
    dockerfile: Optional[str] = None
    if container_detected:
        dockerfile = dockerfile_content
        docker_image = resolve_docker_image(repo_name=repo_name, commit_sha=commit_sha)

    artifacts = Artifacts(
        terraform=TerraformArtifacts(files=terraform_files, variables=terraform_variables),
        dockerfile=dockerfile,
        docker_image=docker_image,
        source_code=source_code_path,
    )

    aws_config = _build_aws_config(decision, region, estimated_monthly_cost)
    deployment_config = _build_deployment_config(decision)

    return InfraCostOutput(
        job_id=job_id,
        compute_type=decision.compute_type,
        artifacts=artifacts,
        aws_config=aws_config,
        deployment_config=deployment_config,
        approval=approval,
        enrichment=enrichment,
    )
