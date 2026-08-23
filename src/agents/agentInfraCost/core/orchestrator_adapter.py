"""Adapter translating this agent's output into the orchestrator's expected shape.

Its ``InfraCostResult`` TypedDict predates this contract (zero shared field names)
and is mirrored locally, NOT imported — the orchestrator file is mid-move. Four
fields come straight from InfraCostOutput; load_scenarios/optimizations/regions need
the internal decision+finops (see below). Update the mirror if the shape changes.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from core.decision_engine import ComputeType, DecisionResult
from core.finops_optimizer import FinOpsRecommendation
from core.region_comparator import compare_regions
from core.scenario_simulator import simulate_load_scenarios
from models.output_schema import InfraCostOutput

OrchestratorArchitecture = Literal["ecs_fargate", "lambda", "ec2", "s3", "hybrid"]


class OrchestratorInfraCostResult(TypedDict):
    """Mirrors the orchestrator's ``InfraCostResult`` TypedDict (as of
    2026-08-05). See the module docstring for why this is a local copy
    rather than an import."""

    architecture_recommendation: OrchestratorArchitecture
    justification: str
    generated_terraform: dict
    cost_estimate: dict
    load_scenarios: list[dict]
    optimizations: list[dict]
    region_comparison: list[dict]
    warnings: list[str]


# "hybrid" exists in the orchestrator's Literal but this agent never produces it —
# decide_architecture_via_llm is restricted to {ecs, lambda, ec2, s3}.
_ARCHITECTURE_RECOMMENDATION: dict[ComputeType, OrchestratorArchitecture] = {
    "ecs": "ecs_fargate",
    "lambda": "lambda",
    "ec2": "ec2",
    "s3": "s3",
}


def to_orchestrator_result(
    output: InfraCostOutput,
    decision: DecisionResult,
    finops: FinOpsRecommendation,
) -> OrchestratorInfraCostResult:
    """Translate this agent's real output into the orchestrator's expected shape.
    ``decision``/``finops`` come from run_pipeline_with_context for the same job —
    used to (re)compute load scenarios and region comparison the output never
    carries. Returns a dict for the orchestrator's "infracost_result" state slot."""
    scenarios = simulate_load_scenarios(decision)
    regions = compare_regions(decision)
    optimizations = [
        {**finops.recommended.model_dump(), "selected": True},
        *({**option.model_dump(), "selected": False} for option in finops.discarded),
    ]

    return OrchestratorInfraCostResult(
        architecture_recommendation=_ARCHITECTURE_RECOMMENDATION[output.compute_type],
        justification=output.enrichment.architecture_explanation,
        generated_terraform=output.artifacts.terraform.model_dump(by_alias=True),
        cost_estimate=output.aws_config.estimated_monthly_cost.model_dump(),
        load_scenarios=[scenario.model_dump() for scenario in scenarios],
        optimizations=optimizations,
        region_comparison=[region.model_dump() for region in regions],
        warnings=[],
    )
