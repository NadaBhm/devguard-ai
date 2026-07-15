"""Module 5: simulates estimated cost at 1K / 10K / 100K active-user load tiers.

Unlike a naive "rule of three" scaling of the base cost_estimator result,
this module recalculates the actual resource DIMENSIONING needed at each
load tier — the number of running ECS tasks / EC2 instances, or the number
of monthly Lambda invocations — using simple, generic capacity-per-unit
ratios, then reprices that recalculated dimensioning via cost_estimator.
"""

from __future__ import annotations

import logging
import math

from ..models.internal_models import CostEstimateResult, DecisionResult, ScenarioResult
from .cost_estimator import estimate_ec2_cost, estimate_ecs_cost, estimate_lambda_cost

logger = logging.getLogger(__name__)

SCENARIO_USER_LOADS: tuple[int, ...] = (1_000, 10_000, 100_000)

# Generic capacity assumptions used in the absence of real traffic telemetry
# (Agent 1 does not provide any). Deliberately round, conservative numbers,
# not tuned to any specific fixture.
CONCURRENT_USERS_PER_ECS_TASK = 500
CONCURRENT_USERS_PER_EC2_INSTANCE = 300
REQUESTS_PER_ACTIVE_USER_PER_MONTH = 50

# Kept identical to cost_estimator's own baseline assumption so a Lambda
# scenario at "typical" load reproduces the same per-invocation cost model.
LAMBDA_AVG_DURATION_SECONDS = 0.2


def _scale_cost(cost: CostEstimateResult, factor: int) -> CostEstimateResult:
    """Scales an already-computed per-unit CostEstimateResult by `factor`
    identical units. Linear scaling preserves the +-20% range ratio."""
    return CostEstimateResult(
        amount=round(cost.amount * factor, 2),
        currency=cost.currency,
        range_min=round(cost.range_min * factor, 2),
        range_max=round(cost.range_max * factor, 2),
    )


def _simulate_ecs(
    decision: DecisionResult, user_load: int, *, use_graviton: bool
) -> ScenarioResult:
    if decision.ecs is None:
        raise ValueError("DecisionResult.compute_type='ecs' but decision.ecs is None")
    task_count = max(1, math.ceil(user_load / CONCURRENT_USERS_PER_ECS_TASK))
    per_task_cost = estimate_ecs_cost(
        vcpu=decision.ecs.task_cpu / 1024.0,
        memory_gb=decision.ecs.task_memory / 1024.0,
        use_graviton=use_graviton,
    )
    return ScenarioResult(
        user_load=user_load,
        compute_units=task_count,
        cost=_scale_cost(per_task_cost, task_count),
    )


def _simulate_lambda(
    decision: DecisionResult, user_load: int, *, use_graviton: bool
) -> ScenarioResult:
    if decision.lambda_ is None:
        raise ValueError("DecisionResult.compute_type='lambda' but decision.lambda_ is None")
    monthly_invocations = user_load * REQUESTS_PER_ACTIVE_USER_PER_MONTH
    cost = estimate_lambda_cost(
        memory_mb=decision.lambda_.memory_mb,
        avg_duration_seconds=LAMBDA_AVG_DURATION_SECONDS,
        monthly_invocations=monthly_invocations,
        use_graviton=use_graviton,
    )
    return ScenarioResult(user_load=user_load, compute_units=monthly_invocations, cost=cost)


def _simulate_ec2(decision: DecisionResult, user_load: int) -> ScenarioResult:
    if decision.ec2 is None:
        raise ValueError("DecisionResult.compute_type='ec2' but decision.ec2 is None")
    instance_count = max(1, math.ceil(user_load / CONCURRENT_USERS_PER_EC2_INSTANCE))
    per_instance_cost = estimate_ec2_cost(instance_type=decision.ec2.instance_type, instance_count=1)
    return ScenarioResult(
        user_load=user_load,
        compute_units=instance_count,
        cost=_scale_cost(per_instance_cost, instance_count),
    )


def simulate_scenarios(
    decision: DecisionResult, *, use_graviton: bool = False
) -> list[ScenarioResult]:
    """Computes a ScenarioResult for each of SCENARIO_USER_LOADS (1K/10K/100K).

    Re-derives the compute dimensioning per scenario (task/instance count for
    ECS/EC2, invocation volume for Lambda) rather than scaling the base
    monthly cost linearly.

    :raises ValueError: if decision.compute_type has no matching sizing block
    """
    results: list[ScenarioResult] = []
    for user_load in SCENARIO_USER_LOADS:
        if decision.compute_type == "ecs":
            results.append(_simulate_ecs(decision, user_load, use_graviton=use_graviton))
        elif decision.compute_type == "lambda":
            results.append(_simulate_lambda(decision, user_load, use_graviton=use_graviton))
        elif decision.compute_type == "ec2":
            results.append(_simulate_ec2(decision, user_load))
        else:
            raise ValueError(f"Unknown compute_type: {decision.compute_type!r}")

    logger.info(
        "scenario_simulator computed %d scenarios for compute_type='%s'",
        len(results),
        decision.compute_type,
    )
    return results
