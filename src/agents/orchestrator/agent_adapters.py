"""
DevGuard AI - Agent Adapters
Anti-corruption layer between the orchestrator and the three real agents.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _flag(name: str) -> bool:
    if os.getenv("DEVGUARD_REAL_AGENTS", "").strip() in ("1", "true", "True"):
        return True
    return os.getenv(name, "").strip() in ("1", "true", "True")


def use_real_codesec() -> bool:
    return _flag("DEVGUARD_REAL_CODESEC")


def use_real_infracost() -> bool:
    return _flag("DEVGUARD_REAL_INFRACOST")


def use_real_deployops() -> bool:
    return _flag("DEVGUARD_REAL_DEPLOYOPS")


def report_mode() -> str:
    flags = (use_real_codesec(), use_real_infracost(), use_real_deployops())
    return "real" if all(flags) else ("mixed" if any(flags) else "mock")


def run_sync(coro: Any) -> Any:
    """Drive an async agent call from a synchronous LangGraph node."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


async def call_codesec(repo_url: str, job_id: str) -> dict[str, Any]:
    if not use_real_codesec():
        from .nodes import build_mock_codesec_result
        logger.info("[%s] CodeSec: MOCK mode (set DEVGUARD_REAL_CODESEC=1 for real)", job_id)
        return build_mock_codesec_result(job_id, repo_url)

    from src.agents.codesec.agent import CodeSecAgent

    logger.info("[%s] CodeSec: REAL agent on %s", job_id, repo_url)
    agent = CodeSecAgent()
    result = await agent.analyze(repo_url, job_id)

    payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)

    metadata = payload.get("repo_metadata") or {}
    if metadata.get("commit_sha") is None:
        metadata["commit_sha"] = "unknown"
        payload["repo_metadata"] = metadata
        logger.warning("[%s] commit_sha was None; coerced to 'unknown' for InfraCost", job_id)

    return payload


def _infracost_package_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "agentInfraCost"


def _run_infracost_pipeline(codesec_result: dict[str, Any]) -> Any:
    """
    Run Karim's pipeline with sys.path scoped to agentInfraCost only.
    Prevents PEP 420 namespace collisions with other 'core' packages.
    """
    import sys as _sys
    from pathlib import Path as _Path

    package_dir = str(_infracost_package_dir())
    project_paths = {
        ".", "src",
        str(_Path(".").resolve()),
        str(_Path("src").resolve()),
    }
    original_path = list(_sys.path)
    _sys.path[:] = [package_dir] + [p for p in original_path if p not in project_paths]
    try:
        from core.orchestrator_adapter import to_orchestrator_result  # type: ignore[reportMissingImports]
        from core.pipeline import run_pipeline_with_context  # type: ignore[reportMissingImports]

        ctx = run_pipeline_with_context(codesec_result)
        result = dict(to_orchestrator_result(ctx.output, ctx.decision, ctx.finops))
        result["warnings"] = ctx.warnings
        result["_deploy_inputs"] = {
            "compute_type": ctx.output.compute_type,
            "artifacts": ctx.output.artifacts.model_dump(mode="json", by_alias=True),
            "aws_config": ctx.output.aws_config.model_dump(mode="json", by_alias=True),
            "deployment_config": ctx.output.deployment_config.model_dump(mode="json", by_alias=True),
        }
        return result
    finally:
        _sys.path[:] = original_path


