"""Step 7 of the InfraCost pipeline: assemble the final output contract.

No business logic here — every decision (which compute type, its sizing,
its cost, its Terraform, its FinOps strategy) has already been made by
modules 1-6 and 10. This module only routes those already-computed values
into the exact shape of ``models.output_schema.InfraCostOutput``: which of
``aws_config``/``deployment_config``'s three blocks gets filled in (the
other two are always ``null``, never omitted), and the one piece of real
assembly logic it owns — the Docker image tag fallback (``commit_sha`` if
available, else ``"latest"`` plus a warning, never silently).
"""

from __future__ import annotations

import logging

from core.constants import (
    DOCKER_IMAGE_NAME,
    EC2_AMI_ID,
    EC2_KEY_PAIR_NAME,
    ECS_CLUSTER_NAME,
    ECS_HEALTH_CHECK_PATH,
    ECS_HEALTH_CHECK_PORT,
    ECS_SERVICE_NAME,
    LAMBDA_FUNCTION_NAME,
    LAMBDA_HANDLER,
    LAMBDA_RUNTIME,
    LAMBDA_TIMEOUT_SECONDS,
)
from core.decision_engine import DecisionResult
from models.input_schema import RepoAnalysisInput
from models.output_schema import (
    Approval,
    ApprovalStatus,
    Artifacts,
    AwsConfigEc2,
    AwsConfigEcs,
    AwsConfigLambda,
    DeploymentConfigEc2,
    DeploymentConfigEcs,
    DeploymentConfigLambda,
    DockerImage,
    Ec2AwsConfig,
    Ec2DeploymentConfig,
    Ec2InfraCostOutput,
    EcsAwsConfig,
    EcsDeploymentConfig,
    EcsInfraCostOutput,
    Enrichment,
    InfraCostOutput,
    LambdaAwsConfig,
    LambdaDeploymentConfig,
    LambdaInfraCostOutput,
    Money,
    TerraformArtifacts,
    TerraformFiles,
)

logger = logging.getLogger(__name__)


def resolve_docker_artifacts(
    analysis: RepoAnalysisInput, decision: DecisionResult
) -> tuple[str | None, DockerImage | None]:
    """Decide ``dockerfile`` / ``docker_image`` — ``None`` for a Lambda zip
    deploy (no container detected upstream), otherwise a real Dockerfile
    and image tag, falling back to ``"latest"`` with a warning if
    ``commit_sha`` is unavailable — never silently.

    Public (not ``_``-prefixed): module 3 (``terraform_generator``) needs
    the resolved image string *before* rendering ECS/EC2 templates, so the
    caller (today, ``main.py``; later, module 9's ``pipeline.py``) must be
    able to call this ahead of ``build_output`` rather than only getting it
    buried inside the final assembly.
    """
    is_lambda_zip = (
        decision.compute_type == "lambda"
        and not analysis.stack_detection.container.detected
    )
    if is_lambda_zip:
        return None, None

    commit_sha = analysis.repo_metadata.commit_sha
    if commit_sha:
        tag = f"sha-{commit_sha[:7]}"
    else:
        tag = "latest"
        logger.warning(
            "job_id=%s has no commit_sha; falling back to docker tag 'latest'",
            analysis.job_id,
        )
    base_image = analysis.stack_detection.container.base_image or "python:3.12-slim"
    dockerfile = f"FROM {base_image}\nCOPY . /app\n"
    return dockerfile, DockerImage(name=DOCKER_IMAGE_NAME, tag=tag)


def _build_artifacts(
    analysis: RepoAnalysisInput,
    terraform_files: TerraformFiles,
    dockerfile: str | None,
    docker_image: DockerImage | None,
    source_code: str = ".",
    region: str = "us-east-1",
    environment: str = "dev",
) -> Artifacts:
    terraform = TerraformArtifacts(
        files=terraform_files,
        variables={"region": region, "environment": environment},
    )
    return Artifacts(
        terraform=terraform,
        dockerfile=dockerfile,
        docker_image=docker_image,
        source_code=source_code,
    )


def _build_ecs_output(
    analysis: RepoAnalysisInput,
    decision: DecisionResult,
    artifacts: Artifacts,
    cost: Money,
    enrichment: Enrichment,
    approval_status: ApprovalStatus,
    region: str = "us-east-1",
) -> EcsInfraCostOutput:
    sizing = decision.sizing
    return EcsInfraCostOutput(
        job_id=analysis.job_id,
        artifacts=artifacts,
        aws_config=AwsConfigEcs(
            region=region,
            estimated_monthly_cost=cost,
            ecs=EcsAwsConfig(
                cluster=ECS_CLUSTER_NAME,
                service_name=ECS_SERVICE_NAME,
                task_cpu=str(sizing["task_cpu"]),
                task_memory=str(sizing["task_memory"]),
            ),
        ),
        deployment_config=DeploymentConfigEcs(
            ecs=EcsDeploymentConfig(
                strategy="rolling",
                health_check_path=ECS_HEALTH_CHECK_PATH,
                health_check_port=ECS_HEALTH_CHECK_PORT,
                timeout_minutes=5,
                min_healthy_percent=50,
                max_percent=200,
            )
        ),
        approval=Approval(status=approval_status),
        enrichment=enrichment,
    )


