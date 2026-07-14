"""Module 3: renders main.tf / variables.tf / outputs.tf from Jinja2 templates.

Deterministic by construction: Terraform is always produced from the fixed
templates under templates/{ecs,lambda,ec2}/, never generated as free text by
an LLM. Every value interpolated into a quoted HCL string is passed through
the `hcl_string` filter, which escapes backslashes and double quotes, so a
value containing either can never break the generated file's syntax.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from ..models.internal_models import DecisionResult, TerraformGenerationResult

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

_TEMPLATE_FILES: tuple[str, ...] = ("main.tf", "variables.tf", "outputs.tf")


def _hcl_string(value: Any) -> str:
    """Escapes a value for safe interpolation inside a double-quoted HCL string."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _make_environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    env.filters["hcl_string"] = _hcl_string
    return env


_JINJA_ENV = _make_environment()


def _render_all(compute_type: str, context: dict[str, Any]) -> dict[str, str]:
    files: dict[str, str] = {}
    for filename in _TEMPLATE_FILES:
        template = _JINJA_ENV.get_template(f"{compute_type}/{filename}.j2")
        files[filename] = template.render(**context)
    return files


def _generate_ecs(
    decision: DecisionResult, *, region: str, environment: str, docker_image_uri: Optional[str]
) -> TerraformGenerationResult:
    if decision.ecs is None:
        raise ValueError("DecisionResult.compute_type='ecs' but decision.ecs is None")
    if not docker_image_uri:
        raise ValueError("docker_image_uri is required to generate Terraform for compute_type='ecs'")

    context = {
        "cluster": decision.ecs.cluster,
        "service_name": decision.ecs.service_name,
        "task_cpu": decision.ecs.task_cpu,
        "task_memory": decision.ecs.task_memory,
        "health_check_port": decision.ecs.health_check_port,
        "min_healthy_percent": decision.ecs.min_healthy_percent,
        "max_percent": decision.ecs.max_percent,
        "docker_image": docker_image_uri,
        "region": region,
        "environment": environment,
    }
    files = _render_all("ecs", context)
    variables = {"region": region, "environment": environment}
    return TerraformGenerationResult(files=files, variables=variables)


def _generate_lambda(
    decision: DecisionResult, *, region: str, environment: str
) -> TerraformGenerationResult:
    if decision.lambda_ is None:
        raise ValueError("DecisionResult.compute_type='lambda' but decision.lambda_ is None")

    context = {
        "function_name": decision.lambda_.function_name,
        "runtime": decision.lambda_.runtime,
        "memory_mb": decision.lambda_.memory_mb,
        "timeout_seconds": decision.lambda_.timeout_seconds,
        "handler": decision.lambda_.handler,
        "reserved_concurrency": decision.lambda_.reserved_concurrency,
        "region": region,
        "environment": environment,
    }
    files = _render_all("lambda", context)
    variables = {"region": region, "environment": environment}
    return TerraformGenerationResult(files=files, variables=variables)


def _generate_ec2(
    decision: DecisionResult, *, region: str, environment: str, docker_image_uri: Optional[str]
) -> TerraformGenerationResult:
    if decision.ec2 is None:
        raise ValueError("DecisionResult.compute_type='ec2' but decision.ec2 is None")

    context = {
        "instance_type": decision.ec2.instance_type,
        "ami_id": decision.ec2.ami_id,
        "instance_count": decision.ec2.instance_count,
        "key_pair_name": decision.ec2.key_pair_name,
        "health_check_port": decision.ec2.health_check_port,
        "docker_image": docker_image_uri,
        "region": region,
        "environment": environment,
    }
    files = _render_all("ec2", context)
    variables = {"region": region, "environment": environment}
    return TerraformGenerationResult(files=files, variables=variables)


def generate_terraform(
    decision: DecisionResult,
    *,
    region: str,
    environment: str = "dev",
    docker_image_uri: Optional[str] = None,
) -> TerraformGenerationResult:
    """Renders main.tf / variables.tf / outputs.tf for `decision.compute_type`.

    :param docker_image_uri: e.g. "repo-name:a1b2c3d4e5f6". Required for
        "ecs" (the task definition always needs an image). Optional for
        "ec2" (adds a Docker-run user_data block when given, otherwise the
        instance is provisioned bare). Ignored for "lambda", which always
        deploys from a zipped package via the deployment_package_path
        variable.
    :raises ValueError: if decision.compute_type has no matching sizing
        block, or if docker_image_uri is missing for "ecs".
    """
    if decision.compute_type == "ecs":
        result = _generate_ecs(
            decision, region=region, environment=environment, docker_image_uri=docker_image_uri
        )
    elif decision.compute_type == "lambda":
        result = _generate_lambda(decision, region=region, environment=environment)
    elif decision.compute_type == "ec2":
        result = _generate_ec2(
            decision, region=region, environment=environment, docker_image_uri=docker_image_uri
        )
    else:
        raise ValueError(f"Unknown compute_type: {decision.compute_type!r}")

    logger.info(
        "terraform_generator rendered %d files for compute_type='%s'",
        len(result.files),
        decision.compute_type,
    )
    return result