async def call_infracost(
    codesec_result: dict[str, Any],
    job_id: str,
    *,
    feedback: str | None = None,
    previous_result: Any | None = None,
    iteration_number: int | None = None,
) -> dict[str, Any]:
    if not use_real_infracost():
        from .nodes import build_mock_infracost_result
        logger.info("[%s] InfraCost: MOCK mode (set DEVGUARD_REAL_INFRACOST=1 for real)", job_id)
        return _mock_infracost_with_feedback(job_id, feedback, previous_result)

    logger.info("[%s] InfraCost: REAL agent%s", job_id, " (regenerating from feedback)" if feedback else "")
    raw_input = dict(codesec_result)

    try:
        from src.lib.aws.client import AWSClient
        raw_input["account_id"] = AWSClient().get_account_id()
    except Exception:
        logger.warning("[%s] Could not resolve AWS account ID for ECR image URI", job_id)

    repo_path: str | None = None
    if feedback:
        raw_input["user_feedback"] = feedback
        if iteration_number is not None:
            raw_input["regen_iteration"] = iteration_number

    try:
        from src.lib.repo import clone_repo
        repo_path = tempfile.mkdtemp(prefix=f"devguard-repo-{job_id[:8]}-")
        # 50k: monorepos (next.js = 31k files) are legit; the digestor
        # self-caps reading, so a bigger tree is only a disk/time cost.
        # timeout=300: 60s expired for mid-size repos while docker buildx
        # saturated the link (same fix as codesec's _clone_repo).
        clone_repo(codesec_result.get("repo_url", ""), repo_path, max_files=50_000, timeout=300)
        raw_input["repo_path"] = repo_path
    except Exception as exc:
        logger.warning("[%s] Could not re-clone repo for InfraCost: %s", job_id, exc)
        if repo_path:
            shutil.rmtree(repo_path, ignore_errors=True)
            repo_path = None

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, _run_infracost_pipeline, raw_input
        )
    finally:
        if repo_path:
            shutil.rmtree(repo_path, ignore_errors=True)
    return normalize_infracost_result(result)


def _mock_infracost_with_feedback(
    job_id: str,
    feedback: str | None,
    previous_result: Any | None,
) -> dict[str, Any]:
    from .nodes import build_mock_infracost_result

    result = dict(previous_result or build_mock_infracost_result())
    if not feedback:
        return result

    cost = dict(result.get("cost_estimate") or {})
    baseline = float(cost.get("monthly_cost_usd", 145.32))

    lowered = any(k in feedback.lower() for k in ("cheap", "cheaper", "less", "lower", "reduce", "minimize"))
    raised = any(k in feedback.lower() for k in ("more", "bigger", "scale", "increase", "high", "larger"))
    if lowered:
        baseline = round(baseline * 0.85, 2)
    elif raised:
        baseline = round(baseline * 1.15, 2)

    cost = dict(cost)
    cost["monthly_cost_usd"] = baseline
    result["cost_estimate"] = cost
    result["justification"] = (
        f"Regenerated ({len(result.get('breakdown_rounds', [])) + 1}) after feedback: "
        f"{feedback}. {result.get('justification', '')}"
    ).strip()
    rounds = list(result.get("breakdown_rounds") or [])
    rounds.append({"prompt": feedback, "monthly_cost_usd": baseline})
    result["breakdown_rounds"] = rounds

    if any(k in feedback.lower() for k in ("lambda", "serverless", "function")):
        result["architecture_recommendation"] = "lambda"
    elif any(k in feedback.lower() for k in ("ec2", "vm", "instance")):
        result["architecture_recommendation"] = "ec2"
    elif "ecs" in feedback.lower() or "fargate" in feedback.lower():
        result["architecture_recommendation"] = "ecs_fargate"

    return result


