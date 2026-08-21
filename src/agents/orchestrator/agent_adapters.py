"""
DevGuard AI - Agent Adapters (anti-corruption layer)
=====================================================
Single point of contact between the orchestrator and the three real agents.

WHY THIS FILE EXISTS
--------------------
The three agents were built independently and do NOT share a calling
convention or a data contract:

  | Agent                | Call style                                    |
  |----------------------|-----------------------------------------------|
  | CodeSec (Nada)       | await CodeSecAgent().analyze(url, job_id)     |
  |                      | -> Pydantic CodeSecResult (async class method)|
  | InfraCost (Karim)    | run_pipeline_with_context(raw)                |
  |                      | -> PipelineContext (SYNC plain function)      |
  | DeployOps (Oussema)  | await DeployOpsAgent().deploy(payload)        |
  |                      | -> dict (async class method)                  |

InfraCost's OUTPUT shape and DeployOps's INPUT shape do not line up
(see translate_infracost_to_deploy_payload). Rather than smearing those
mismatches across nodes.py, everything is isolated here: when the agents
change their contracts, THIS is the only file that needs to change.

MOCK MODE
---------
Until all feature branches are merged, real agent modules don't exist.
Every call_* function falls back to Sprint 1 mocks unless switched on:

    DEVGUARD_REAL_CODESEC=1
    DEVGUARD_REAL_INFRACOST=1
    DEVGUARD_REAL_DEPLOYOPS=1
    DEVGUARD_REAL_AGENTS=1      # turns on all three at once

Imports of the real agents are LAZY (inside functions), so this module
imports cleanly even when no agent packages are present.

CDC Reference: T-2.17 (wire orchestrator to real CodeSec/DeployOps),
               T-3.16 (integrate InfraCost into orchestrator workflow)

Owner: Hbib (Subgroup 2 - Execution & Control)
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# =============================================================================
# FEATURE FLAGS
# =============================================================================

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
    """``real`` / ``mixed`` / ``mock`` — whether live agents actually ran.

    Used for the honesty badge on the report and the UI: a run that mixes
    fabricated and real agent output must say so rather than look real.
    """
    flags = (use_real_codesec(), use_real_infracost(), use_real_deployops())
    return "real" if all(flags) else ("mixed" if any(flags) else "mock")


# =============================================================================
# ASYNC -> SYNC BRIDGE
# =============================================================================

def run_sync(coro: "Any") -> Any:
    """
    Drive an async agent call from a synchronous LangGraph node.

    The graph must stay synchronous: `graph.invoke()` cannot run async nodes
    (LangGraph raises InvalidUpdateError: "Expected dict, got coroutine"), and
    making it async would force run_workflow()/resume_workflow() async too,
    breaking the backend's sync FastAPI endpoints (src/backend/api/jobs.py).
    If no event loop is running, asyncio.run() suffices; otherwise the
    coroutine runs on a dedicated thread with its own loop (asyncio.run()
    would raise).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()

# =============================================================================
# 1. CODESEC (Nada)
# =============================================================================

async def call_codesec(repo_url: str, job_id: str) -> dict[str, Any]:
    """
    Run CodeSec on a public GitHub repo and return its result as a plain dict.

    Real contract (src/agents/codesec/agent.py):
        agent = CodeSecAgent()
        result: CodeSecResult = await agent.analyze(repo_url, job_id)

    CodeSecResult is a Pydantic model; the orchestrator state stores
    codesec_result as a flexible dict ("accept everything from Nada"),
    so we model_dump() it here and pass it through untouched.

    NOTE: CodeSec also triggers the RAG ingestion internally (ingest_repo is
    called right after the clone, before the clone is deleted), so the chat
    context for this job is ready as soon as this call returns. Nothing to
    do on the orchestrator side.
    """
    if not use_real_codesec():
        from .nodes import build_mock_codesec_result
        logger.info("[%s] CodeSec: MOCK mode (set DEVGUARD_REAL_CODESEC=1 for real)", job_id)
        return build_mock_codesec_result(job_id, repo_url)

    from src.agents.codesec.agent import CodeSecAgent  # lazy: branch may not have it

    logger.info("[%s] CodeSec: REAL agent on %s", job_id, repo_url)
    agent = CodeSecAgent()
    result = await agent.analyze(repo_url, job_id)

    payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else dict(result)

    # InfraCost's RepoAnalysisInput declares commit_sha as a REQUIRED str,
    # but CodeSec leaves it None when `git rev-parse` fails. Coerce here so a
    # transient git hiccup doesn't blow up the next agent's validation.
    # TODO: remove once Nada defaults it, or Karim relaxes it to Optional[str].
    metadata = payload.get("repo_metadata") or {}
    if metadata.get("commit_sha") is None:
        metadata["commit_sha"] = "unknown"
        payload["repo_metadata"] = metadata
        logger.warning("[%s] commit_sha was None; coerced to 'unknown' for InfraCost", job_id)

    return payload


