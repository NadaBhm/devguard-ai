"""Step 3 of the InfraCost pipeline: render Terraform files from templates.

Generation is entirely deterministic — no LLM writes any Terraform here.
Each compute type has its own set of three Jinja2 templates under
``templates/<compute_type>/`` (``main.tf.j2``, ``variables.tf.j2``,
``outputs.tf.j2``). This module only picks the right template set for
``decision.compute_type`` and fills in values already computed by module 2
(``decision_engine``) plus a small amount of non-decision context (job id,
region, docker image) — it never invents architecture choices itself.
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

# Conventions used to fill in template variables that module 2 does not
# decide (naming, ports, IAM role shape, ...). Single source of truth:
# core/constants.py — module 7 (output_builder) imports the same values
# so the JSON contract and the rendered Terraform can never drift.


class TerraformContext(BaseModel):
    """Inputs needed to render Terraform that module 2 doesn't provide.

    These are identity/environment details, not architecture choices —
    the architecture choice itself (compute_type, sizing) always comes
    from the ``DecisionResult`` passed alongside this context.
    """

    job_id: str
    region: str = "us-east-1"
    environment: str = "dev"
    docker_image: str | None = None
    docker_images: list[dict[str, Any]] | None = None
    """Multi-container mode: one entry per container to co-schedule in the
    task, each ``{"name", "image", "port", "context"}``. When set, the ECS
    template renders one ``container_definitions`` entry per image and routes
    the ALB to the first (primary); secondary containers are reachable over the
    task's shared localhost. ``None`` keeps legacy single-container behaviour
    via ``docker_image``.
    """
    source_code_path: str | None = None
    health_check_port: int | None = None
    """The real container port, extracted from the repo's own Dockerfile EXPOSE
    line by pipeline.py. None falls back to ECS_HEALTH_CHECK_PORT (8080) — a
    confirmed mismatch in practice once wired a FastAPI app on port 8000 into
    containerPort + ALB target group 8080, 502 on every health check -> full
    rollback."""
    account_id: str | None = None
    database: str | None = None
    """The database engine module 1 detected (e.g. "postgresql"), if any. Only
    used by the ECS template today, to declare (not create) the connection
    variables a deployer must fill in — see ``_ecs_render_context``.
    """


def _ecs_render_context(decision: DecisionResult, context: TerraformContext) -> dict[str, Any]:
    # Suffixed with job_id (see constants.unique_resource_name) so concurrent
    # deployments never collide on the same fixed cluster/service/role name
    # -- confirmed colliding in practice (ELBv2 Target Group / IAM Role /
    # CloudWatch Log Group "already exists" on a second `terraform apply`).
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
    # The ALB (target group, listener, SG ingress) always fronts the FIRST
    # container — the app's public entrypoint. Secondary containers share the
    # task's network namespace (localhost) and need no external listener.
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
    # Same job_id suffixing as ECS (constants.unique_resource_name): the IAM
    # role, instance profile and security group are all derived from
    # instance_name, so a fixed name collides across jobs -- and worse, a
    # retried job finds the partial-apply leftovers and dies on
    # EntityAlreadyExists. Confirmed colliding in practice on the EC2 path.
    instance_name = unique_resource_name(EC2_INSTANCE_NAME, context.job_id)
    # The instance's real listen port: pipeline.py extracts it from the repo's
    # own Dockerfile EXPOSE line and threads it through health_check_port
    # (same as ECS). The instance runs the same container image the pipeline
    # built, so instance_port must match what the app actually listens on, or
    # the SG / docker run / url output all point at a dead port. Fail-soft:
    # None falls back to the template default 8080.
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
        # Rendered into a locals block so the Gate-2 refiner can correct it to
        # match the app (e.g. "/api/health") and output_builder's EC2 health
        # check reads it back from the actual refined Terraform — same
        # contract as the ECS target group path.
        "health_check_path": EC2_HEALTH_CHECK_PATH,
        "docker_image": context.docker_image or "devguard-app:latest",
        "ecr_registry_host": _ecr_registry_host(context),
    }


def _ecr_registry_host(context: TerraformContext) -> str:
    """Extract the ECR registry host (e.g. ``123456.dkr.ecr.us-east-1.amazonaws.com``)
    from the fully-qualified image string. Fail-soft: if the image has no
    registry prefix (bare ``name:tag``), fall back to the account-qualified
    host when ``account_id`` is available on the context, else the region's
    ECR endpoint with a placeholder account.
    """
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

    Args:
        decision: Module 2's output — names which template set to use
            (``decision.compute_type``) and supplies the computed sizing.
        context: Non-decision values (job id, region, docker image, ...)
            needed to fill in the rest of the templates.

    Returns:
        A ``TerraformFiles`` with all three files rendered.
    """
    render_context = _CONTEXT_BUILDERS[decision.compute_type](decision, context)
    rendered = {
        filename: _ENV.get_template(f"{decision.compute_type}/{filename}.j2").render(**render_context)
        for filename in _TEMPLATE_FILENAMES
    }
    return TerraformFiles.model_validate(rendered)
