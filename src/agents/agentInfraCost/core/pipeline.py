"""Module 9: orchestrates modules 1-8 and 10 in order, end to end.

Every stage is wrapped so a failure surfaces as a PipelineStageError naming
exactly which stage failed (`.stage`) and preserving the original typed
exception (`.original_error`) — never a silent crash or a bare traceback
from deep inside one module.

`run_pipeline` produces an InfraCostOutput with approval.status="pending":
it is Agent 2's complete output, awaiting the human approval step that sits
between this agent and Agent 3 in the overall pipeline. `apply_approval`
performs that separate step afterwards, via approval_manager.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, TypeVar

from ..models.exceptions import PipelineStageError
from ..models.internal_models import ApprovalRecord
from ..models.output_models import Approval, InfraCostOutput
from . import (
    approval_manager,
    cost_estimator,
    decision_engine,
    finops_optimizer,
    input_validator,
    llm_enrichment,
    output_builder,
    scenario_simulator,
    terraform_generator,
)

logger = logging.getLogger(__name__)

DEFAULT_REGION = "us-east-1"
DEFAULT_ENVIRONMENT = "dev"

_T = TypeVar("_T")


def _run_stage(stage_name: str, fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - this is the pipeline's single error boundary
        logger.error("Pipeline stage '%s' failed: %s", stage_name, exc)
        raise PipelineStageError(stage_name, exc) from exc


def run_pipeline(
    raw_payload: dict[str, Any],
    *,
    region: str = DEFAULT_REGION,
    environment: str = DEFAULT_ENVIRONMENT,
) -> InfraCostOutput:
    """Runs the full InfraCost pipeline and returns a pending-approval output.

    Stage order: input_validator -> decision_engine -> cost_estimator ->
    scenario_simulator (computed for completeness/logging; not part of the
    wire output) -> finops_optimizer -> terraform_generator ->
    approval_manager (creates the initial 'pending' record) ->
    llm_enrichment (always last) -> output_builder.

    :raises PipelineStageError: if any stage fails; `.stage` names which one
        and `.original_error` carries the underlying typed exception (e.g. a
        LowConfidenceStackDetectionError from input_validator).
    """
    payload = _run_stage("input_validation", input_validator.validate_input, raw_payload)
    decision = _run_stage("decision_engine", decision_engine.decide, payload)
    cost = _run_stage("cost_estimator", cost_estimator.estimate_cost, decision)
    _run_stage("scenario_simulator", scenario_simulator.simulate_scenarios, decision)
    finops = _run_stage("finops_optimizer", finops_optimizer.optimize, payload, decision)

    container_detected = payload.stack_detection.container.detected
    docker_image_uri: Optional[str] = None
    if container_detected:
        image = output_builder.resolve_docker_image(
            repo_name=payload.repo_metadata.name,
            commit_sha=payload.repo_metadata.commit_sha,
        )
        docker_image_uri = f"{image.name}:{image.tag}"

    terraform = _run_stage(
        "terraform_generator",
        terraform_generator.generate_terraform,
        decision,
        region=region,
        environment=environment,
        docker_image_uri=docker_image_uri,
    )

    approval_record = _run_stage(
        "approval_manager", approval_manager.create_approval_record, payload.job_id
    )

    enrichment = _run_stage(
        "llm_enrichment",
        llm_enrichment.enrich,
        decision=decision,
        cost=cost,
        finops=finops,
    )

    output = _run_stage(
        "output_builder",
        output_builder.build_output,
        job_id=payload.job_id,
        decision=decision,
        estimated_monthly_cost=cost,
        terraform_files=terraform.files,
        terraform_variables=terraform.variables,
        region=region,
        repo_name=payload.repo_metadata.name,
        commit_sha=payload.repo_metadata.commit_sha,
        container_detected=container_detected,
        # Agent 1 only reports the Dockerfile's path, not its contents;
        # Agent 3 reads the actual file from the checked-out source_code.
        dockerfile_content=None,
        source_code_path=f"/tmp/repo_{payload.job_id}",
        approval=Approval(status=approval_record.status, approved_by=approval_record.approved_by),
        enrichment=enrichment,
    )
    return output


def apply_approval(
    output: InfraCostOutput, *, approved: bool, approved_by: Optional[str] = None
) -> InfraCostOutput:
    """Applies the human approval/rejection decision to a pending output.

    Returns a new InfraCostOutput with an updated `approval` block; does not
    mutate `output`.

    :raises InvalidApprovalTransitionError: if `output.approval.status` is
        not already "pending" (already approved or rejected)
    :raises ValueError: if approved=True but `approved_by` is not provided
    """
    record = ApprovalRecord(
        job_id=output.job_id,
        status=output.approval.status,
        approved_by=output.approval.approved_by,
    )
    if approved:
        if not approved_by:
            raise ValueError("approved_by is required when approving a job")
        record = approval_manager.approve(record, approved_by=approved_by)
    else:
        record = approval_manager.reject(record)

    return output.model_copy(
        update={"approval": Approval(status=record.status, approved_by=record.approved_by)}
    )
