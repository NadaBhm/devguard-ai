"""Step 4: estimate the monthly AWS cost.

Prices come from the live AWS Pricing API when reachable (cached ~24h), falling
back to ``data/aws_pricing.json`` otherwise. Formulas differ per compute_type;
estimates are ranges (amount ± 20%), never a single figure.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import BaseModel

from core.decision_engine import DecisionResult
from models.output_schema import Money

logger = logging.getLogger(__name__)

_PRICING_FILE: Final[Path] = Path(__file__).resolve().parent.parent / "data" / "aws_pricing.json"

_UNCERTAINTY_MARGIN: Final[float] = 0.20

# AWS Fargate convention: CPU is expressed in units where 1024 = 1 vCPU;
# memory is expressed in MiB.
_FARGATE_CPU_UNITS_PER_VCPU: Final[int] = 1024
_MB_PER_GB: Final[int] = 1024
_EC2_HOURS_PER_MONTH: Final[int] = 730

ArchFamily = Literal["x86", "arm_graviton"]


class CostEstimationError(Exception):
    pass


class MissingPricingDataError(CostEstimationError):
    """A pricing key the formula needs is absent from aws_pricing.json — never
    guessed or defaulted; update the pricing table.
    """

    def __init__(self, key_path: str) -> None:
        self.key_path = key_path
        super().__init__(f"Missing pricing data for '{key_path}' in {_PRICING_FILE.name}")


class CostEstimationContext(BaseModel):
    """Traffic/workload assumptions the formulas need but the decision doesn't
    carry (one baseline moderate month; module 5 overrides per-scenario).
    """

    avg_duration_seconds: float = 1.0
    monthly_invocations: int = 100_000
    ebs_gb: int = 20


@lru_cache(maxsize=1)
def _load_static_pricing_data() -> dict[str, Any]:
    """The offline, manually-maintained table — read from disk exactly once
    per process, always available, never a network call."""
    return json.loads(_PRICING_FILE.read_text(encoding="utf-8"))


def _load_pricing_data() -> dict[str, Any]:
    """Live prices when reachable, static table otherwise. Not lru_cache-d itself:
    the live path manages its own ~24h cache so long-running processes see refreshes
    (static underneath is cached). Never raises — failures warn and return static."""
    static = _load_static_pricing_data()
    try:
        from core.aws_pricing_client import fetch_live_pricing_data

        return fetch_live_pricing_data(fallback=static)
    except Exception:
        logger.warning("Live AWS pricing unavailable; using the static table.", exc_info=True)
        return static


def _get_pricing(data: dict[str, Any], *path: str) -> Any:
    node: Any = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise MissingPricingDataError(".".join(path))
        node = node[key]
    return node


def _select_arch_family(decision: DecisionResult) -> ArchFamily:
    """Pick which pricing tier applies. EC2 instance types encode family in a
    trailing "g" (t4g, m6g...); Fargate/Lambda carry no signal, so default to
    arm_graviton — AWS's recommended baseline for standard workloads."""
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

    per_task = (vcpu_per_hour * nb_vcpu + memory_gb_per_hour * ram_gb) * hours_per_month

    # The refiner may scale desired_count on capacity requests; base sizing keys
    # never carry it, so default to 1 (single task).
    desired_count = int(decision.sizing.get("desired_count", 1))
    return per_task * desired_count


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


def _estimate_s3(
    decision: DecisionResult, pricing: dict[str, Any], context: CostEstimationContext
) -> float:
    # S3 static hosting is effectively free at the scale this pipeline serves:
    # storage pennies + no compute hours. Modeled as a flat, sub-dollar cost.
    storage_gb = context.ebs_gb
    s3_per_gb_month = _get_pricing(pricing, "s3_standard_per_gb_month")
    return s3_per_gb_month * storage_gb


_ESTIMATORS = {
    "ecs": _estimate_ecs,
    "lambda": _estimate_lambda,
    "ec2": _estimate_ec2,
    "s3": _estimate_s3,
}


def estimate_cost(decision: DecisionResult, context: CostEstimationContext | None = None) -> Money:
    """Estimate the monthly AWS cost for a decided architecture. ``context`` holds
    traffic/workload assumptions (baseline month if omitted). Returns a ``Money``
    with amount ± 20% range — never an exact figure. Raises MissingPricingDataError
    when a needed pricing key is absent from the table."""
    context = context or CostEstimationContext()
    pricing = _load_pricing_data()
    amount = _ESTIMATORS[decision.compute_type](decision, pricing, context)

    return Money(
        amount=round(amount, 2),
        currency="USD",
        range_min=round(amount * (1 - _UNCERTAINTY_MARGIN), 2),
        range_max=round(amount * (1 + _UNCERTAINTY_MARGIN), 2),
    )
