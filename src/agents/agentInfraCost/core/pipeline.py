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

from pydantic import BaseModel

from core.cost_estimator import estimate_cost
from core.decision_engine import DecisionResult
from core.finops_optimizer import FinOpsRecommendation, optimize_finops
from core.input_validator import InputValidationError, validate_input
from core.llm_architecture_advisor import decide_architecture_via_llm
from core.llm_deployment_advisor import decide_deployment_context
from core.llm_enrichment import build_enrichment
from core.output_builder import build_output, resolve_docker_artifacts
from core.terraform_generator import generate_terraform
from models.output_schema import InfraCostOutput


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


def _run_pipeline_internal(raw: dict) -> PipelineContext:
    """The actual pipeline. Both public entry points below call this and
    only differ in how much of the result they hand back — the steps
    themselves, and every error-handling decision, live here exactly once.
    """
    analysis = validate_input(raw)  # not wrapped: already typed + carries job_id

    try:
        decision = decide_architecture_via_llm(analysis)
    except Exception as exc:
        raise PipelineStageError("decision_engine", exc) from exc

    try:
        dockerfile, docker_image = resolve_docker_artifacts(analysis, decision)
        terraform_context = decide_deployment_context(
            analysis,
            job_id=analysis.job_id,
            docker_image=f"{docker_image.name}:{docker_image.tag}" if docker_image else None,
        )
        terraform_files = generate_terraform(decision, terraform_context)
    except Exception as exc:
        raise PipelineStageError("terraform_generator", exc) from exc

    try:
        cost = estimate_cost(decision)
    except Exception as exc:
        raise PipelineStageError("cost_estimator", exc) from exc

    try:
        finops = optimize_finops(analysis, decision)
    except Exception as exc:
        raise PipelineStageError("finops_optimizer", exc) from exc

    try:
        enrichment = build_enrichment(decision, cost, finops)
    except Exception as exc:
        raise PipelineStageError("llm_enrichment", exc) from exc

    try:
        output = build_output(
            analysis, decision, terraform_files, cost, enrichment, dockerfile, docker_image
        )
    except Exception as exc:
        raise PipelineStageError("output_builder", exc) from exc

    return PipelineContext(output=output, decision=decision, finops=finops)


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
