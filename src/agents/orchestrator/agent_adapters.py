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

On top of that, InfraCost's OUTPUT shape and DeployOps's INPUT shape do not
line up (see translate_infracost_to_deploy_payload below for the gory
details). Rather than smearing those mismatches across nodes.py, everything
is isolated here: when the agents change their contracts, THIS is the only
file that needs to change.

MOCK MODE
---------
Until all four feature branches are merged onto master, the real agent
modules simply don't exist in this branch. Every call_* function therefore
falls back to the Sprint 1 mock payloads unless explicitly switched on:

    DEVGUARD_REAL_CODESEC=1
    DEVGUARD_REAL_INFRACOST=1
    DEVGUARD_REAL_DEPLOYOPS=1
    DEVGUARD_REAL_AGENTS=1      # turns on all three at once

Imports of the real agents are LAZY (inside the functions), so this module
imports cleanly even when none of the agent packages are present.

CDC Reference: T-2.17 (wire orchestrator to real CodeSec/DeployOps),
               T-3.16 (integrate InfraCost into orchestrator workflow)

Owner: Hbib (Subgroup 2 - Execution & Control)
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# =============================================================================
# FEATURE FLAGS
# =============================================================================

def _flag(name: str) -> bool:
    """Read a DEVGUARD_REAL_* env flag (also honours the global switch)."""
    if os.getenv("DEVGUARD_REAL_AGENTS", "").strip() in ("1", "true", "True"):
        return True
    return os.getenv(name, "").strip() in ("1", "true", "True")


def use_real_codesec() -> bool:
    return _flag("DEVGUARD_REAL_CODESEC")


def use_real_infracost() -> bool:
    return _flag("DEVGUARD_REAL_INFRACOST")


def use_real_deployops() -> bool:
    return _flag("DEVGUARD_REAL_DEPLOYOPS")


# =============================================================================
# ASYNC -> SYNC BRIDGE
# =============================================================================