def _build_lambda_output(
    analysis: RepoAnalysisInput,
    decision: DecisionResult,
    artifacts: Artifacts,
    cost: Money,
    enrichment: Enrichment,
    approval_status: ApprovalStatus,
    region: str = "us-east-1",
) -> LambdaInfraCostOutput:
    sizing = decision.sizing
    return LambdaInfraCostOutput(
        job_id=analysis.job_id,
        artifacts=artifacts,
        aws_config=AwsConfigLambda(
            region=region,
            estimated_monthly_cost=cost,
            lambda_=LambdaAwsConfig(
                function_name=LAMBDA_FUNCTION_NAME,
                runtime=LAMBDA_RUNTIME,
                memory_mb=int(sizing["memory_mb"]),
                timeout_seconds=LAMBDA_TIMEOUT_SECONDS,
                handler=LAMBDA_HANDLER,
            ),
        ),
        deployment_config=DeploymentConfigLambda(
            lambda_=LambdaDeploymentConfig(strategy="all_at_once", reserved_concurrency=None)
        ),
        approval=Approval(status=approval_status),
        enrichment=enrichment,
    )


def _build_ec2_output(
    analysis: RepoAnalysisInput,
    decision: DecisionResult,
    artifacts: Artifacts,
    cost: Money,
    enrichment: Enrichment,
    approval_status: ApprovalStatus,
    region: str = "us-east-1",
) -> Ec2InfraCostOutput:
    sizing = decision.sizing
    return Ec2InfraCostOutput(
        job_id=analysis.job_id,
        artifacts=artifacts,
        aws_config=AwsConfigEc2(
            region=region,
            estimated_monthly_cost=cost,
            ec2=Ec2AwsConfig(
                instance_type=str(sizing["instance_type"]),
                ami_id=EC2_AMI_ID,
                instance_count=1,
                key_pair_name=EC2_KEY_PAIR_NAME,
            ),
        ),
        deployment_config=DeploymentConfigEc2(
            ec2=Ec2DeploymentConfig(
                strategy="rolling",
                health_check_path=ECS_HEALTH_CHECK_PATH,
                health_check_port=ECS_HEALTH_CHECK_PORT,
                timeout_minutes=5,
            )
        ),
        approval=Approval(status=approval_status),
        enrichment=enrichment,
    )


_BUILDERS = {
    "ecs": _build_ecs_output,
    "lambda": _build_lambda_output,
    "ec2": _build_ec2_output,
}


def build_output(
    analysis: RepoAnalysisInput,
    decision: DecisionResult,
    terraform_files: TerraformFiles,
    cost: Money,
    enrichment: Enrichment,
    dockerfile: str | None = None,
    docker_image: DockerImage | None = None,
    approval_status: ApprovalStatus = "pending",
    source_code: str = ".",
    region: str = "us-east-1",
    environment: str = "dev",
) -> InfraCostOutput:
    """Assemble the final contract from already-computed module outputs.

    Args:
        analysis: Module 1's output.
        decision: Module 2's output — routes which variant is built.
        terraform_files: Module 3's output.
        cost: Module 4's output.
        enrichment: Module 10's output (or a fallback), never computed here.
        dockerfile: Pre-resolved via ``resolve_docker_artifacts`` — passed
            in rather than recomputed here, since module 3 already needed
            the same value to render ECS/EC2 templates. If omitted, it is
            resolved on the spot (convenient for callers, like tests, that
            don't already have it from a prior ``generate_terraform`` call).
        docker_image: Same as ``dockerfile``, resolved together.
        approval_status: Defaults to "pending" — module 8 owns the real
            state machine; this is just what gets stamped on assembly.
        source_code: Where the deployable source lives for tooling that
            consumes these artifacts (e.g. the DeployOps agent). Defaults
            to ``"."`` — a relative build context, never a fabricated
            ``/tmp`` checkout path that nobody created.
        region: Deployment region, threaded through from the decided
            deployment context so the metadata contract never diverges from
            the rendered Terraform's ``variable "region"`` default.
        environment: Same provenance as ``region`` — stamped into
            ``Artifacts.variables.environment``.

    Returns:
        One of the three ``InfraCostOutput`` variants, with the other two
        ``aws_config``/``deployment_config`` blocks explicitly ``null``.
    """
    if dockerfile is None and docker_image is None:
        dockerfile, docker_image = resolve_docker_artifacts(analysis, decision)
    artifacts = _build_artifacts(
        analysis, terraform_files, dockerfile, docker_image, source_code, region, environment
    )
    builder = _BUILDERS[decision.compute_type]
    return builder(analysis, decision, artifacts, cost, enrichment, approval_status, region)