# =============================================================================
# 2. INFRACOST (Karim)
# =============================================================================
# CDC Reference: T-3.16 (integrate InfraCost into orchestrator workflow)
#
# InfraCost's API has churned twice on master: this targets
# run_pipeline_with_context() + core.orchestrator_adapter.to_orchestrator_result().
# An earlier run_pipeline() version never produced Dockerfile CONTENT, only a
# source_code path into CodeSec's already-deleted clone. This build's
# output_builder.py generates the Dockerfile itself (f"FROM {base_image}\nCOPY
# . /app\n"), so if the API changes again, watch artifacts.dockerfile - the
# translator below fails loudly if it regresses to empty/path-only.

def _infracost_package_dir() -> Path:
    """Absolute path to src/agents/agentInfraCost, regardless of cwd."""
    return Path(__file__).resolve().parents[1] / "agentInfraCost"


def _run_infracost_pipeline(codesec_result: dict[str, Any]) -> Any:
    """
    Import and run Karim's pipeline using ITS OWN import convention.

    agentInfraCost/core/*.py import each other with flat names (`from
    core.decision_engine import ...`), which only resolve if agentInfraCost/
    itself - not src/ - is on sys.path. Merely inserting it into the path
    failed on Windows ("No module named 'core.orchestrator_adapter'"): with
    '.', 'src' and agentInfraCost/ all on sys.path, PEP 420 namespace packages
    could assemble a bare `core` from more than one entry, and which portion
    won depended on path order.

    So for this call ONLY, sys.path is replaced with agentInfraCost/ plus
    everything outside this project's source tree (stdlib and site-packages
    stay reachable; '.', 'src' and their absolute-path equivalents do not).
    No other entry can contribute a competing `core` or `models` portion, on
    any platform. Restored afterwards; runs off the main thread (via
    run_in_executor), so mutating sys.path here can't race the app.
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
        # Stash the raw artifacts DeployOps needs (Dockerfile content,
        # terraform files, aws_config/deployment_config) - the orchestrator's
        # InfraCostResult TypedDict has no field for these, so without
        # keeping them here they'd be lost between this node and DeployOps's.
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
    previous_result: dict[str, Any] | None = None,
    iteration_number: int | None = None,
) -> dict[str, Any]:
    """
    Run InfraCost on CodeSec's output and return the orchestrator-shaped result.

    feedback: optional free-form user prompt from Gate 2 ("regenerate with
        changes"). When present, the InfraCost pipeline regenerates its
        artifacts (architecture + Terraform) honoring it.
    previous_result: the last InfraCost result, so mock mode can produce a
        visibly different output each regeneration round.

    Real contract (src/agents/agentInfraCost/core/):
        ctx = run_pipeline_with_context(codesec_result)   # SYNC
        result = to_orchestrator_result(ctx.output, ctx.decision, ctx.finops)

    run_pipeline_with_context is synchronous and does real work (LLM calls,
    AWS pricing lookups), so it goes through run_in_executor rather than
    blocking the event loop, same reasoning as CodeSec's async agent call.
    """
    if not use_real_infracost():
        from .nodes import build_mock_infracost_result
        logger.info("[%s] InfraCost: MOCK mode (set DEVGUARD_REAL_INFRACOST=1 for real)", job_id)
        return _mock_infracost_with_feedback(job_id, feedback, previous_result)

    logger.info("[%s] InfraCost: REAL agent%s", job_id, " (regenerating from feedback)" if feedback else "")
    raw_input = dict(codesec_result)
    # InfraCost's pipeline runs with sys.path swapped to its own package dir
    # (see _run_infracost_pipeline), so it cannot import src.lib.aws itself.
    # Resolve the AWS account ID here, where src/ is on the normal path, and
    # thread it through raw_input so the generated Terraform can build a
    # fully-qualified ECR image URI instead of a bare "name:tag" (which
    # Docker/ECS would otherwise resolve against Docker Hub and fail to pull).
    try:
        from src.lib.aws.client import AWSClient  # lazy: mirrors other real-agent imports
        raw_input["account_id"] = AWSClient().get_account_id()
    except Exception:
        logger.warning("[%s] Could not resolve AWS account ID for ECR image URI; "
                        "Terraform will be generated with a bare image name.", job_id)

    repo_path: str | None = None
    # InfraCost's RepoAnalysisInput carries this optional field; threading
    # it through the raw dict lets the pipeline's LLM advisors and the
    # Terraform refiner act on the user's request.
    if feedback:
        raw_input["user_feedback"] = feedback
        if iteration_number is not None:
            # 1-based regeneration round; the pipeline uses it to gate the
            # hidden first-regen auto-fix (correct container port / health
            # check to match the repo) to exactly one run.
            raw_input["regen_iteration"] = iteration_number
    # CodeSec deletes its clone the moment analysis finishes, so we re-clone
    # the repo and let the pipeline digest the whole codebase for the
    # OpenRouter LLM (see core.repo_ingestor) on EVERY pass -- first try
    # included, not just Gate-2 regeneration. That way the LLM generates the
    # artifacts (Dockerfile, port, health path) from the real code instead of
    # shipping the deterministic stub and waiting for a regen to fix it.
    # Fail-soft: if the clone fails, the pipeline proceeds exactly as before,
    # without repo context.
    try:
        from src.lib.repo import clone_repo  # lazy: shared clone helper
        repo_path = tempfile.mkdtemp(prefix=f"devguard-repo-{job_id[:8]}-")
        clone_repo(codesec_result.get("repo_url", ""), repo_path)
        raw_input["repo_path"] = repo_path
    except Exception as exc:
        logger.warning(
            "[%s] Could not re-clone repo for InfraCost; "
            "generating without repo context: %s", job_id, exc,
        )
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
    previous_result: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Deterministic mock response to a Gate-2 regeneration prompt.

    Without an OPENROUTER_API_KEY / real InfraCost, this lets the feedback
    loop be exercised end-to-end: each round produces a visibly different
    result derived from the previous one plus the prompt, so tests and demo
    runs can confirm the loop actually loops without burning real LLM calls.
    """
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

    # Make the loop's effect unmistakable: flip the architecture on an
    # explicit request, otherwise keep it stable.
    if any(k in feedback.lower() for k in ("lambda", "serverless", "function")):
        result["architecture_recommendation"] = "lambda"
    elif any(k in feedback.lower() for k in ("ec2", "vm", "instance")):
        result["architecture_recommendation"] = "ec2"
    elif "ecs" in feedback.lower() or "fargate" in feedback.lower():
        result["architecture_recommendation"] = "ecs_fargate"

    return result


def _money_amount(value: Any) -> Any:
    """The scalar inside a Money dump ({amount,...}) — or the scalar itself."""
    if isinstance(value, dict):
        return value.get("amount")
    return value


def normalize_infracost_result(result: dict[str, Any]) -> dict[str, Any]:
    """
    Defensive normalization on top of Karim's to_orchestrator_result().

    It renames 6 of the 7 InfraCostResult fields correctly but passes
    cost_estimate through as a raw Money dump ({amount, currency, range_min,
    range_max}) rather than the documented {monthly_cost_usd, currency,
    breakdown, ...}; load_scenarios/region_comparison nest Money under
    estimated_monthly_cost, and optimizations uses FinOps naming (name /
    reason / projected_monthly_savings). Unnormalized, the report's cost
    tables render blank for real runs (caught by test_schema_conformance.py).
    Idempotent: already-normalized input passes through unchanged, so this is
    safe to call even if Karim fixes the mapping upstream later.
    """
    result = dict(result)
    cost = dict(result.get("cost_estimate") or {})
    if "monthly_cost_usd" not in cost and "amount" in cost:
        cost = {
            "monthly_cost_usd": cost.get("amount", 0),
            "currency": cost.get("currency", "USD"),
            "breakdown": cost.get("breakdown") or [],  # this agent never splits by service
            "range_min": cost.get("range_min"),
            "range_max": cost.get("range_max"),
        }
        result["cost_estimate"] = cost

    # load_scenarios: Money dict -> estimated_monthly_cost_usd, and derive the
    # documented scaling_assumptions prose from the agent's sizing dict.
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

    # optimizations: FinOps naming -> documented naming.
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

    # region_comparison: Money dict -> documented monthly_cost_usd.
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


# =============================================================================
# 3. INFRACOST -> DEPLOYOPS TRANSLATION
# =============================================================================
# The two contracts diverge in four ways, and Pydantic does NOT reject the
# mismatch: DeployOps's Artifacts.docker_images defaults to [], so a payload
# with InfraCost's singular `dockerfile` + `docker_image` validates cleanly
# and deploys zero images - the failure surfaces deep inside AWS, far from
# its cause. Hence this explicit, loud translation step.
#
#   (a) docker_image (singular obj) + dockerfile (str)  ->  docker_images (list)
#   (b) `context` and `platform` don't exist upstream   ->  defaulted here
#   (c) aws_config.ecs.cluster                          ->  aws_config.ecs_cluster
#   (d) deployment_config.ecs.{...}                     ->  flat deployment_config
#
# TODO: delete this function if Karim and Oussema converge their schemas.

_DEFAULT_PLATFORM = "linux/amd64"   # Fargate requires amd64 unless Graviton
_DEFAULT_CONTEXT = "."


def _raise_missing_image_dockerfile(name: str | None, job_id: str) -> str:
    """Loud failure when a plural image entry lacks Dockerfile content.

    Mirrors the singular guard above: an image without a Dockerfile would
    silently build nothing. A missing ``dockerfile`` on a multi-container
    entry is always a contract regression — refuse to ship it.
    """
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

    Args:
        job_id: orchestrator job id (DeployOps requires a non-empty job_id).
        deploy_inputs: the "_deploy_inputs" block stashed by
            _run_infracost_pipeline() - compute_type / artifacts / aws_config
            / deployment_config, model_dump(by_alias=True).
        approved_by: whoever approved human gate 2.
        is_update: True for an "update deployment" run -- redeploy onto the
            project's existing ECS service instead of provisioning fresh
            infrastructure. When True, InfraCost's freshly-sized aws_config
            is ignored for cluster/service targeting in favor of
            `existing_deployment` (the live service to update).
        existing_deployment: {region, ecs_cluster, service_name} of the
            currently live deployment, resolved by the backend. Required
            when is_update=True.
        repo_url: source repository URL, forwarded so DeployOps can clone the
            code into its build workspace. Without it DeployOps would try to
            build an image from an empty context (only Dockerfile + terraform),
            which fails for any real repo (e.g. `npm ci` finds no package.json).

    Raises:
        ValueError: if the compute type is one DeployOps can't deploy, or if
            no Dockerfile content is available. Failing loudly here is the
            whole point - the alternative is a silent no-op that only breaks
            later, inside AWS.
    """
    compute_type = deploy_inputs.get("compute_type")
    artifacts = deploy_inputs.get("artifacts") or {}
    aws_config = deploy_inputs.get("aws_config") or {}
    deployment_config = deploy_inputs.get("deployment_config") or {}

    # Lambda has no deploy path yet — fall back to ec2 when guard is set.
    import os as _os

    if compute_type not in ("ecs", "ec2", "s3"):
        if compute_type == "lambda" and _os.getenv("DEVGUARD_FORCE_COMPUTE_ECS", "1").lower() == "1":
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "[%s] InfraCost recommended lambda, but DeployOps has no lambda path yet — "
                "falling back to ec2 (DEVGUARD_FORCE_COMPUTE_ECS=1)",
                job_id,
            )
            compute_type = "ec2"
            deploy_inputs["compute_type"] = "ec2"
            # Lambda outputs carry no container image (zip path); ec2 needs one.
            # Synthesize a minimal docker image entry so the later validation
            # ("must contain at least one image") passes — the real Dockerfile
            # content will be regenerated from the repo clone in DeployOps.
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
            # Also regenerate Terraform for ec2 — the lambda terraform carries
            # a zip-based aws_lambda_function that will fail apply when the
            # compute type is remapped. Synthesize a valid ec2 stack instead.
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
            except Exception as _exc:  # noqa: BLE001 - best-effort fallback
                import logging as _lg

                _lg.getLogger(__name__).warning("[%s] ec2 terraform regeneration failed, keeping original: %s", job_id, _exc)
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
                f"but InfraCost recommended compute_type={compute_type!r}. No "
                f"{compute_type!r} mapping exists in DeployOps's AWSConfig model yet."
            )

    # This InfraCost build GENERATES the Dockerfile content itself
    # (output_builder.resolve_docker_artifacts -> f"FROM {base_image}\nCOPY
    # . /app\n"), unlike an earlier API that only pointed at a source_code
    # path into CodeSec's already-deleted clone. Keep failing loudly if this
    # ever regresses. S3 static sites ship plain files and carry no image.
    # Multi-container payloads carry the canonical plural `docker_images`
    # list (name/dockerfile/context/tag/platform per image); legacy payloads
    # fall back to the singular `dockerfile` + `docker_image`.
    plural_images = artifacts.get("docker_images")
    dockerfile_content = artifacts.get("dockerfile")
    if compute_type != "s3" and not plural_images and not dockerfile_content:
        raise ValueError(
            "artifacts.dockerfile is empty - InfraCost produced no Dockerfile "
            "content (e.g. a Lambda deployment, which doesn't use one) or its "
            "Dockerfile-generation logic changed. Refusing to send DeployOps "
            "an image build with no Dockerfile."
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
            # Neither field exists upstream; InfraCost never emits them.
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
            "health_check_path": dep_block.get("health_check_path", "/health"),
            "health_check_port": dep_block.get("health_check_port", 80),
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
        # compute_type IS included: sanitize_and_validate() uses it to skip the
        # ECS-only ecs_cluster/service_name checks for EC2/S3. DeployOps's
        # _normalize_payload() re-derives docker_images from the *older*,
        # singular InfraCost-raw shape (artifacts.docker_image) whenever it sees
        # a top-level "compute_type" key -- so a payload that already carries
        # the correct docker_images list (plural) must be preserved, which
        # _normalize_payload() now does. Omitting compute_type (the old
        # workaround) made every EC2/S3 deploy fail validation because the
        # payload silently defaulted to compute_type="ecs".
        "compute_type": compute_type,
    }

    return payload


# =============================================================================
# 4. DEPLOYOPS (Oussema)
# =============================================================================

async def call_deployops(deploy_payload: dict[str, Any], job_id: str) -> dict[str, Any]:
    """
    Deploy to AWS and return a result in the orchestrator's DeployOpsResult shape.

    Real contract (src/agents/deployops/agent.py):
        agent = DeployOpsAgent()
        result: dict = await agent.deploy(payload)

    DeployOps returns its own flat dict ({"status": "success"|"failed", ...}),
    which is NOT the orchestrator's DeployOpsResult shape (deployment_status,
    health_check{}, terraform_outputs{}, rollback_triggered...). The mapping
    happens here.
    """
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
        """Terraform outputs come back as {"key": {"value": ...}}."""
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
            # Prefer explicit health_check details from DeployOps when they
            # are provided (measured latency, status code, timestamp). If
            # the agent only reports overall status, fall back to inferred
            # defaults to keep the orchestrator shape stable.
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