"""Step 3: render Terraform files from templates.

Deterministic — no LLM writes Terraform here: this module picks the Jinja2
template set for ``decision.compute_type`` (under ``templates/<compute_type>/``)
and fills in module-2 decisions plus identity context, inventing no architecture
choices itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel

from core.constants import (
    EC2_AMI_ID,
    EC2_HEALTH_CHECK_PATH,
    EC2_INSTANCE_COUNT,
    EC2_INSTANCE_NAME,
    EC2_INSTANCE_PORT,
    EC2_KEY_PAIR_NAME,
    ECS_CLUSTER_NAME,
    unique_resource_name,
    ECS_HEALTH_CHECK_PATH,
    ECS_HEALTH_CHECK_PORT,
    ECS_SERVICE_NAME,
    ECS_TASK_EXECUTION_ROLE_NAME,
    LAMBDA_FUNCTION_NAME,
    LAMBDA_HANDLER,
    LAMBDA_RUNTIME,
    LAMBDA_TIMEOUT_SECONDS,
    S3_BUCKET_PREFIX,
    S3_ERROR_DOCUMENT,
    S3_INDEX_DOCUMENT,
)
from core.decision_engine import DecisionResult
from models.output_schema import TerraformFiles

_TEMPLATES_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "templates"

_ENV: Final[Environment] = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,
)

_TEMPLATE_FILENAMES: Final[tuple[str, ...]] = ("main.tf", "variables.tf", "outputs.tf")

# Conventions filling template vars module 2 doesn't decide (naming, ports, IAM).
# Single source of truth: core/constants.py, imported by modules 3 AND 7 (no drift).


class TerraformContext(BaseModel):
    """Inputs needed to render Terraform that module 2 doesn't provide — identity/
    environment details, never architecture choices (those ride on DecisionResult).
    """

    job_id: str
    region: str = "us-east-1"
    environment: str = "dev"
    docker_image: str | None = None
    docker_images: list[dict[str, Any]] | None = None
    """Multi-container mode: one ``{"name", "image", "port", "context"}`` entry per
    container; the ECS template renders one container_definitions entry per image
    and routes the ALB to the first (primary). None keeps legacy single-container."""
    source_code_path: str | None = None
    health_check_port: int | None = None
    """Real container port, extracted from the repo Dockerfile's EXPOSE by
    pipeline.py. None falls back to ECS_HEALTH_CHECK_PORT (8080) — a confirmed
    mismatch once 502'd every health check on a port-8000 FastAPI app -> rollback."""
    account_id: str | None = None
    database: str | None = None
    """Database engine module 1 detected (e.g. "postgresql"), if any. Only the ECS
    template uses it, to declare (not create) connection variables a deployer fills.
    """


def _ecs_render_context(decision: DecisionResult, context: TerraformContext) -> dict[str, Any]:
    # job_id-suffixed (unique_resource_name): concurrent deployments collided on the
    # fixed cluster/service/role names ("already exists" on a second apply).
    service_name = unique_resource_name(ECS_SERVICE_NAME, context.job_id)
    if context.docker_images:
        containers = [
            {
                "name": img["name"],
                "image": img["image"],
                "port": img.get("port") or ECS_HEALTH_CHECK_PORT,
            }
            for img in context.docker_images
        ]
    else:
        containers = [
            {
                "name": service_name,
                "image": context.docker_image or "devguard-app:latest",
                "port": context.health_check_port or ECS_HEALTH_CHECK_PORT,
            }
        ]
    # The ALB always fronts the FIRST container (the public entrypoint); secondary
    # containers share the task's localhost and need no external listener.
    primary = containers[0]
    return {
        "region": context.region,
        "environment": context.environment,
        "cluster_name": unique_resource_name(ECS_CLUSTER_NAME, context.job_id),
        "service_name": service_name,
        "task_cpu": decision.sizing["task_cpu"],
        "task_memory": decision.sizing["task_memory"],
        "docker_image": context.docker_image or "devguard-app:latest",
        "containers": containers,
        "primary_container": primary,
        "health_check_port": primary["port"],
        "health_check_path": ECS_HEALTH_CHECK_PATH,
        "database": context.database,
        "execution_role_name": unique_resource_name(ECS_TASK_EXECUTION_ROLE_NAME, context.job_id),
        "log_group_name": f"/ecs/{service_name}",
    }


