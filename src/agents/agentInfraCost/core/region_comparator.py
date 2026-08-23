"""Compare estimated monthly cost across AWS regions (T-3.8, Sprint 4 addition).

Deliberately reuses cost_estimator rather than duplicating formulas — answers
"same architecture, different region", never resizing. data/aws_pricing.json
prices only us-east-1 in full, so cross-region cost approximates via the
documented ``region_multipliers`` table (documented assumption, not precision).
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
    region: str
    estimated_monthly_cost: Money


def compare_regions(
    decision: DecisionResult, context: CostEstimationContext | None = None
) -> list[RegionCost]:
    """Estimate monthly cost for the same architecture across candidate regions.
    ``context`` passes straight through to cost_estimator (baseline month by
    default). Returns one RegionCost per entry in region_multipliers, each ±20%;
    raises MissingPricingDataError when region_multipliers is absent."""
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
