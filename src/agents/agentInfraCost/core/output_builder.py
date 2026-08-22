"""Step 7 of the InfraCost pipeline: assemble the final output contract.

No business logic here — every decision (which compute type, its sizing,
its cost, its Terraform, its FinOps strategy) has already been made by
modules 1-6 and 10. This module only routes those already-computed values
into the exact shape of ``models.output_schema.InfraCostOutput``: which of
``aws_config``/``deployment_config``'s four blocks gets filled in (the
other three are always ``null``, never omitted), and the one piece of real
assembly logic it owns — the Docker image tag fallback (``commit_sha`` if
available, else ``"latest"`` plus a warning, never silently).
"""

from __future__ import annotations

import logging
import re
from typing import Final
from pathlib import Path

from core.constants import (
    DOCKER_IMAGE_NAME,
    EC2_AMI_ID,
    EC2_HEALTH_CHECK_PATH,
    EC2_HEALTH_CHECK_PORT,
    EC2_KEY_PAIR_NAME,
    ECS_CLUSTER_NAME,
    unique_resource_name,
    ECS_HEALTH_CHECK_PATH,
    ECS_HEALTH_CHECK_PORT,
    ECS_SERVICE_NAME,
    LAMBDA_FUNCTION_NAME,
    LAMBDA_HANDLER,
    LAMBDA_RUNTIME,
    LAMBDA_TIMEOUT_SECONDS,
    S3_BUCKET_PREFIX,
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
    AwsConfigS3,
    DeploymentConfigEc2,
    DeploymentConfigEcs,
    DeploymentConfigLambda,
    DeploymentConfigS3,
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
    S3AwsConfig,
    S3DeploymentConfig,
    S3InfraCostOutput,
    TerraformArtifacts,
    TerraformFiles,
)

logger = logging.getLogger(__name__)

# Language-aware base image for repos without a Dockerfile.
_FALLBACK_BASE_IMAGES: Final[dict[str, str]] = {
    "python": "python:3.12-slim",
    "javascript": "node:20-alpine",
    "typescript": "node:20-alpine",
    "go": "golang:1.21-alpine",
    "java": "maven:3.9-eclipse-temurin-17",
    "php": "php:8.2-cli",
    "ruby": "ruby:3.2-slim",
    "rust": "rust:1.75-slim",
    "csharp": "mcr.microsoft.com/dotnet/sdk:8.0",
}


def _health_check_from_terraform(main_tf: str) -> tuple[int, str]:
    """Read the container port and health-check path out of the rendered
    (and possibly refiner-edited) ``aws_lb_target_group`` block.

    The ECS template renders ``port`` and ``health_check.path`` from the
    constants (8080 + "/health"), but the Gate-2 refiner legitimately
    rewrites them to match the app (e.g. 3000 + "/api/health"). The
    ``deployment_config`` DeployOps consumes for its post-deploy health
    check must agree with the Terraform that actually ships, so this returns
    what main.tf really says. Fail-soft: falls back to the constants if the
    block is unreadable.
    """
    tg = re.search(
        r'resource\s+"aws_lb_target_group"\s+"[^"]*"\s*\{.*?\n\}',
        main_tf,
        re.DOTALL,
    )
    if tg is None:
        return ECS_HEALTH_CHECK_PORT, ECS_HEALTH_CHECK_PATH
    block = tg.group(0)
    port_match = re.search(r"\bport\s*=\s*(\d+)", block)
    path_match = re.search(r'\bpath\s*=\s*"([^"]+)"', block)
    port = int(port_match.group(1)) if port_match else ECS_HEALTH_CHECK_PORT
    path = path_match.group(1) if path_match else ECS_HEALTH_CHECK_PATH
    return port, path


def _ec2_health_check_from_terraform(main_tf: str) -> tuple[int, str]:
    """Read the container port and health-check path out of the rendered
    (and possibly refiner-edited) EC2 ``locals`` block.

    Same contract as ``_health_check_from_terraform`` but for the EC2 path,
    which has no ALB target group — the template renders
    ``health_check_port`` / ``health_check_path`` into a ``locals`` block
    that the Gate-2 refiner can correct to match the app, and this reads
    back what actually ships so DeployOps' post-deploy health check probes
    the right port/path. Fail-soft: falls back to the constants if the block
    is unreadable.
    """
    block = re.search(r"locals\s*\{.*?\n\}", main_tf, re.DOTALL)
    if block is None:
        return EC2_HEALTH_CHECK_PORT, EC2_HEALTH_CHECK_PATH
    body = block.group(0)
    port_match = re.search(r"health_check_port\s*=\s*(\d+)", body)
    path_match = re.search(r'health_check_path\s*=\s*"([^"]+)"', body)
    port = int(port_match.group(1)) if port_match else EC2_HEALTH_CHECK_PORT
    path = path_match.group(1) if path_match else EC2_HEALTH_CHECK_PATH
    return port, path


