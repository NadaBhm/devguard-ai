"""Step 4 of the InfraCost pipeline: estimate the monthly AWS cost.

Reads static, offline pricing data from ``data/aws_pricing.json`` (loaded
once and cached — no network call, ever) and applies a different formula
per ``compute_type``. A stack detection and a sizing decision can only ever
produce an estimate, never an exact bill, so this always returns a range
(``amount`` ± 20%), never a single figure.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel

from core.decision_engine import DecisionResult
from models.output_schema import Money

_PRICING_FILE: Final[Path] = Path(__file__).resolve().parent.parent / "data" / "aws_pricing.json"

_UNCERTAINTY_MARGIN: Final[float] = 0.20

# AWS Fargate convention: CPU is expressed in units where 1024 = 1 vCPU;
# memory is expressed in MiB.
_FARGATE_CPU_UNITS_PER_VCPU: Final[int] = 1024
_MB_PER_GB: Final[int] = 1024
_EC2_HOURS_PER_MONTH: Final[int] = 730

ArchFamily = Literal["x86", "arm_graviton"]


class CostEstimationError(Exception):
    """Base class for cost_estimator failures."""


class MissingPricingDataError(CostEstimationError):
    """A pricing key the formula needs is absent from aws_pricing.json.

    Never guessed or defaulted — the caller must update the pricing table.
    """

    def __init__(self, key_path: str) -> None:
        self.key_path = key_path
        super().__init__(f"Missing pricing data for '{key_path}' in {_PRICING_FILE.name}")


class CostEstimationContext(BaseModel):
    """Traffic/workload assumptions the pricing formulas need but the
    architecture decision doesn't carry.

    These describe one baseline, moderate-traffic month. Module 5
    (``scenario_simulator``) will override them with real per-scenario
    numbers (1K / 10K / 100K users) rather than relying on these defaults.
    """

    avg_duration_seconds: float = 1.0
    monthly_invocations: int = 100_000
    ebs_gb: int = 20


@lru_cache(maxsize=1)
def _load_pricing_data() -> dict[str, Any]:
    """Load and cache aws_pricing.json — read from disk exactly once per process."""
    return json.loads(_PRICING_FILE.read_text(encoding="utf-8"))


def _get_pricing(data: dict[str, Any], *path: str) -> Any:
    """Walk `data` through `path`; raise MissingPricingDataError naming the
    full dotted path if any key along the way is absent."""
    node: Any = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise MissingPricingDataError(".".join(path))
        node = node[key]
    return node


def _select_arch_family(decision: DecisionResult) -> ArchFamily:
    """Pick which pricing tier applies.

    For EC2, the instance type name already encodes the family — AWS's own
    naming convention marks Graviton/ARM instances with a trailing "g" on
    the generation number (t4g, m6g, m7g, ...). For ECS Fargate and Lambda,
    the decision carries no such signal, so we default to arm_graviton:
    AWS's recommended baseline for any standard containerized or
    interpreted workload, absent a known reason not to use it.
    """
    if decision.compute_type == "ec2":
        instance_family = str(decision.sizing["instance_type"]).split(".")[0]
        return "arm_graviton" if instance_family.endswith("g") else "x86"
    return "arm_graviton"


def _estimate_ecs(
    decision: DecisionResult, pricing: dict[str, Any], context: CostEstimationContext
) -> float:
    arch = _select_arch_family(decision)
    vcpu_per_hour = _get_pricing(pricing, "ecs_fargate", arch, "vcpu_per_hour")
    memory_gb_per_hour = _get_pricing(pricing, "ecs_fargate", arch, "memory_gb_per_hour")
    hours_per_month = _get_pricing(pricing, "ecs_fargate", "hours_per_month")

    nb_vcpu = int(decision.sizing["task_cpu"]) / _FARGATE_CPU_UNITS_PER_VCPU
    ram_gb = int(decision.sizing["task_memory"]) / _MB_PER_GB

    return (vcpu_per_hour * nb_vcpu + memory_gb_per_hour * ram_gb) * hours_per_month


def _estimate_lambda(
    decision: DecisionResult, pricing: dict[str, Any], context: CostEstimationContext
) -> float:
    arch = _select_arch_family(decision)
    gb_second = _get_pricing(pricing, "lambda", arch, "gb_second")
    requests_per_million = _get_pricing(pricing, "lambda", arch, "requests_per_million")

    memory_gb = int(decision.sizing["memory_mb"]) / _MB_PER_GB
    compute_cost = gb_second * memory_gb * context.avg_duration_seconds * context.monthly_invocations
    request_cost = requests_per_million * context.monthly_invocations / 1_000_000
    return compute_cost + request_cost


def _estimate_ec2(
    decision: DecisionResult, pricing: dict[str, Any], context: CostEstimationContext
) -> float:
    instance_type = str(decision.sizing["instance_type"])
    hourly_rate = _get_pricing(pricing, "ec2_on_demand_hourly", instance_type)
    ebs_per_gb_month = _get_pricing(pricing, "ebs_gp3_per_gb_month")

    compute_cost = hourly_rate * _EC2_HOURS_PER_MONTH
    ebs_cost = ebs_per_gb_month * context.ebs_gb
    return compute_cost + ebs_cost


_ESTIMATORS = {
    "ecs": _estimate_ecs,
    "lambda": _estimate_lambda,
    "ec2": _estimate_ec2,
}


def estimate_cost(decision: DecisionResult, context: CostEstimationContext | None = None) -> Money:
    """Estimate the monthly AWS cost for a decided architecture.

    Args:
        decision: Module 2's output — names ``compute_type`` and its sizing.
        context: Traffic/workload assumptions; defaults to one baseline
            moderate-traffic month if omitted.

    Returns:
        A ``Money`` with ``amount`` plus a ±20% ``range_min``/``range_max``
        — never a single exact figure.

    Raises:
        MissingPricingDataError: a pricing key the formula needs is absent
            from ``data/aws_pricing.json``.
    """
    context = context or CostEstimationContext()
    pricing = _load_pricing_data()
    amount = _ESTIMATORS[decision.compute_type](decision, pricing, context)

    return Money(
        amount=round(amount, 2),
        currency="USD",
        range_min=round(amount * (1 - _UNCERTAINTY_MARGIN), 2),
        range_max=round(amount * (1 + _UNCERTAINTY_MARGIN), 2),
    )
