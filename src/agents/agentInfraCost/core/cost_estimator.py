"""Module 4: computes the estimated monthly AWS cost for a compute decision.

Uses the static price table in data/aws_pricing.json (loaded once and cached
in memory — no network calls, no live AWS Pricing API). Every result is
returned as a range (amount ± 20%), never a single number, per the output
contract.

Pricing table structure this module depends on (see data/aws_pricing.json):
  - ecs_fargate.{x86,arm_graviton}.{vcpu_per_hour,memory_gb_per_hour}
  - ecs_fargate.hours_per_month
  - lambda.{x86,arm_graviton}.{gb_second,requests_per_million}
  - ec2_on_demand_hourly[instance_type]   (Graviton instances are separate
    keys here, e.g. "t4g.small" / "m7g.large" — there is no "arm_graviton"
    sub-block for EC2, unlike ecs_fargate/lambda. `use_graviton` is
    therefore a no-op for the EC2 formula: the instance_type string alone
    determines the price, and picking a Graviton instance type is a
    decision made upstream (decision_engine / finops_optimizer), not here.)
  - ebs_gp3_per_gb_month
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from ..models.exceptions import PricingDataError
from ..models.internal_models import CostEstimateResult, DecisionResult

logger = logging.getLogger(__name__)

_PRICING_FILE = Path(__file__).resolve().parent.parent / "data" / "aws_pricing.json"

RANGE_UNCERTAINTY_PCT = 0.20

# Baseline usage assumptions used only when a caller (typically pipeline.py,
# for the initial pre-scenario estimate) does not supply real Lambda usage
# figures. scenario_simulator.py (module 5) overrides these with figures
# derived from each 1K/10K/100K user-load tier instead of relying on them.
DEFAULT_LAMBDA_MONTHLY_INVOCATIONS = 100_000
DEFAULT_LAMBDA_AVG_DURATION_SECONDS = 0.2


@lru_cache(maxsize=1)
def load_pricing_table() -> dict[str, Any]:
    """Loads and caches data/aws_pricing.json for the lifetime of the process.

    Cached via lru_cache so the file is read from disk at most once, no
    matter how many times an estimate is requested.
    """
    with _PRICING_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def _get(table: dict[str, Any], *path: str) -> Any:
    """Walks `path` into `table`, raising PricingDataError naming the exact
    missing key instead of guessing or extrapolating a value."""
    node: Any = table
    for i, key in enumerate(path):
        if not isinstance(node, dict) or key not in node:
            raise PricingDataError(".".join(path[: i + 1]))
        node = node[key]
    return node


def _to_range(amount: float) -> CostEstimateResult:
    """Wraps a computed amount into the ± RANGE_UNCERTAINTY_PCT contract shape."""
    return CostEstimateResult(
        amount=round(amount, 2),
        currency="USD",
        range_min=round(amount * (1 - RANGE_UNCERTAINTY_PCT), 2),
        range_max=round(amount * (1 + RANGE_UNCERTAINTY_PCT), 2),
    )


def _hours_per_month(pricing: dict[str, Any]) -> float:
    return float(_get(pricing, "ecs_fargate", "hours_per_month"))


def estimate_ecs_cost(
    *,
    vcpu: float,
    memory_gb: float,
    use_graviton: bool = False,
    pricing: Optional[dict[str, Any]] = None,
) -> CostEstimateResult:
    """Estimates monthly ECS Fargate cost for one task.

    Formula: (vcpu_per_hour * vcpu + memory_gb_per_hour * memory_gb) * hours_per_month

    :param vcpu: fractional vCPU count (e.g. 0.5 for 512 Fargate CPU units)
    :param memory_gb: task memory in GB (e.g. 1.0 for 1024 MB)
    :param use_graviton: selects the arm_graviton price sub-block instead of x86
    """
    table = pricing if pricing is not None else load_pricing_table()
    arch = "arm_graviton" if use_graviton else "x86"
    vcpu_per_hour = float(_get(table, "ecs_fargate", arch, "vcpu_per_hour"))
    memory_gb_per_hour = float(_get(table, "ecs_fargate", arch, "memory_gb_per_hour"))
    hours = _hours_per_month(table)

    amount = (vcpu_per_hour * vcpu + memory_gb_per_hour * memory_gb) * hours
    return _to_range(amount)


def estimate_lambda_cost(
    *,
    memory_mb: int,
    avg_duration_seconds: float,
    monthly_invocations: int,
    use_graviton: bool = False,
    pricing: Optional[dict[str, Any]] = None,
) -> CostEstimateResult:
    """Estimates monthly AWS Lambda cost.

    Formula: (gb_second * memory_gb * avg_duration_seconds * monthly_invocations)
              + (requests_per_million * monthly_invocations / 1_000_000)

    The AWS Lambda free tier (present in the pricing table under
    lambda.free_tier) is intentionally NOT subtracted here: this function
    computes gross compute + request cost, matching the formula given in
    the module spec. Net-of-free-tier cost is a finops_optimizer concern,
    not a cost_estimator one.

    :param memory_mb: configured Lambda memory in MB
    :param avg_duration_seconds: assumed average execution time per invocation
    :param monthly_invocations: assumed number of invocations per month
    :param use_graviton: selects the arm_graviton price sub-block instead of x86
    """
    table = pricing if pricing is not None else load_pricing_table()
    arch = "arm_graviton" if use_graviton else "x86"
    gb_second = float(_get(table, "lambda", arch, "gb_second"))
    requests_per_million = float(_get(table, "lambda", arch, "requests_per_million"))

    memory_gb = memory_mb / 1024.0
    compute_cost = gb_second * memory_gb * avg_duration_seconds * monthly_invocations
    request_cost = requests_per_million * monthly_invocations / 1_000_000
    amount = compute_cost + request_cost
    return _to_range(amount)


def estimate_ec2_cost(
    *,
    instance_type: str,
    instance_count: int = 1,
    ebs_gb: Optional[float] = None,
    pricing: Optional[dict[str, Any]] = None,
) -> CostEstimateResult:
    """Estimates monthly EC2 On-Demand cost.

    Formula: ec2_on_demand_hourly[instance_type] * hours_per_month * instance_count
              + (ebs_gp3_per_gb_month * ebs_gb * instance_count if ebs_gb is given)

    :param instance_type: must exist as a key in ec2_on_demand_hourly;
        raises PricingDataError naming the instance type if not covered.
    :param instance_count: number of identical instances (fleet size)
    :param ebs_gb: optional attached EBS gp3 volume size per instance, in GB
    """
    table = pricing if pricing is not None else load_pricing_table()
    hourly_rate = _get(table, "ec2_on_demand_hourly", instance_type)
    hours = _hours_per_month(table)

    amount = float(hourly_rate) * hours * instance_count
    if ebs_gb is not None:
        ebs_rate = float(_get(table, "ebs_gp3_per_gb_month"))
        amount += ebs_rate * ebs_gb * instance_count

    return _to_range(amount)


def estimate_cost(
    decision: DecisionResult,
    *,
    use_graviton: bool = False,
    lambda_avg_duration_seconds: Optional[float] = None,
    lambda_monthly_invocations: Optional[int] = None,
    ec2_ebs_gb: Optional[float] = None,
) -> CostEstimateResult:
    """Routes to the matching formula for decision.compute_type.

    This is the entry point pipeline.py calls with decision_engine's output.
    scenario_simulator.py (module 5) instead calls estimate_ecs_cost /
    estimate_lambda_cost / estimate_ec2_cost directly with per-scenario
    sizing and usage figures, since it recomputes sizing per load tier
    rather than reusing decision_engine's single baseline decision.

    :raises ValueError: if decision.compute_type has no matching sizing block
        (indicates a bug upstream in decision_engine)
    :raises PricingDataError: if a required price is missing from the table
    """
    if decision.compute_type == "ecs":
        if decision.ecs is None:
            raise ValueError("DecisionResult.compute_type='ecs' but decision.ecs is None")
        vcpu = decision.ecs.task_cpu / 1024.0
        memory_gb = decision.ecs.task_memory / 1024.0
        return estimate_ecs_cost(vcpu=vcpu, memory_gb=memory_gb, use_graviton=use_graviton)

    if decision.compute_type == "lambda":
        if decision.lambda_ is None:
            raise ValueError("DecisionResult.compute_type='lambda' but decision.lambda_ is None")
        return estimate_lambda_cost(
            memory_mb=decision.lambda_.memory_mb,
            avg_duration_seconds=(
                lambda_avg_duration_seconds
                if lambda_avg_duration_seconds is not None
                else DEFAULT_LAMBDA_AVG_DURATION_SECONDS
            ),
            monthly_invocations=(
                lambda_monthly_invocations
                if lambda_monthly_invocations is not None
                else DEFAULT_LAMBDA_MONTHLY_INVOCATIONS
            ),
            use_graviton=use_graviton,
        )

    if decision.compute_type == "ec2":
        if decision.ec2 is None:
            raise ValueError("DecisionResult.compute_type='ec2' but decision.ec2 is None")
        return estimate_ec2_cost(
            instance_type=decision.ec2.instance_type,
            instance_count=decision.ec2.instance_count,
            ebs_gb=ec2_ebs_gb,
        )

    raise ValueError(f"Unknown compute_type: {decision.compute_type!r}")