def _money_amount(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("amount")
    return value


def normalize_infracost_result(result: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize InfraCost output to the orchestrator's expected shape.
    Idempotent: safe to call even if upstream fixes the mapping.
    """
    result = dict(result)
    cost = dict(result.get("cost_estimate") or {})
    if "monthly_cost_usd" not in cost and "amount" in cost:
        cost = {
            "monthly_cost_usd": cost.get("amount", 0),
            "currency": cost.get("currency", "USD"),
            "breakdown": cost.get("breakdown") or [],
            "range_min": cost.get("range_min"),
            "range_max": cost.get("range_max"),
        }
        result["cost_estimate"] = cost

    scenarios = []
    for item in result.get("load_scenarios") or []:
        item = dict(item)
        if "estimated_monthly_cost_usd" not in item:
            amount = _money_amount(item.get("estimated_monthly_cost"))
            if amount is not None:
                item["estimated_monthly_cost_usd"] = amount
        if not item.get("scaling_assumptions") and isinstance(item.get("sizing"), dict):
            item["scaling_assumptions"] = ", ".join(
                f"{key}={value}" for key, value in item["sizing"].items()
            )
        scenarios.append(item)
    if scenarios:
        result["load_scenarios"] = scenarios

    opts = []
    for item in result.get("optimizations") or []:
        item = dict(item)
        if (
            "projected_savings_usd" not in item
            and item.get("projected_monthly_savings") is not None
        ):
            item["projected_savings_usd"] = item["projected_monthly_savings"]
        if not item.get("description") and item.get("reason"):
            item["description"] = item["reason"]
        if not item.get("strategy") and item.get("name"):
            item["strategy"] = item["name"]
        opts.append(item)
    if opts:
        result["optimizations"] = opts

    regions = []
    for item in result.get("region_comparison") or []:
        item = dict(item)
        if "monthly_cost_usd" not in item:
            amount = _money_amount(item.get("estimated_monthly_cost"))
            if amount is not None:
                item["monthly_cost_usd"] = amount
        regions.append(item)
    if regions:
        result["region_comparison"] = regions

    return result


_DEFAULT_PLATFORM = "linux/amd64"
_DEFAULT_CONTEXT = "."


def _raise_missing_image_dockerfile(name: str | None, job_id: str) -> str:
    raise ValueError(
        f"artifacts.docker_images[{name or '?'}] has no dockerfile content — "
        f"InfraCost produced an image entry without its Dockerfile for job "
        f"{job_id}. Refusing to send DeployOps an image build with no Dockerfile."
    )


def translate_infracost_to_deploy_payload(
    job_id: str,
    deploy_inputs: dict[str, Any],
    *,
    approved_by: str,
    repo_url: str | None = None,
    is_update: bool = False,
    existing_deployment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build a DeployOps-compatible payload from InfraCost's raw output.
    Fails loudly on missing Dockerfile or unsupported compute type.

    is_update: True for an "update deployment" run -- redeploy onto the
        project's existing ECS service instead of provisioning fresh
        infrastructure. When True, InfraCost's freshly-sized aws_config is
        ignored for cluster/service targeting in favor of
        `existing_deployment` (the live service to update).
    existing_deployment: {region, ecs_cluster, service_name} of the
        currently live deployment, resolved by the backend. Required when
        is_update=True.
    """
    compute_type = deploy_inputs.get("compute_type")
    artifacts = deploy_inputs.get("artifacts") or {}
    aws_config = deploy_inputs.get("aws_config") or {}
    deployment_config = deploy_inputs.get("deployment_config") or {}

    if compute_type not in ("ecs", "ec2", "s3"):
        if compute_type == "lambda" and os.getenv("DEVGUARD_FORCE_COMPUTE_ECS", "1").lower() == "1":
            logger.warning(
                "[%s] InfraCost recommended lambda, but DeployOps has no lambda path yet — falling back to ec2",
                job_id,
            )
            compute_type = "ec2"
            deploy_inputs["compute_type"] = "ec2"
            if not artifacts.get("docker_images"):
                artifacts["docker_images"] = [
                    {
                        "name": "devguard-app",
                        "tag": "latest",
                        "dockerfile": (
                            "FROM python:3.12-slim\n"
                            "WORKDIR /app\n"
                            "COPY . .\n"
                            "RUN pip install fastapi uvicorn\n"
                            'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]\n'
                        ),
                        "context": ".",
                        "platform": "linux/amd64",
                    }
                ]
            _sys = None
            _orig_path = None
            try:
                import sys as _sys
                import pathlib as _pathlib

                _orig_path = _sys.path[:]
                _agent_cost = _pathlib.Path(__file__).resolve().parents[1] / "agentInfraCost"
                if str(_agent_cost) not in _sys.path:
                    _sys.path.insert(0, str(_agent_cost))
                from core.decision_engine import DecisionResult as _DecisionResult  # type: ignore[import]
                from core.terraform_generator import TerraformContext as _TFContext, generate_terraform as _gen  # type: ignore[import]

                _region = (aws_config.get("region") if isinstance(aws_config, dict) else None) or "us-east-1"
                _first = artifacts["docker_images"][0]
                _image = f"{_first.get('name','devguard-app')}:{_first.get('tag','latest')}"
                _decision = _DecisionResult(compute_type="ec2", sizing={"instance_type": "t3.micro"}, score_breakdown={})
                _ctx = _TFContext(job_id=job_id, region=_region, docker_image=_image)
                _tf = _gen(_decision, _ctx)
                artifacts["terraform"] = {
                    "files": _tf.model_dump(by_alias=True),
                    "variables": {"region": _region, "environment": "dev"},
                }
            except Exception as _exc:  # noqa: BLE001
                logger.warning("[%s] ec2 terraform regeneration failed, keeping original: %s", job_id, _exc)
            finally:
                if _sys is not None and _orig_path is not None:
                    try:
                        _sys.path[:] = _orig_path
                    except Exception:
                        pass
                deploy_inputs["artifacts"] = artifacts
        else:
            raise ValueError(
                f"DeployOps currently only supports ecs, ec2, and s3 deployments, "
                f"but InfraCost recommended compute_type={compute_type!r}."
            )

    plural_images = artifacts.get("docker_images")
    dockerfile_content = artifacts.get("dockerfile")
    if compute_type != "s3" and not plural_images and not dockerfile_content:
        raise ValueError(
            "artifacts.dockerfile is empty - InfraCost produced no Dockerfile "
            "content. Refusing to send DeployOps an image build with no Dockerfile."
        )

    docker_images: list[dict[str, Any]] = []
    if compute_type != "s3" and plural_images:
        docker_images = [
            {
                "name": img.get("name") or f"devguard-{job_id[:8]}",
                "dockerfile": img.get("dockerfile")
                or _raise_missing_image_dockerfile(img.get("name"), job_id),
                "context": img.get("context") or _DEFAULT_CONTEXT,
                "tag": img.get("tag") or "latest",
                "platform": img.get("platform") or _DEFAULT_PLATFORM,
            }
            for img in plural_images
        ]
    elif compute_type != "s3":
        docker_image = artifacts.get("docker_image") or {}
        docker_images = [{
            "name": docker_image.get("name") or f"devguard-{job_id[:8]}",
            "dockerfile": dockerfile_content,
            "context": artifacts.get("source_code") or _DEFAULT_CONTEXT,
            "tag": docker_image.get("tag") or "latest",
            "platform": _DEFAULT_PLATFORM,
        }]

    terraform = artifacts.get("terraform") or {}
    tf_files = dict(terraform.get("files") or {})
    if not tf_files:
        raise ValueError("InfraCost produced no Terraform files; nothing to deploy.")

    dep_block = deployment_config.get(compute_type) or {}
    strategy = dep_block.get("strategy", "rolling")
    if strategy == "blue-green":
        strategy = "blue_green"

    # When DeployOps forces a different compute than InfraCost decided
    # (lambda -> ec2 fallback), deployment_config[compute] is null and the
    # defaults below don't match the rendered Terraform. Parse the rendered
    # locals first — they are what the deploy actually serves.
    rendered_main = (terraform.get("files") or {}).get("main.tf", "")
    m_port = re.search(r"health_check_port\s*=\s*(\d+)", rendered_main)
    m_path = re.search(r'health_check_path\s*=\s*"([^"]+)"', rendered_main)
    default_port = int(m_port.group(1)) if m_port else 80
    default_path = m_path.group(1) if m_path else "/health"

    ecs_block = aws_config.get("ecs") or {}
    ec2_block = aws_config.get("ec2") or {}
    s3_block = aws_config.get("s3") or {}
    if compute_type == "ecs" and is_update:
        # Update runs redeploy onto the project's already-live ECS service --
        # InfraCost's freshly-sized ecs_block names a NEW service and must be
        # ignored here, or this would target (or fail to find) the wrong one.
        if not existing_deployment or not existing_deployment.get("ecs_cluster") or not existing_deployment.get("service_name"):
            raise ValueError(
                "is_update=True but existing_deployment is missing "
                "ecs_cluster/service_name; refusing to guess which live "
                "service to redeploy onto."
            )
        flat_aws = {
            "region": existing_deployment.get("region") or "us-east-1",
            "ecs_cluster": existing_deployment["ecs_cluster"],
            "service_name": existing_deployment["service_name"],
        }
    elif compute_type == "ecs":
        if not ecs_block.get("cluster") or not ecs_block.get("service_name"):
            raise ValueError(
                "InfraCost's aws_config.ecs is missing cluster/service_name; "
                "DeployOps.rollback() would raise KeyError on these later."
            )
        flat_aws = {
            "region": aws_config.get("region", "us-east-1"),
            "ecs_cluster": ecs_block.get("cluster"),
            "service_name": ecs_block.get("service_name"),
        }
    elif compute_type == "ec2":
        flat_aws = {
            "region": aws_config.get("region", "us-east-1"),
            "ecs_cluster": None,
            "service_name": None,
        }
    else:  # s3
        flat_aws = {
            "region": aws_config.get("region", "us-east-1"),
            "ecs_cluster": None,
            "service_name": None,
            "bucket_name": s3_block.get("bucket_name"),
        }

    payload = {
        "job_id": job_id,
        "artifacts": {
            "terraform": {
                "files": tf_files,
                "variables": dict(terraform.get("variables") or {}),
            },
            "docker_images": docker_images,
        },
        "aws_config": flat_aws,
        "deployment_config": {
            "strategy": strategy,
            "health_check_path": dep_block.get("health_check_path", default_path),
            "health_check_port": dep_block.get("health_check_port", default_port),
            "min_healthy_percent": dep_block.get("min_healthy_percent", 50),
            "max_percent": dep_block.get("max_percent", 200),
            "timeout_minutes": dep_block.get("timeout_minutes", 15),
            "auto_rollback": True,
        },
        "approval": {
            "deploy_approved": True,
            "approved_by": approved_by,
            "approved_at": datetime.now(timezone.utc).isoformat(),
        },
        "metadata": (
            ({"repo_url": repo_url} if repo_url else {})
            | ({"ecs_update_only": True} if is_update else {})
        ),
        "compute_type": compute_type,
    }

    return payload


async def call_deployops(deploy_payload: dict[str, Any], job_id: str) -> dict[str, Any]:
    if not use_real_deployops():
        from .nodes import build_mock_deployops_result
        logger.info("[%s] DeployOps: MOCK mode (set DEVGUARD_REAL_DEPLOYOPS=1 for real)", job_id)
        return build_mock_deployops_result(job_id)

    from src.agents.deployops.agent import DeployOpsAgent

    logger.info("[%s] DeployOps: REAL agent", job_id)
    agent = DeployOpsAgent()
    raw = await agent.deploy(deploy_payload)

    return translate_deployops_result(raw, job_id)


def translate_deployops_result(raw: dict[str, Any], job_id: str) -> dict[str, Any]:
    succeeded = raw.get("status") == "success"
    outputs = raw.get("resources") or {}

    def _tf_out(key: str) -> str:
        entry = outputs.get(key)
        if isinstance(entry, dict):
            return entry.get("value") or ""
        return entry or ""

    hc = raw.get("health_check") or {}
    return {
        "job_id": raw.get("job_id") or job_id,
        "deployment_status": "success" if succeeded else "failed",
        "deployed_url": raw.get("deployed_url"),
        "health_check": {
            **({
                "passed": bool(raw.get("health_check", {}).get("passed")),
                "response_time_ms": raw.get("health_check", {}).get("response_time_ms", 0),
                "status_code": raw.get("health_check", {}).get("status_code", (200 if succeeded else 0)),
                "checked_at": raw.get("health_check", {}).get("checked_at", datetime.now(timezone.utc).isoformat()),
            } if isinstance(raw.get("health_check"), dict) else {
                "passed": succeeded,
                "response_time_ms": 0,
                "status_code": 200 if succeeded else 0,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            })
        },
        "rollback_triggered": raw.get("error") == "health check failed",
        "rollback_reason": raw.get("error") if not succeeded else None,
        "error": raw.get("error"),
        "terraform_outputs": {
            "ecs_cluster_name": _tf_out("ecs_cluster_name"),
            "service_name": _tf_out("service_name"),
            "alb_dns": _tf_out("alb_dns") or _tf_out("alb_dns_name"),
        },
    }