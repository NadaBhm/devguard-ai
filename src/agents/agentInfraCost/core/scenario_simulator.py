"""Step 5 of the InfraCost pipeline: simulate cost at three load levels.

For 1,000 / 10,000 / 100,000 active users, this recomputes the actual
sizing each level needs — how many parallel ECS tasks, how many EC2
instances, how many Lambda invocations per month — and derives the cost
from that recomputed sizing. It never takes the module 4 baseline cost and
scales it by a ratio of user counts (a "rule of three"): AWS capacity is a
step function (you add whole replicas, never a fraction of one), and only
recomputing the sizing captures that correctly.

AWS publishes no formula mapping "X vCPU" or "X instance type" to a request
throughput — that number is workload-dependent and unknowable from a static
repo analysis. So, like the confidence threshold in module 1 or the size
brackets in module 2, this module leans on small, explicitly named,
documented capacity assumptions rather than inventing false precision. Two
things keep those assumptions honest instead of arbitrary: they are named
constants (not magic numbers buried in a formula), and ECS/EC2 capacity
scales with the *actual* size module 2 already chose — a bigger task or a
pricier instance is assumed to serve proportionally more users, never a
flat count blind to the sizing decision.
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

# --------------------------------------------------------------------------
# Capacity assumptions — documented, not measured. Each is expressed in the
# same unit throughout (active users per month), never mixed with
# requests/second or concurrent connections.
# --------------------------------------------------------------------------

# Lambda scales invocations automatically; only the request volume matters.
_LAMBDA_REQUESTS_PER_USER_PER_MONTH: Final[int] = 100

# ECS Fargate: capacity is assumed proportional to the vCPU allocated to
# ONE task replica (the size module 2 already chose) — a task with more
# vCPU serves proportionally more users, it is never a flat count.
_ECS_USERS_PER_VCPU: Final[int] = 500

# EC2: with no per-instance-type vCPU table to reference, on-demand hourly
# price is used as a proxy for relative compute capacity (AWS's own pricing
# scales with instance capability). Capacity is defined for one reference
# instance type and scaled by the ratio of hourly prices for any other type.
_EC2_REFERENCE_INSTANCE_TYPE: Final[str] = "t3.micro"
_EC2_USERS_PER_REFERENCE_INSTANCE: Final[int] = 400


class ScenarioResult(BaseModel):
    """The recomputed sizing and cost for one load level."""

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


_SIMULATORS = {
    "ecs": _simulate_ecs,
    "lambda": _simulate_lambda,
    "ec2": _simulate_ec2,
}


def simulate_load_scenarios(decision: DecisionResult) -> list[ScenarioResult]:
    """Simulate cost at 1K / 10K / 100K active users.

    Args:
        decision: Module 2's output — the base architecture decision.

    Returns:
        Three ``ScenarioResult``, one per load level in ``_LOAD_SCENARIOS``,
        each with its own recomputed sizing and cost — never the module 4
        baseline cost scaled by a ratio of user counts.
    """
    simulator = _SIMULATORS[decision.compute_type]
    return [simulator(decision, users) for users in _LOAD_SCENARIOS]
