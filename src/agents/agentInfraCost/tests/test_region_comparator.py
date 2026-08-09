"""Tests for core.region_comparator (T-3.8)."""

import json
from pathlib import Path

import pytest

from core.cost_estimator import MissingPricingDataError, estimate_cost
from core.decision_engine import DecisionResult, decide_architecture
from core.region_comparator import compare_regions
from models.input_schema import RepoAnalysisInput

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_analysis(filename: str) -> RepoAnalysisInput:
    raw = json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))
    return RepoAnalysisInput.model_validate(raw)


# --------------------------------------------------------------------------
# Nominal cases
# --------------------------------------------------------------------------


def test_compare_regions_returns_one_entry_per_region() -> None:
    analysis = _load_analysis("sample_input.json")
    decision = decide_architecture(analysis)

    results = compare_regions(decision)

    regions = {r.region for r in results}
    assert regions == {"us-east-1", "eu-west-1", "ap-southeast-1"}


def test_us_east_1_matches_the_module_4_baseline_exactly() -> None:
    """us-east-1 has a 1.0 multiplier — comparing regions must never drift
    from module 4's own estimate for the region it already prices."""
    analysis = _load_analysis("sample_input.json")
    decision = decide_architecture(analysis)
    baseline = estimate_cost(decision)

    results = compare_regions(decision)
    us_east_1 = next(r for r in results if r.region == "us-east-1")

    assert us_east_1.estimated_monthly_cost.amount == baseline.amount
    assert us_east_1.estimated_monthly_cost.range_min == baseline.range_min
    assert us_east_1.estimated_monthly_cost.range_max == baseline.range_max


def test_other_regions_cost_more_than_us_east_1() -> None:
    analysis = _load_analysis("sample_input_variant_lambda_candidate.json")
    decision = decide_architecture(analysis)

    results = {r.region: r.estimated_monthly_cost.amount for r in compare_regions(decision)}

    assert results["eu-west-1"] > results["us-east-1"]
    assert results["ap-southeast-1"] > results["us-east-1"]


# --------------------------------------------------------------------------
# Limit / edge cases
# --------------------------------------------------------------------------


def test_each_region_cost_is_a_computed_range_not_a_single_figure() -> None:
    decision = DecisionResult(
        compute_type="ec2",
        sizing={"instance_type": "t3.medium"},
        score_breakdown={"ecs": 0.0, "lambda": 0.0, "ec2": 1.0},
    )
    for region_cost in compare_regions(decision):
        money = region_cost.estimated_monthly_cost
        assert money.range_min < money.amount < money.range_max


def test_region_costs_scale_with_decision_sizing_not_hardcoded() -> None:
    """Two different sizings must produce two genuinely different
    per-region costs, proving the multiplier is applied to a real
    computed baseline, not a fixed number."""
    small = DecisionResult(
        compute_type="ecs",
        sizing={"task_cpu": "256", "task_memory": "512"},
        score_breakdown={"ecs": 1.0, "lambda": 0.0, "ec2": 0.0},
    )
    large = DecisionResult(
        compute_type="ecs",
        sizing={"task_cpu": "1024", "task_memory": "2048"},
        score_breakdown={"ecs": 1.0, "lambda": 0.0, "ec2": 0.0},
    )
    small_eu = next(r for r in compare_regions(small) if r.region == "eu-west-1")
    large_eu = next(r for r in compare_regions(large) if r.region == "eu-west-1")
    assert small_eu.estimated_monthly_cost.amount < large_eu.estimated_monthly_cost.amount


# --------------------------------------------------------------------------
# Error cases
# --------------------------------------------------------------------------


def test_missing_region_multipliers_raises_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.region_comparator as region_comparator

    monkeypatch.setattr(region_comparator, "_load_pricing_data", lambda: {"_meta": {}})
    decision = DecisionResult(
        compute_type="ec2",
        sizing={"instance_type": "t3.medium"},
        score_breakdown={"ecs": 0.0, "lambda": 0.0, "ec2": 1.0},
    )
    with pytest.raises(MissingPricingDataError) as excinfo:
        compare_regions(decision)
    assert excinfo.value.key_path == "region_multipliers"