def run_sync(coro: "Any") -> Any:
    """
    Drive an async agent call from a synchronous LangGraph node.

    Why this exists: two of the three agents are async, but the graph itself
    must stay synchronous. `graph.invoke()` cannot execute async nodes
    (LangGraph raises InvalidUpdateError: "Expected dict, got coroutine"),
    and switching the graph to `ainvoke()` would force run_workflow() /
    resume_workflow() to become async too — which would break the backend,
    where they are called from ordinary sync FastAPI endpoints
    (src/backend/api/jobs.py). The public API is a contract with Oussema's
    code; the async-ness of the agents is an implementation detail. So the
    bridge lives here.

    If no event loop is running (the normal case: a sync FastAPI endpoint is
    executed in a threadpool), asyncio.run() is enough. If one IS already
    running, asyncio.run() would raise, so the coroutine is handed to a
    dedicated thread with its own loop.
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

async def call_infracost(codesec_result: dict[str, Any], job_id: str) -> dict[str, Any]:
    """
    Run InfraCost on CodeSec's output and return the orchestrator-shaped result.

    Real contract (src/agents/agentInfraCost/core/):
        ctx = run_pipeline_with_context(codesec_result)   # SYNC
        result = to_orchestrator_result(ctx.output, ctx.decision, ctx.finops)

    Two things to know:

    1. We must use run_pipeline_with_context(), NOT plain run_pipeline().
       Karim's to_orchestrator_result() needs `decision` and `finops`, which
       only the _with_context variant exposes. run_pipeline() returns just
       the output and would leave us unable to build load_scenarios,
       optimizations and region_comparison.

    2. run_pipeline_with_context is SYNCHRONOUS and CPU/LLM-bound (it calls
       an LLM advisor and the AWS Pricing API). Calling it directly inside an
       async node would block the whole event loop, freezing WebSocket
       progress streaming for every other job in the process. It therefore
       goes through run_in_executor.

    Returns the orchestrator's InfraCostResult shape, PLUS an extra
    orchestrator-internal key `_deploy_inputs` holding the raw artifacts /
    aws_config / deployment_config that DeployOps needs later. The
    orchestrator's InfraCostResult TypedDict has no field for the Dockerfile
    or source_code, so without stashing them here they would be lost between
    the InfraCost node and the DeployOps node.
    """
    if not use_real_infracost():
        from .nodes import build_mock_infracost_result
        logger.info("[%s] InfraCost: MOCK mode (set DEVGUARD_REAL_INFRACOST=1 for real)", job_id)
        return build_mock_infracost_result()

    from agents.agentInfraCost.core.orchestrator_adapter import to_orchestrator_result
    from agents.agentInfraCost.core.pipeline import run_pipeline_with_context

    logger.info("[%s] InfraCost: REAL agent", job_id)
    loop = asyncio.get_event_loop()
    ctx = await loop.run_in_executor(None, run_pipeline_with_context, codesec_result)

    result: dict[str, Any] = normalize_infracost_result(
        dict(to_orchestrator_result(ctx.output, ctx.decision, ctx.finops))
    )

    # Stash what DeployOps will need but the orchestrator TypedDict drops.
    output = ctx.output
    result["_deploy_inputs"] = {
        "compute_type": output.compute_type,
        "artifacts": output.artifacts.model_dump(mode="json", by_alias=True),
        "aws_config": output.aws_config.model_dump(mode="json", by_alias=True),
        "deployment_config": output.deployment_config.model_dump(mode="json", by_alias=True),
    }
    return result


# =============================================================================
# INFRACOST OUTPUT NORMALIZATION
# =============================================================================
# Karim's to_orchestrator_result() fills the orchestrator's seven InfraCostResult
# keys, but the VALUES inside five of them are his own Pydantic models dumped
# as-is, and none of their field names match what
# docs/api-contracts/orchestrator-input-schema.json declares:
#
#   schema                                     real output
#   ------                                     -----------
#   cost_estimate.monthly_cost_usd             cost_estimate.amount        (Money)
#   cost_estimate.breakdown[]                  (does not exist at all)
#   generated_terraform.main_tf                generated_terraform.files["main.tf"]
#   optimizations[].projected_savings_usd      optimizations[].projected_monthly_savings
#   optimizations[].strategy / .description    optimizations[].name / .reason
#   load_scenarios[].estimated_monthly_cost_usd  load_scenarios[].estimated_monthly_cost.amount
#   region_comparison[].monthly_cost_usd       region_comparison[].estimated_monthly_cost.amount
#
# This is not caught by validation: the schema declares those sub-properties
# without marking any of them `required`, so the divergent shape passes
# cleanly. What actually breaks is downstream - report.py reads
# cost_estimate["monthly_cost_usd"], finds nothing, and prints "$0/month" in
# the report handed to stakeholders. Same silent-wrong-answer failure mode as
# the empty docker_images list.
#
# Normalizing here (rather than teaching every consumer both dialects) keeps
# the schema as the single contract: whatever the agent emits, the state
# always holds the documented shape.
#
# TODO: delete once Karim's to_orchestrator_result() emits schema field names.

def _money_amount(value: Any) -> float:
    """Read a cost that may be a bare number or one of Karim's Money objects."""
    if isinstance(value, dict):
        return float(value.get("amount", value.get("monthly_cost_usd", 0)) or 0)
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# Karim's optimization `name` comes from an LLM and is free text; the schema
# declares a closed enum. Map what we recognise, pass the rest through.
_STRATEGY_ALIASES = {
    "graviton": "graviton",
    "arm": "graviton",
    "spot": "spot_instances",
    "spot instances": "spot_instances",
    "reserved": "reserved_instances",
    "reserved instances": "reserved_instances",
    "savings plan": "savings_plan",
    "savings_plan": "savings_plan",
}


def _normalize_strategy(name: str) -> str:
    lowered = (name or "").strip().lower()
    for needle, canonical in _STRATEGY_ALIASES.items():
        if needle in lowered:
            return canonical
    return name or "unspecified"


