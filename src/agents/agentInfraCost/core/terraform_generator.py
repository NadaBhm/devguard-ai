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

# --------------------------------------------------------------------------
# Conventions used to fill in template variables that module 2 does not
# decide (naming, ports, IAM role shape, ...). These mirror the same
# defaults used by main.py's mock response builder; module 7
# (output_builder) will be the single source of truth for them once built.
# --------------------------------------------------------------------------

_ECS_CLUSTER_NAME: Final[str] = "devguard-cluster"
_ECS_SERVICE_NAME: Final[str] = "app-service"
_ECS_HEALTH_CHECK_PORT: Final[int] = 8080

_LAMBDA_FUNCTION_NAME: Final[str] = "app-handler"
_LAMBDA_HANDLER: Final[str] = "handler.main"
_LAMBDA_RUNTIME: Final[str] = "python3.12"
_LAMBDA_TIMEOUT_SECONDS: Final[int] = 30

_EC2_AMI_ID: Final[str] = "ami-0000000000000000"
_EC2_KEY_PAIR_NAME: Final[str] = "devguard-key"
_EC2_INSTANCE_COUNT: Final[int] = 1
_EC2_INSTANCE_NAME: Final[str] = "devguard-app"


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
    source_code_path: str | None = None


def _ecs_render_context(decision: DecisionResult, context: TerraformContext) -> dict[str, Any]:
    return {
        "region": context.region,
        "environment": context.environment,
        "cluster_name": _ECS_CLUSTER_NAME,
        "service_name": _ECS_SERVICE_NAME,
        "task_cpu": decision.sizing["task_cpu"],
        "task_memory": decision.sizing["task_memory"],
        "docker_image": context.docker_image or "devguard-app:latest",
        "health_check_port": _ECS_HEALTH_CHECK_PORT,
    }


def _lambda_render_context(decision: DecisionResult, context: TerraformContext) -> dict[str, Any]:
    return {
        "region": context.region,
        "environment": context.environment,
        "function_name": _LAMBDA_FUNCTION_NAME,
        "runtime": _LAMBDA_RUNTIME,
        "handler": _LAMBDA_HANDLER,
        "memory_mb": decision.sizing["memory_mb"],
        "timeout_seconds": _LAMBDA_TIMEOUT_SECONDS,
        "source_code_path": context.source_code_path or f"/tmp/repo_{context.job_id}.zip",
    }


def _ec2_render_context(decision: DecisionResult, context: TerraformContext) -> dict[str, Any]:
    return {
        "region": context.region,
        "environment": context.environment,
        "ami_id": _EC2_AMI_ID,
        "instance_type": decision.sizing["instance_type"],
        "instance_count": _EC2_INSTANCE_COUNT,
        "key_pair_name": _EC2_KEY_PAIR_NAME,
        "instance_name": _EC2_INSTANCE_NAME,
    }


_CONTEXT_BUILDERS = {
    "ecs": _ecs_render_context,
    "lambda": _lambda_render_context,
    "ec2": _ec2_render_context,
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