def _image_name_for(dockerfile_path: str | None, index: int) -> str:
    """Derive a unique ECR image name for a container.

    The first container keeps the canonical ``DOCKER_IMAGE_NAME``. Additional
    containers get a suffix derived from their Dockerfile path so concurrent
    images never collide in ECR: ``backend/Dockerfile`` -> ``devguard-app-backend``,
    ``Dockerfile.web`` -> ``devguard-app-web``. Falls back to an index suffix
    when the path carries nothing usable.
    """
    if index == 0 or not dockerfile_path:
        return DOCKER_IMAGE_NAME
    p = Path(dockerfile_path)
    if p.name.lower().startswith("dockerfile"):
        stem = re.sub(r"^dockerfile", "", p.stem, flags=re.IGNORECASE).lstrip(".-_")
        if not stem:
            stem = p.parent.name or ""
    else:
        stem = p.parent.name or ""
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "-", stem).strip("-").lower()
    return f"{DOCKER_IMAGE_NAME}-{stem}" if stem else f"{DOCKER_IMAGE_NAME}-{index}"


def _dockerfile_context(dockerfile_path: str | None) -> str:
    """Build-context directory (repo-relative) for a Dockerfile.

    ``backend/Dockerfile`` builds from ``backend``; a root-level Dockerfile
    (or any ``Dockerfile`` in the repo root) builds from ``"."``.
    """
    if not dockerfile_path:
        return "."
    parent = Path(dockerfile_path).parent.as_posix()
    return "." if parent == "." else parent


def resolve_docker_artifacts(
    analysis: RepoAnalysisInput, decision: DecisionResult
) -> list[DockerImage]:
    """Decide ``docker_images`` — one entry per detected container, or an
    empty list for a Lambda zip / S3 deploy (no container detected upstream).

    Each returned ``DockerImage`` carries its own ``dockerfile`` content
    (real content the CodeSec scanner captured, else a synthesized
    ``FROM {base_image}\\nCOPY . /app\\n`` stand-in) and ``context``. Tags
    fall back to ``"latest"`` with a warning if ``commit_sha`` is unavailable
    — never silently.

    Public (not ``_``-prefixed): module 3 (``terraform_generator``) needs
    the resolved image string *before* rendering ECS/EC2 templates, so the
    caller (today, ``main.py``; later, module 9's ``pipeline.py``) must be
    able to call this ahead of ``build_output`` rather than only getting it
    buried inside the final assembly.
    """
    is_lambda_zip = (
        decision.compute_type == "lambda"
        and not (
            analysis.stack_detection.container is not None
            and analysis.stack_detection.container.detected
        )
    )
    # S3 static sites ship plain files — no container to build.
    if is_lambda_zip or decision.compute_type == "s3":
        return []

    commit_sha = analysis.repo_metadata.commit_sha
    if commit_sha:
        tag = f"sha-{commit_sha[:7]}"
    else:
        tag = "latest"
        logger.warning(
            "job_id=%s has no commit_sha; falling back to docker tag 'latest'",
            analysis.job_id,
        )

    containers = analysis.stack_detection.containers or [
        analysis.stack_detection.container
    ]
    # Language-aware base image for repos without a Dockerfile.
    fallback_base = _FALLBACK_BASE_IMAGES.get(
        analysis.stack_detection.primary_language, "python:3.12-slim"
    )
    images: list[DockerImage] = []
    for index, container in enumerate(containers):
        base_image = container.base_image or fallback_base
        dockerfile = container.dockerfile_content or f"FROM {base_image}\nCOPY . /app\n"
        images.append(
            DockerImage(
                name=_image_name_for(container.dockerfile_path, index),
                tag=tag,
                dockerfile=dockerfile,
                context=_dockerfile_context(container.dockerfile_path),
            )
        )
    return images