def normalize_infracost_result(result: dict[str, Any]) -> dict[str, Any]:
    """
    Coerce InfraCost's output into the shape orchestrator-input-schema.json
    documents. Idempotent: a payload already in canonical form (the Sprint 1
    mock) passes through unchanged.
    """
    out = dict(result)

    # --- cost_estimate: Money -> {monthly_cost_usd, currency, breakdown} ---
    cost = dict(out.get("cost_estimate") or {})
    if "monthly_cost_usd" not in cost:
        cost = {
            "monthly_cost_usd": _money_amount(cost),
            "currency": cost.get("currency", "USD"),
            # Karim's pipeline produces a single total, never a per-service
            # split, so there is nothing to populate this with. Left empty
            # rather than faked; the report simply omits the breakdown table.
            "breakdown": cost.get("breakdown") or [],
            # Keep the uncertainty range - it's real information the schema
            # has no field for, and dropping it would lose it entirely.
            "range_min": cost.get("range_min"),
            "range_max": cost.get("range_max"),
        }
    out["cost_estimate"] = cost

    # --- generated_terraform: {files: {...}} -> {main_tf, variables_tf, ...} ---
    terraform = dict(out.get("generated_terraform") or {})
    if "files" in terraform and "main_tf" not in terraform:
        files = terraform.get("files") or {}
        out["generated_terraform"] = {
            "main_tf": files.get("main.tf", ""),
            "variables_tf": files.get("variables.tf", ""),
            "outputs_tf": files.get("outputs.tf", ""),
            # The agent validates with `terraform plan` before emitting; if it
            # hadn't passed there would be no artifacts to normalize.
            "plan_passed": terraform.get("plan_passed", True),
            "variables": terraform.get("variables") or {},
        }

    # --- optimizations: {name, reason, projected_monthly_savings} -> schema ---
    optimizations = []
    for option in out.get("optimizations") or []:
        if "strategy" in option and "projected_savings_usd" in option:
            optimizations.append(option)
            continue
        optimizations.append({
            "strategy": _normalize_strategy(option.get("name", "")),
            "projected_savings_usd": option.get(
                "projected_monthly_savings", option.get("projected_savings_usd", 0)
            ) or 0,
            "description": option.get("reason") or option.get("description") or "",
            "selected": option.get("selected"),
        })
    out["optimizations"] = optimizations

    # --- load_scenarios: estimated_monthly_cost (Money) -> ..._usd ---
    scenarios = []
    for scenario in out.get("load_scenarios") or []:
        if "estimated_monthly_cost_usd" in scenario:
            scenarios.append(scenario)
            continue
        sizing = scenario.get("sizing") or {}
        scenarios.append({
            "users": scenario.get("users"),
            "estimated_monthly_cost_usd": _money_amount(
                scenario.get("estimated_monthly_cost")
            ),
            # The schema wants prose; the agent gives a sizing dict. Render it
            # rather than leaving the column blank.
            "scaling_assumptions": scenario.get("scaling_assumptions")
            or ", ".join(f"{k}: {v}" for k, v in sizing.items())
            or "",
        })
    out["load_scenarios"] = scenarios

    # --- region_comparison: estimated_monthly_cost (Money) -> monthly_cost_usd ---
    regions = []
    for region in out.get("region_comparison") or []:
        if "monthly_cost_usd" in region:
            regions.append(region)
            continue
        regions.append({
            "region": region.get("region", ""),
            "monthly_cost_usd": _money_amount(region.get("estimated_monthly_cost")),
        })
    out["region_comparison"] = regions

    return out


# =============================================================================
# 3. INFRACOST -> DEPLOYOPS TRANSLATION
# =============================================================================
# This is the messiest part of the integration. The two contracts diverge in
# four separate ways, and — critically — Pydantic does NOT reject the
# mismatch. DeployOps's Artifacts model declares
#     docker_images: List[DockerImageConfig] = Field(default_factory=list)
# so a payload carrying InfraCost's singular `dockerfile` + `docker_image`
# validates cleanly, silently yielding docker_images == []. deploy() then
# loops over zero images, builds/pushes nothing to ECR, and terraform apply
# later references an image tag that was never published. The failure
# surfaces deep inside AWS, far from its real cause. Hence this explicit,
# loud translation step.
#
#   (a) docker_image (singular obj) + dockerfile (str)  ->  docker_images (list)
#   (b) `context` and `platform` don't exist upstream   ->  defaulted here
#   (c) aws_config.ecs.cluster                          ->  aws_config.ecs_cluster
#   (d) deployment_config.ecs.{...}                     ->  flat deployment_config
#
# TODO: delete this function if Karim and Oussema converge their schemas.

_DEFAULT_PLATFORM = "linux/amd64"   # Fargate requires amd64 unless Graviton
_DEFAULT_CONTEXT = "."


