"""Step 7: assemble the final output contract.

No business logic — earlier modules made every decision; this routes them into
``models.output_schema.InfraCostOutput`` (one aws_config/deployment_config block
fills, the others stay explicitly null) plus the Docker tag fallback (commit_sha
else "latest" + a warning, never silently).
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
from core.deploy_templates import match_template as _match_template
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

# Runnable stub bodies per language: bare FROM+COPY exits immediately (base-image
# CMD is a no-op), crash-looping the task until every health probe times out.
_STUB_RUN: Final[dict[str, str]] = {
    "javascript": 'EXPOSE 3000\nENV PORT=3000\nCMD ["npx", "-y", "http-server", "-p", "3000", "."]',
    "typescript": 'EXPOSE 3000\nENV PORT=3000\nCMD ["npx", "-y", "http-server", "-p", "3000", "."]',
    "python": 'EXPOSE 8080\nCMD ["python", "-m", "http.server", "8080", "--bind", "0.0.0.0"]',
    "ruby": 'EXPOSE 8080\nCMD ["ruby", "-run", "-e", "httpd", ".", "-p", "8080"]',
    "php": 'EXPOSE 8080\nCMD ["php", "-S", "0.0.0.0:8080", "-t", "."]',
}


def _stub_dockerfile(base_image: str, primary_language: str) -> str:
    body = _STUB_RUN.get(primary_language, f'CMD ["{primary_language}"]')
    return f"FROM {base_image}\nWORKDIR /app\nCOPY . .\n{body}\n"


def _health_check_from_terraform(main_tf: str) -> tuple[int, str]:
    """Read container port + health-check path from the rendered/refined
    ``aws_lb_target_group`` block — DeployOps' post-deploy probe must match the
    Terraform that ships, not the template defaults. Fail-soft to constants."""
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
    """Same contract as ``_health_check_from_terraform`` for the EC2 path: read
    health_check_port/path from the ``locals`` block the Gate-2 refiner may have
    corrected, so the post-deploy probe hits what ships. Fail-soft to constants."""
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
    """Derive a unique ECR image name: the first container keeps canonical
    DOCKER_IMAGE_NAME, later ones suffix from the Dockerfile path (backend/Dockerfile
    -> devguard-app-backend) or index, so concurrent images never collide."""
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
    """Build-context directory (repo-relative): backend/Dockerfile -> "backend",
    root-level Dockerfile -> ".".
    """
    if not dockerfile_path:
        return "."
    parent = Path(dockerfile_path).parent.as_posix()
    return "." if parent == "." else parent


def resolve_docker_artifacts(
    analysis: RepoAnalysisInput, decision: DecisionResult
) -> list[DockerImage]:
    """Decide ``docker_images`` — one entry per detected container, empty for a
    Lambda zip / S3 deploy. Each carries its own dockerfile (real CodeSec capture
    else a synthesized stand-in) and build context; tags fall back to "latest" with
    a warning, never silently. Public: rendering needs images before build_output."""
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
        # Template match takes priority: proven Dockerfiles for known frameworks
        tmpl = _match_template(
            analysis.stack_detection.primary_language,
            analysis.stack_detection.frameworks,
            analysis.stack_detection.detected_files,
        ) if index == 0 and not container.dockerfile_content else None

        if tmpl:
            dockerfile = tmpl["dockerfile"]
            logger.info("Using deploy template for %s/%s",
                        analysis.stack_detection.primary_language,
                        ",".join(analysis.stack_detection.frameworks[:3]))
        elif container.dockerfile_content:
            dockerfile = container.dockerfile_content
        else:
            dockerfile = _stub_dockerfile(base_image, analysis.stack_detection.primary_language)
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
        # S3 variables.tf declares bucket_name with no default — it must ride along
        # in tfvars or plan fails with "No value for required variable".
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
            # Suffixed with job_id — must match terraform_generator's _ecs_render_context
            # exactly or DeployOps learns the wrong (unsuffixed) names.
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
                # Read from the actual rendered/refined Terraform so DeployOps' post-deploy
                # probe matches what ships; the constants are only starting points.
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
                # Same as ECS: read from the actual rendered/refined Terraform (refiner
                # corrects the template starting point) so the post-deploy probe matches.
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
    """Assemble the final contract from already-computed outputs. ``docker_images``
    arrive pre-resolved via resolve_docker_artifacts (resolved here if omitted);
    ``dockerfile``/``docker_image`` are legacy aliases ignored when docker_images is
    given; the rest stamp assembly time. Unused aws_config/deployment_config blocks
    stay explicitly null."""
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