def _build_artifacts(
    analysis: RepoAnalysisInput,
    terraform_files: TerraformFiles,
    docker_images: list[DockerImage],
    source_code: str = ".",
    region: str = "us-east-1",
    environment: str = "dev",
    compute_type: str = "ecs",
) -> Artifacts:
    variables: dict[str, str] = {"region": region, "environment": environment}
    if compute_type == "s3":
        # The S3 variables.tf declares bucket_name (no default) — the value
        # must ride along in tfvars or terraform plan fails with "No value
        # for required variable".
        variables["bucket_name"] = f"{S3_BUCKET_PREFIX}-{analysis.job_id[:32].lower()}"
    terraform = TerraformArtifacts(
        files=terraform_files,
        variables=variables,
    )
    return Artifacts(
        terraform=terraform,
        docker_images=docker_images,
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
            # Suffixed with job_id -- must match terraform_generator.py's
            # _ecs_render_context exactly, or DeployOps would be told the
            # wrong (unsuffixed) cluster/service name for a real resource
            # Terraform created under the suffixed one.
            ecs=EcsAwsConfig(
                cluster=unique_resource_name(ECS_CLUSTER_NAME, analysis.job_id),
                service_name=unique_resource_name(ECS_SERVICE_NAME, analysis.job_id),
                task_cpu=str(sizing["task_cpu"]),
                task_memory=str(sizing["task_memory"]),
            ),
        ),
        deployment_config=DeploymentConfigEcs(
            ecs=EcsDeploymentConfig(
                strategy="rolling",
                # Read from the actual rendered/refined Terraform so the
                # post-deploy health check DeployOps performs hits the same
                # port/path the ALB target group ships with — the constants
                # (8080 + "/health") are only the template's starting point.
                health_check_path=_health_check_from_terraform(
                    artifacts.terraform.files.main_tf
                )[1],
                health_check_port=_health_check_from_terraform(
                    artifacts.terraform.files.main_tf
                )[0],
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
                # Read from the actual rendered/refined Terraform so the
                # post-deploy health check DeployOps performs hits the same
                # port/path the instance ships with — the constants
                # (8080 + "/health") are only the template's starting point,
                # and the refiner corrects them to match the app.
                health_check_path=_ec2_health_check_from_terraform(
                    artifacts.terraform.files.main_tf
                )[1],
                health_check_port=_ec2_health_check_from_terraform(
                    artifacts.terraform.files.main_tf
                )[0],
                timeout_minutes=5,
            )
        ),
        approval=Approval(status=approval_status),
        enrichment=enrichment,
    )


def _build_s3_output(
    analysis: RepoAnalysisInput,
    decision: DecisionResult,
    artifacts: Artifacts,
    cost: Money,
    enrichment: Enrichment,
    approval_status: ApprovalStatus,
    region: str = "us-east-1",
) -> S3InfraCostOutput:
    bucket_name = f"{S3_BUCKET_PREFIX}-{analysis.job_id[:32].lower()}"
    return S3InfraCostOutput(
        job_id=analysis.job_id,
        artifacts=artifacts,
        aws_config=AwsConfigS3(
            region=region,
            estimated_monthly_cost=cost,
            s3=S3AwsConfig(bucket_name=bucket_name),
        ),
        deployment_config=DeploymentConfigS3(
            s3=S3DeploymentConfig(
                strategy="static",
                health_check_path="/",
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
    "s3": _build_s3_output,
}


def build_output(
    analysis: RepoAnalysisInput,
    decision: DecisionResult,
    terraform_files: TerraformFiles,
    cost: Money,
    enrichment: Enrichment,
    docker_images: list[DockerImage] | None = None,
    dockerfile: str | None = None,
    docker_image: DockerImage | None = None,
    approval_status: ApprovalStatus = "pending",
    source_code: str = ".",
    region: str = "us-east-1",
    environment: str = "dev",
) -> InfraCostOutput:
    """Assemble the final contract from already-computed module outputs.

    Args:
        docker_images: Pre-resolved via ``resolve_docker_artifacts`` — module 3
            already needed the same values to render templates; if omitted,
            resolved on the spot (convenient for tests).
        dockerfile: Legacy singular alias for the first image's Dockerfile
            content. Ignored when ``docker_images`` is provided; the first
            image's content wins otherwise if ``docker_images`` is empty.
        docker_image: Legacy singular alias for the first image. Ignored when
            ``docker_images`` is provided.
        approval_status: Defaults to "pending" — module 8 owns the real
            state machine; this is just what gets stamped on assembly.
        source_code: Defaults to "." — a relative build context, never a
            fabricated ``/tmp`` checkout path that nobody created.
        region/environment: Threaded through so the metadata contract never
            diverges from the rendered Terraform's defaults.

    Returns:
        One of the four ``InfraCostOutput`` variants, with the other
        ``aws_config``/``deployment_config`` blocks explicitly ``null``.
    """
    if docker_images is None:
        docker_images = resolve_docker_artifacts(analysis, decision)
    if not docker_images and (dockerfile is not None or docker_image is not None):
        docker_images = [
            DockerImage(
                name=(docker_image.name if docker_image else DOCKER_IMAGE_NAME),
                tag=(docker_image.tag if docker_image else "latest"),
                dockerfile=dockerfile,
                context=(docker_image.context if docker_image else source_code or "."),
            )
        ]
    artifacts = _build_artifacts(
        analysis, terraform_files, docker_images, source_code, region, environment,
        compute_type=decision.compute_type,
    )
    builder = _BUILDERS[decision.compute_type]
    return builder(analysis, decision, artifacts, cost, enrichment, approval_status, region)