def translate_infracost_to_deploy_payload(
    job_id: str,
    deploy_inputs: dict[str, Any],
    *,
    approved_by: str,
) -> dict[str, Any]:
    """
    Build a DeployOps-compatible payload from InfraCost's raw output.

    Args:
        job_id: orchestrator job id (DeployOps requires a non-empty job_id).
        deploy_inputs: the `_deploy_inputs` block stashed by call_infracost().
        approved_by: whoever approved human gate 2.

    Raises:
        ValueError: if the compute type is one DeployOps can't deploy, or if
            a container-based deployment carries no Dockerfile. Failing loudly
            here is the whole point — the alternative is a silent no-op that
            only breaks later, inside AWS.
    """
    compute_type = deploy_inputs.get("compute_type")
    artifacts = deploy_inputs.get("artifacts") or {}
    aws_config = deploy_inputs.get("aws_config") or {}
    deployment_config = deploy_inputs.get("deployment_config") or {}

    # ---- (c) aws_config -------------------------------------------------
    # DeployOps only models ECS (ecs_cluster / service_name). InfraCost can
    # also emit lambda / ec2 blocks, which DeployOps has no fields for.
    ecs_block = aws_config.get("ecs")
    if not ecs_block:
        raise ValueError(
            f"DeployOps currently only supports ECS deployments, but InfraCost "
            f"recommended compute_type={compute_type!r}. No lambda/ec2 mapping "
            f"exists in DeployOps's AWSConfig model yet."
        )

    # ---- (a)+(b) docker images -----------------------------------------
    docker_images: list[dict[str, Any]] = []
    dockerfile = artifacts.get("dockerfile")
    docker_image = artifacts.get("docker_image") or {}

    if dockerfile:
        docker_images.append({
            "name": docker_image.get("name") or f"devguard-{job_id[:8]}",
            "dockerfile": dockerfile,
            # Neither field exists upstream; InfraCost never emits them.
            "context": artifacts.get("source_code") or _DEFAULT_CONTEXT,
            "tag": docker_image.get("tag") or "latest",
            "platform": _DEFAULT_PLATFORM,
        })
    else:
        raise ValueError(
            "ECS deployment requires a Dockerfile, but InfraCost's artifacts "
            "carry none (artifacts.dockerfile is null). Refusing to send a "
            "payload that would silently deploy zero images."
        )

    # ---- terraform files ------------------------------------------------
    # InfraCost dumps by_alias, so keys are already real filenames
    # ("main.tf", "variables.tf", "outputs.tf") -> matches DeployOps's
    # Dict[str, str]. No translation needed, just a defensive copy.
    terraform = artifacts.get("terraform") or {}
    tf_files = dict(terraform.get("files") or {})
    if not tf_files:
        raise ValueError("InfraCost produced no Terraform files; nothing to deploy.")

    # ---- (d) deployment_config -----------------------------------------
    ecs_deploy = deployment_config.get("ecs") or {}
    strategy = ecs_deploy.get("strategy", "rolling")
    # DeployOps's DeploymentStrategy enum uses "blue_green" (underscore);
    # the orchestrator state Literal and InfraCost both say "blue-green".
    if strategy == "blue-green":
        strategy = "blue_green"

    payload = {
        "job_id": job_id,
        "artifacts": {
            "terraform": {
                "files": tf_files,
                "variables": dict(terraform.get("variables") or {}),
            },
            "docker_images": docker_images,
        },
        "aws_config": {
            "region": aws_config.get("region", "us-east-1"),
            "ecs_cluster": ecs_block.get("cluster"),
            "service_name": ecs_block.get("service_name"),
        },
        "deployment_config": {
            "strategy": strategy,
            "health_check_path": ecs_deploy.get("health_check_path", "/health"),
            "health_check_port": ecs_deploy.get("health_check_port", 80),
            "min_healthy_percent": ecs_deploy.get("min_healthy_percent", 50),
            "max_percent": ecs_deploy.get("max_percent", 200),
            "timeout_minutes": ecs_deploy.get("timeout_minutes", 15),
            "auto_rollback": True,
        },
        "approval": {
            "deploy_approved": True,
            "approved_by": approved_by,
            "approved_at": datetime.now(timezone.utc).isoformat(),
        },
        "metadata": {},
    }

    # DeployOps's rollback() does payload["aws_config"]["ecs_cluster"] with no
    # guard, while its own AWSConfig model declares that field Optional. A
    # None here would only blow up later, mid-rollback, when things are
    # already going wrong. Fail now instead.
    if not payload["aws_config"]["ecs_cluster"] or not payload["aws_config"]["service_name"]:
        raise ValueError(
            "InfraCost's aws_config.ecs is missing cluster/service_name; "
            "DeployOps.rollback() would raise KeyError on these later."
        )

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

    from src.agents.deployops.agent import DeployOpsAgent  # lazy

    logger.info("[%s] DeployOps: REAL agent", job_id)
    agent = DeployOpsAgent()
    raw = await agent.deploy(deploy_payload)

    return translate_deployops_result(raw, job_id)


def translate_deployops_result(raw: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Map DeployOps's flat return dict onto the orchestrator's DeployOpsResult."""
    succeeded = raw.get("status") == "success"
    outputs = raw.get("resources") or {}

    def _tf_out(key: str) -> str:
        """Terraform outputs come back as {"key": {"value": ...}}."""
        entry = outputs.get(key)
        if isinstance(entry, dict):
            return entry.get("value") or ""
        return entry or ""

    return {
        "job_id": raw.get("job_id") or job_id,
        "deployment_status": "success" if succeeded else "failed",
        "deployed_url": raw.get("deployed_url"),
        "health_check": {
            # DeployOps runs its own health check internally and only tells us
            # pass/fail via the overall status; it doesn't report latency or
            # status code. Values are inferred, not measured.
            "passed": succeeded,
            "response_time_ms": 0,
            "status_code": 200 if succeeded else 0,
            "checked_at": datetime.now(timezone.utc).isoformat(),
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
