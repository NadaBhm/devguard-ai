"""Step 9 of the InfraCost pipeline: orchestrate modules 1-4, 6, 7 and 10.

Architecture decision (module 2) now goes through
``decide_architecture_via_llm`` (Phase B): an LLM picks ``compute_type``
when ``OPENROUTER_API_KEY`` is set and the call succeeds, otherwise it
transparently falls back to ``decide_architecture``'s deterministic scoring
— see ``core/llm_architecture_advisor.py`` for the full contract. The
Terraform deployment context (region/environment, module 3's inputs) goes
through ``decide_deployment_context`` (Phase C) the same way — see
``core/llm_deployment_advisor.py``.

A single synchronous entry point, ``run_pipeline``, calling every module in
order. Deliberately a plain function, not ``async def``: the shared
orchestrator (``src/subgroup2/orchestrator/graph.py``) integrates every
agent as a synchronous, in-process call today — no agent anywhere in this
project is async yet. Any future async caller can wrap this in
``asyncio.to_thread(run_pipeline, raw)`` without this module changing at
all; making it async pre-emptively, with no async caller to justify it,
would be the wrong default.

Error handling is precise per stage: module 1's own typed exceptions
(``InvalidStatusError``, ``LowConfidenceError``, ...) already name exactly
what went wrong and carry a ``job_id`` — they propagate unwrapped, since
wrapping them would only obscure that detail. Every other stage's failure
is wrapped in ``PipelineStageError``, naming which stage failed — never a
bare, ambiguous traceback.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Final

from core.cost_estimator import estimate_cost
from core.decision_engine import DecisionResult
from core.finops_optimizer import FinOpsRecommendation, optimize_finops
from core.input_validator import validate_input
from core.llm_architecture_advisor import decide_architecture_via_llm
from core.llm_deployment_advisor import decide_deployment_context
from core.llm_enrichment import build_enrichment
from core.llm_terraform_refiner import refine_terraform
from core.output_builder import build_output, resolve_docker_artifacts
from core.repo_ingestor import ingest_repo
from core.terraform_generator import generate_terraform
from models.output_schema import InfraCostOutput, TerraformFiles
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# On the FIRST Gate-2 regeneration only, the refiner also gets this hidden
# instruction (appended to the user's own feedback): correct the container
# port and health-check path to match the real server config in the repo
# context. The base template hardcodes 8080 + "/health" (constants.py), which
# is wrong for most apps (e.g. a Next.js server on 3000 with /api/health) and
# silently fails the ECS health check -> rollback. The first regen is the one
# place where a deterministic "make the artifacts match the repo" pass is
# warranted; later regens are owned by the user's own prompts and must not be
# overridden. Keep it invisible to the user-visible feedback (Gate-2 logs show
# only what the user typed).
_HIDDEN_FIRST_REGEN_FIX: Final[str] = (
    "Additionally, correct the container port and the health-check path to "
    "match the actual server configuration found in the repo context (use the "
    "real listen port and the real health route the app exposes; for a "
    "Next.js/Express app that is typically 3000 and /api/health). Keep every "
    "other resource and setting strictly identical."
)

# The refiner may change sizing in main.tf when the user asks for cost changes
# ("use 512MB / 0.25 vCPU"). It renders these as Terraform variables —
# `cpu = var.task_cpu` with the real value as a `default` in variables.tf —
# so cost must resolve both the literal and the var-reference form and be
# re-estimated against what actually ships.
_ECS_CPU_PATTERN = re.compile(r"\bcpu\s*=\s*\"?(\d+)\"?")
_ECS_MEMORY_PATTERN = re.compile(r"\bmemory\s*=\s*\"?(\d+)\"?")
_ECS_DESIRED_COUNT_PATTERN = re.compile(r"\bdesired_count\s*=\s*(\d+)")
_EC2_INSTANCE_PATTERN = re.compile(r"\binstance_type\s*=\s*\"([^\"]+)\"")
_LAMBDA_MEMORY_PATTERN = re.compile(r"\bmemory_mb\s*=\s*\"?(\d+)\"?")
_VAR_REFERENCE_PATTERN = re.compile(r"\bvar\.[a-z_]+")


class PipelineContext(BaseModel):
    """Everything ``run_pipeline()`` computes internally, for callers that
    need more than the final ``InfraCostOutput`` contract exposes.

    Currently used by ``core.orchestrator_adapter``, which needs
    ``decision`` and ``finops`` to derive fields (load scenarios, region
    comparison, FinOps options) that ``InfraCostOutput`` itself never
    carries — see that module's docstring for why.
    """

    output: InfraCostOutput
    decision: DecisionResult
    finops: FinOpsRecommendation


class PipelineStageError(Exception):
    """A pipeline stage other than module 1 failed.

    Names exactly which stage, and keeps the original exception attached
    (both as ``original_exception`` and via ``raise ... from``) — never an
    ambiguous, unattributed crash.
    """

    def __init__(self, stage: str, original_exception: Exception) -> None:
        self.stage = stage
        self.original_exception = original_exception
        super().__init__(f"Pipeline failed at stage '{stage}': {original_exception}")


def _resolve_terraform_value(
    main_tf: str, variables_tf: str, literal_pattern: re.Pattern[str], var_name: str
) -> str | None:
    """Resolve a sizing value from refined Terraform.

    Tries the literal form first (``cpu = "512"`` in main.tf), then the
    variable-reference form the refiner produces (``cpu = var.task_cpu`` with
    ``default = "512"`` in variables.tf). Returns ``None`` when neither form
    carries a usable value.
    """
    literal = literal_pattern.search(main_tf)
    if literal:
        return literal.group(1)
    if not _VAR_REFERENCE_PATTERN.search(main_tf):
        return None
    var_block = re.search(
        rf'variable\s+"{re.escape(var_name)}"\s*\{{.*?default\s*=\s*("?[^"\n}}]+"?)',
        variables_tf,
        re.DOTALL,
    )
    if not var_block:
        return None
    value = var_block.group(1).strip().strip('"')
    return value or None


def _sizing_from_refined_terraform(
    main_tf: str, variables_tf: str, compute_type: str
) -> dict[str, int | str] | None:
    """Extract sizing back out of a refined ``main.tf``.

    The Gate-2 refiner may rewrite ``cpu``/``memory``/``desired_count`` (ECS),
    ``instance_type`` (EC2) or ``memory_mb`` (lambda) when the user asks for
    cost changes. Cost must be re-estimated against those real values, not the
    pre-regen decision. Returns the extracted sizing dict, or ``None`` when the
    files don't carry usable values (fail-soft: keep the original decision).
    """
    try:
        if compute_type == "ecs":
            cpu = _resolve_terraform_value(main_tf, variables_tf, _ECS_CPU_PATTERN, "task_cpu")
            memory = _resolve_terraform_value(
                main_tf, variables_tf, _ECS_MEMORY_PATTERN, "task_memory"
            )
            if not cpu or not memory:
                return None
            sizing: dict[str, int | str] = {
                "task_cpu": int(cpu),
                "task_memory": int(memory),
            }
            desired = _resolve_terraform_value(
                main_tf, variables_tf, _ECS_DESIRED_COUNT_PATTERN, "desired_count"
            )
            if desired:
                sizing["desired_count"] = int(desired)
            return sizing
        if compute_type == "ec2":
            inst = _EC2_INSTANCE_PATTERN.search(main_tf)
            if not inst:
                return None
            return {"instance_type": inst.group(1)}
        if compute_type == "lambda":
            mem = _resolve_terraform_value(
                main_tf, variables_tf, _LAMBDA_MEMORY_PATTERN, "memory_mb"
            )
            if not mem:
                return None
            return {"memory_mb": int(mem)}
        if compute_type == "s3":
            # No compute sizing to extract — S3 scales on storage, not CPU.
            return {}
    except Exception:
        pass
    return None


def _recompute_decision_from_refined(
    decision: DecisionResult, terraform_files: TerraformFiles
) -> DecisionResult:
    """Rebuild the decision from the refiner's actual Terraform output.

    Returns the original ``decision`` unchanged when the refined Terraform
    carries no readable sizing (fail-soft — cost then matches the pre-regen
    decision, which is still internally consistent).
    """
    sizing = _sizing_from_refined_terraform(
        terraform_files.main_tf, terraform_files.variables_tf, decision.compute_type
    )
    if not sizing:
        return decision
    return decision.model_copy(update={"sizing": sizing})


def _run_pipeline_internal(raw: dict) -> PipelineContext:
    """The actual pipeline. Both public entry points below call this and
    only differ in how much of the result they hand back — the steps
    themselves, and every error-handling decision, live here exactly once.
    """
    analysis = validate_input(raw)  # not wrapped: already typed + carries job_id

    # Gate-2 regeneration: digest the whole repository so the OpenRouter LLM
    # advisors and the Terraform refiner see the real code, not just the
    # stack-detection metadata module 1 carries. Fail-soft by design: no
    # repo_path, no digest, or any failure -> the pipeline proceeds exactly
    # as before (repo_context stays None).
    if raw.get("repo_path") and analysis.user_feedback:
        try:
            repo_context = ingest_repo(
                Path(raw["repo_path"]),
                analysis.job_id,
                commit_sha=analysis.repo_metadata.commit_sha,
            )
        except Exception as exc:
            logger.warning(
                "[%s] Repo digestion failed; regenerating without repo context: %s",
                analysis.job_id, exc,
            )
            repo_context = None
        if repo_context:
            analysis = analysis.model_copy(update={"repo_context": repo_context})

    try:
        decision = decide_architecture_via_llm(analysis)
    except Exception as exc:
        raise PipelineStageError("decision_engine", exc) from exc

    try:
        dockerfile, docker_image = resolve_docker_artifacts(analysis, decision)
        # Prefer the real Dockerfile content the CodeSec agent extracted
        # (agent.payload["dockerfile_content"]), never a synthesized stand-in.
        if raw.get("dockerfile_content"):
            dockerfile = raw["dockerfile_content"]
        terraform_context = decide_deployment_context(
            analysis,
            job_id=analysis.job_id,
            docker_image=f"{docker_image.name}:{docker_image.tag}" if docker_image else None,
        )
        # A bare "name:tag" resolves against Docker Hub by default, not our
        # ECR repo, so ECS fails with CannotPullContainerError. Qualify it
        # with the ECR registry host once account_id and region (the latter
        # decided above by decide_deployment_context) are both known.
        # account_id may be absent (STS call failed upstream in the
        # orchestrator adapter) — fail-soft and keep the bare name rather
        # than raise, same policy as the rest of this pipeline.
        if terraform_context.docker_image and analysis.account_id:
            ecr_registry = f"{analysis.account_id}.dkr.ecr.{terraform_context.region}.amazonaws.com"
            terraform_context.docker_image = f"{ecr_registry}/{terraform_context.docker_image}"
        terraform_files = generate_terraform(decision, terraform_context)
        # Gate-2 feedback: let the LLM edit the rendered files (and the
        # effective Dockerfile, when the user asked for Docker changes) to
        # honor the request. Fail-soft — unchanged files if the refiner
        # can't run.
        if analysis.user_feedback:
            feedback = analysis.user_feedback
            # First regen only: append the hidden repo-conformance fix so the
            # container port / health check match the app instead of the
            # template's hardcoded 8080 + "/health". Later regens (2+) keep
            # exactly what the user typed — their prompts own the artifacts.
            if int(raw.get("regen_iteration") or 0) == 1:
                feedback = f"{feedback}\n\n{_HIDDEN_FIRST_REGEN_FIX}"
            terraform_files, dockerfile = refine_terraform(
                terraform_files,
                feedback,
                dockerfile=dockerfile,
                repo_context=analysis.repo_context,
            )
    except Exception as exc:
        raise PipelineStageError("terraform_generator", exc) from exc

    # After Gate-2 feedback the refiner may have rewritten sizing in main.tf;
    # cost must reflect what actually ships, not the pre-regen decision.
    # Fail-soft: if the refined Terraform carries no readable sizing, this
    # returns the original decision and cost stays unchanged. The recomputed
    # decision is threaded through every downstream stage (finops, enrichment,
    # output) so the reported sizing, cost and FinOps scenarios stay in sync.
    refined_decision = _recompute_decision_from_refined(decision, terraform_files)
    try:
        cost = estimate_cost(refined_decision)
    except Exception as exc:
        raise PipelineStageError("cost_estimator", exc) from exc

    try:
        finops = optimize_finops(analysis, refined_decision)
    except Exception as exc:
        raise PipelineStageError("finops_optimizer", exc) from exc

    try:
        enrichment = build_enrichment(refined_decision, cost, finops)
    except Exception as exc:
        raise PipelineStageError("llm_enrichment", exc) from exc

    try:
        output = build_output(
            analysis,
            refined_decision,
            terraform_files,
            cost,
            enrichment,
            dockerfile,
            docker_image,
            region=terraform_context.region,
            environment=terraform_context.environment,
        )
    except Exception as exc:
        raise PipelineStageError("output_builder", exc) from exc

    return PipelineContext(output=output, decision=refined_decision, finops=finops)


def run_pipeline(raw: dict) -> InfraCostOutput:
    """Run the full InfraCost pipeline on a raw Agent 1 payload.

    Args:
        raw: Agent 1's raw analysis payload (already-decoded JSON).

    Returns:
        The final ``InfraCostOutput`` contract.

    Raises:
        InputValidationError: (or a subclass) module 1 rejected the
            payload — propagates unwrapped, already precisely typed.
        PipelineStageError: any other stage failed; ``.stage`` names which
            one, ``.original_exception`` holds the real cause.
    """
    return _run_pipeline_internal(raw).output


def run_pipeline_with_context(raw: dict) -> PipelineContext:
    """Like ``run_pipeline``, but also returns the internal ``DecisionResult``
    and ``FinOpsRecommendation`` computed for this same job.

    Same arguments and exceptions as ``run_pipeline`` — this is not a
    different pipeline, just a richer view into the one pipeline that
    exists. Intended for ``core.orchestrator_adapter``; the HTTP endpoint
    in ``main.py`` should keep using plain ``run_pipeline``.
    """
    return _run_pipeline_internal(raw)
