"""Step 5: simulate cost at three load levels (1K/10K/100K active users).

Recomputes the sizing each level actually needs and derives cost from it — never
baseline scaled by a ratio of users: AWS capacity is a step function (whole
replicas, never fractions). With no published vCPU->throughput formula, capacity
rests on the documented assumptions below, scaled with module 2's chosen size.
"""

from __future__ import annotations

import math
from typing import Final

from pydantic import BaseModel

from core.cost_estimator import (
    CostEstimationContext,
    estimate_cost,
    _FARGATE_CPU_UNITS_PER_VCPU,
    _get_pricing,
    _load_pricing_data,
)
from core.decision_engine import DecisionResult
from models.output_schema import Money

_LOAD_SCENARIOS: Final[tuple[int, ...]] = (1_000, 10_000, 100_000)

# Capacity assumptions — documented, not measured. Each is expressed in the
# same unit throughout (active users per month), never mixed with other units.

# Lambda scales invocations automatically; only the request volume matters.
_LAMBDA_REQUESTS_PER_USER_PER_MONTH: Final[int] = 100

# ECS Fargate: capacity assumed proportional to ONE replica's vCPU (the chosen
# size) — more vCPU serves proportionally more users, never a flat count.
_ECS_USERS_PER_VCPU: Final[int] = 500

# EC2: no per-type vCPU table exists, so on-demand hourly price proxies relative
# capacity — defined for one reference type, scaled by hourly-price ratio.
_EC2_REFERENCE_INSTANCE_TYPE: Final[str] = "t3.micro"
_EC2_USERS_PER_REFERENCE_INSTANCE: Final[int] = 400


class ScenarioResult(BaseModel):
    users: int
    sizing: dict[str, int | str]
    estimated_monthly_cost: Money


def _scale_money(money: Money, factor: int) -> Money:
    return Money(
        amount=round(money.amount * factor, 2),
        currency=money.currency,
        range_min=round(money.range_min * factor, 2),
        range_max=round(money.range_max * factor, 2),
    )


def _replica_count(users: int, capacity_per_replica: float) -> int:
    """Users needing service, divided by what one replica can serve —
    always rounded up (never truncated), and never below 1 replica."""
    return max(1, math.ceil(users / capacity_per_replica))


def _ecs_capacity_per_task(decision: DecisionResult) -> float:
    nb_vcpu = int(decision.sizing["task_cpu"]) / _FARGATE_CPU_UNITS_PER_VCPU
    return nb_vcpu * _ECS_USERS_PER_VCPU


def _ec2_capacity_per_instance(decision: DecisionResult) -> float:
    pricing = _load_pricing_data()
    instance_type = str(decision.sizing["instance_type"])
    hourly_rate = _get_pricing(pricing, "ec2_on_demand_hourly", instance_type)
    reference_rate = _get_pricing(pricing, "ec2_on_demand_hourly", _EC2_REFERENCE_INSTANCE_TYPE)
    return (hourly_rate / reference_rate) * _EC2_USERS_PER_REFERENCE_INSTANCE


def _simulate_ecs(decision: DecisionResult, users: int) -> ScenarioResult:
    task_count = _replica_count(users, _ecs_capacity_per_task(decision))
    per_task_cost = estimate_cost(decision)
    sizing = {**decision.sizing, "task_count": task_count}
    return ScenarioResult(
        users=users,
        sizing=sizing,
        estimated_monthly_cost=_scale_money(per_task_cost, task_count),
    )


def _simulate_lambda(decision: DecisionResult, users: int) -> ScenarioResult:
    monthly_invocations = users * _LAMBDA_REQUESTS_PER_USER_PER_MONTH
    cost = estimate_cost(decision, CostEstimationContext(monthly_invocations=monthly_invocations))
    sizing = {**decision.sizing, "monthly_invocations": monthly_invocations}
    return ScenarioResult(users=users, sizing=sizing, estimated_monthly_cost=cost)


def _simulate_ec2(decision: DecisionResult, users: int) -> ScenarioResult:
    instance_count = _replica_count(users, _ec2_capacity_per_instance(decision))
    per_instance_cost = estimate_cost(decision)
    sizing = {**decision.sizing, "instance_count": instance_count}
    return ScenarioResult(
        users=users,
        sizing=sizing,
        estimated_monthly_cost=_scale_money(per_instance_cost, instance_count),
    )


def _simulate_s3(decision: DecisionResult, users: int) -> ScenarioResult:
    # Static hosting doesn't scale compute with traffic — a flat cost at every
    # load level, roughly the baseline (storage dominates, not users).
    cost = estimate_cost(decision)
    sizing = {**decision.sizing}
    return ScenarioResult(users=users, sizing=sizing, estimated_monthly_cost=cost)


_SIMULATORS = {
    "ecs": _simulate_ecs,
    "lambda": _simulate_lambda,
    "ec2": _simulate_ec2,
    "s3": _simulate_s3,
}


def simulate_load_scenarios(decision: DecisionResult) -> list[ScenarioResult]:
    """Simulate cost at 1K/10K/100K active users. Returns one ScenarioResult per
    load level in ``_LOAD_SCENARIOS``, each with its own recomputed sizing and cost —
    never the baseline scaled by a ratio of user counts."""
    simulator = _SIMULATORS[decision.compute_type]
    return [simulator(decision, users) for users in _LOAD_SCENARIOS]
