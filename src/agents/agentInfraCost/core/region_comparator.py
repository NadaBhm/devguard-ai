"""T-3.8 (Sprint 4): compare estimated monthly cost across AWS regions.

Not part of the original 10-module pipeline (modules 1-10 cover input
validation through LLM enrichment) — added on top of it to satisfy a later
sprint requirement. Deliberately reuses module 4 (``cost_estimator``)
rather than duplicating its formulas: this module answers "same
architecture, different region", never recomputes sizing or pricing logic.

``data/aws_pricing.json`` only prices ``us-east-1`` in full. Real
region-by-region AWS pricing would require a full pricing table per region
— not available here — so cross-region cost is approximated with a small,
explicitly documented multiplier table (``region_multipliers``) applied to
the us-east-1 estimate, the same "documented assumption, never invented"
approach used throughout this agent (see the confidence threshold in
module 1, the capacity assumptions in module 5).
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel

from core.cost_estimator import (
    CostEstimationContext,
    _get_pricing,
    _load_pricing_data,
    estimate_cost,
)
from core.decision_engine import DecisionResult
from models.output_schema import Money

_PRIVATE_KEY_PREFIX: Final[str] = "_"


class RegionCost(BaseModel):
    """The estimated monthly cost of the same architecture in one region."""

    region: str
    estimated_monthly_cost: Money


def compare_regions(
    decision: DecisionResult, context: CostEstimationContext | None = None
) -> list[RegionCost]:
    """Estimate monthly cost for the same architecture across candidate regions.

    Args:
        decision: Module 2's output — the architecture decision.
        context: Traffic/workload assumptions passed straight through to
            module 4; defaults to one baseline moderate-traffic month.

    Returns:
        One ``RegionCost`` per region listed in ``region_multipliers``
        (``data/aws_pricing.json``), each with its own ±20% cost range.

    Raises:
        MissingPricingDataError: ``region_multipliers`` is absent from the
            pricing table.
    """
    baseline = estimate_cost(decision, context)
    pricing = _load_pricing_data()
    multipliers = _get_pricing(pricing, "region_multipliers")

    return [
        RegionCost(
            region=region,
            estimated_monthly_cost=Money(
                amount=round(baseline.amount * multiplier, 2),
                currency=baseline.currency,
                range_min=round(baseline.range_min * multiplier, 2),
                range_max=round(baseline.range_max * multiplier, 2),
            ),
        )
        for region, multiplier in multipliers.items()
        if not region.startswith(_PRIVATE_KEY_PREFIX)
    ]