def _lambda_render_context(decision: DecisionResult, context: TerraformContext) -> dict[str, Any]:
    return {
        "region": context.region,
        "environment": context.environment,
        "function_name": LAMBDA_FUNCTION_NAME,
        "runtime": LAMBDA_RUNTIME,
        "handler": LAMBDA_HANDLER,
        "memory_mb": decision.sizing["memory_mb"],
        "timeout_seconds": LAMBDA_TIMEOUT_SECONDS,
        "source_code_path": context.source_code_path or f"/tmp/repo_{context.job_id}.zip",
    }


def _ec2_render_context(decision: DecisionResult, context: TerraformContext) -> dict[str, Any]:
    # Same job_id suffixing as ECS: IAM role/profile/SG derive from instance_name,
    # and a retried job died on EntityAlreadyExists leftovers. Confirmed in practice.
    instance_name = unique_resource_name(EC2_INSTANCE_NAME, context.job_id)
    # Real listen port from the repo Dockerfile EXPOSE (as ECS): instance_port must
    # match the app or SG/docker run/url all point at a dead port; None -> 8080.
    instance_port = context.health_check_port or EC2_INSTANCE_PORT
    return {
        "region": context.region,
        "environment": context.environment,
        "ami_id": EC2_AMI_ID,
        "instance_type": decision.sizing["instance_type"],
        "instance_count": EC2_INSTANCE_COUNT,
        "key_pair_name": EC2_KEY_PAIR_NAME,
        "instance_name": instance_name,
        "instance_port": instance_port,
        # Rendered into a locals block so the Gate-2 refiner can correct it to match
        # the app; output_builder's EC2 health check reads it back (same as ECS).
        "health_check_path": EC2_HEALTH_CHECK_PATH,
        "docker_image": context.docker_image or "devguard-app:latest",
        "ecr_registry_host": _ecr_registry_host(context),
    }


def _ecr_registry_host(context: TerraformContext) -> str:
    """Extract the ECR registry host from the fully-qualified image string.
    Fail-soft: a bare ``name:tag`` falls back to the account-qualified host when
    ``account_id`` is available, else the region endpoint with a placeholder."""
    image = context.docker_image or ""
    if "/" in image:
        return image.split("/", 1)[0]
    return f"{context.account_id or '000000000000'}.dkr.ecr.{context.region}.amazonaws.com"


def _s3_render_context(decision: DecisionResult, context: TerraformContext) -> dict[str, Any]:
    return {
        "region": context.region,
        "environment": context.environment,
        "bucket_name": f"{S3_BUCKET_PREFIX}-{context.job_id[:32].lower()}",
        "index_document": S3_INDEX_DOCUMENT,
        "error_document": S3_ERROR_DOCUMENT,
    }


_CONTEXT_BUILDERS = {
    "ecs": _ecs_render_context,
    "lambda": _lambda_render_context,
    "ec2": _ec2_render_context,
    "s3": _s3_render_context,
}


def generate_terraform(decision: DecisionResult, context: TerraformContext) -> TerraformFiles:
    """Render main.tf / variables.tf / outputs.tf for the decided architecture.
    ``decision`` names compute_type (template set) and supplies sizing; ``context``
    fills the rest (job id, region, docker image, ...). Returns TerraformFiles."""
    render_context = _CONTEXT_BUILDERS[decision.compute_type](decision, context)
    rendered = {
        filename: _ENV.get_template(f"{decision.compute_type}/{filename}.j2").render(**render_context)
        for filename in _TEMPLATE_FILENAMES
    }
    return TerraformFiles.model_validate(rendered)
