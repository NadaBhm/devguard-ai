"""Adapter translating this agent's real output into the shape the
orchestrator's InfraCost integration point currently expects.

The orchestrator (as of 2026-08-05, ``src/subgroup2/orchestrator/graph.py``
on this branch — a teammate's already-merged commit on ``master`` relocates
it to ``src/agents/orchestrator/``, not yet present here) defines its own
``TypedDict``, ``InfraCostResult``, describing what it expects this agent to
return. It predates this agent's real, Pydantic-validated ``InfraCostOutput``
contract and was never updated to match it — the two shapes share no field
names. It's mirrored below as ``OrchestratorInfraCostResult`` (a local copy,
NOT an import) specifically because the orchestrator file's own path is
mid-move: importing it directly would break the moment that move lands on
this branch. If the orchestrator's expected shape changes, update the
``TypedDict`` below to match it.

Four of its seven fields come straight from ``InfraCostOutput``. The other
three — ``load_scenarios``, ``optimizations``, ``region_comparison`` —
describe data ``InfraCostOutput``'s own contract never carries:
``scenario_simulator.py`` and ``region_comparator.py`` aren't part of
``run_pipeline()``'s normal flow, and FinOps's structured detail
(``OptimizationOption``) only ever gets flattened into a prose sentence in
``enrichment.finops_justification``. So this adapter needs the
``DecisionResult`` and ``FinOpsRecommendation`` ``run_pipeline()`` computes
internally, not just its final output — get both via
``core.pipeline.run_pipeline_with_context``, not plain ``run_pipeline``.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from core.decision_engine import ComputeType, DecisionResult
from core.finops_optimizer import FinOpsRecommendation
from core.region_comparator import compare_regions
from core.scenario_simulator import simulate_load_scenarios
from models.output_schema import InfraCostOutput

OrchestratorArchitecture = Literal["ecs_fargate", "lambda", "ec2", "hybrid"]


class OrchestratorInfraCostResult(TypedDict):
    """Mirrors ``src/subgroup2/orchestrator/graph.py``'s ``InfraCostResult``
    (as of 2026-08-05). See the module docstring for why this is a local
    copy rather than an import.
    """

    architecture_recommendation: OrchestratorArchitecture
    justification: str
    generated_terraform: dict
    cost_estimate: dict
    load_scenarios: list[dict]
    optimizations: list[dict]
    region_comparison: list[dict]


# "hybrid" is part of the orchestrator's Literal but this agent never
# produces it — decide_architecture_via_llm is itself restricted to
# {ecs, lambda, ec2} (see llm_architecture_advisor.py), so there is no
# ComputeType value that would need to map to it.
_ARCHITECTURE_RECOMMENDATION: dict[ComputeType, OrchestratorArchitecture] = {
    "ecs": "ecs_fargate",
    "lambda": "lambda",
    "ec2": "ec2",
}


def to_orchestrator_result(
    output: InfraCostOutput,
    decision: DecisionResult,
    finops: FinOpsRecommendation,
) -> OrchestratorInfraCostResult:
    """Translate this agent's real output into the orchestrator's expected shape.

    Args:
        output: ``run_pipeline_with_context(raw).output`` for a job.
        decision: ``run_pipeline_with_context(raw).decision`` for that same
            job — used to (re)compute load scenarios and region comparison,
            neither of which ``output`` itself carries.
        finops: ``run_pipeline_with_context(raw).finops`` for that same job.

    Returns:
        A dict matching ``OrchestratorInfraCostResult`` — ready to slot into
        the orchestrator's state under ``"infracost_result"``, once
        ``graph.py`` is wired to call this agent instead of its mock.
    """
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
    )
