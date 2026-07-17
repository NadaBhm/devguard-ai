"""Tests for core.cost_estimator."""

import json
from pathlib import Path

import pytest

from core.cost_estimator import (
    CostEstimationContext,
    MissingPricingDataError,
    _get_pricing,
    _load_pricing_data,
    _select_arch_family,
    estimate_cost,
)
from core.decision_engine import DecisionResult, decide_architecture
from models.input_schema import RepoAnalysisInput

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_analysis(filename: str) -> RepoAnalysisInput:
    raw = json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))
    return RepoAnalysisInput.model_validate(raw)


# --------------------------------------------------------------------------
# Nominal cases
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    [
        "sample_input.json",
        "sample_input_variant_lambda_candidate.json",
        "sample_input_variant_node_ecs.json",
    ],
)
def test_estimate_cost_returns_a_sane_range(filename: str) -> None:
    analysis = _load_analysis(filename)
    decision = decide_architecture(analysis)

    money = estimate_cost(decision)

    assert money.currency == "USD"
    assert money.amount > 0
    assert money.range_min < money.amount < money.range_max


def test_estimate_cost_ec2_matches_hand_computed_formula() -> None:
    decision = DecisionResult(
        compute_type="ec2",
        sizing={"instance_type": "t3.small"},
        score_breakdown={"ecs": -3.0, "lambda": 0.0, "ec2": 5.0},
    )
    money = estimate_cost(decision, CostEstimationContext(ebs_gb=20))

    raw = 0.0208 * 730 + 0.08 * 20
    assert money.amount == round(raw, 2)
    assert money.range_min == round(raw * 0.8, 2)
    assert money.range_max == round(raw * 1.2, 2)


def test_estimate_cost_ecs_matches_hand_computed_formula() -> None:
    decision = DecisionResult(
        compute_type="ecs",
        sizing={"task_cpu": "512", "task_memory": "1024"},
        score_breakdown={"ecs": 7.0, "lambda": -5.0, "ec2": 2.0},
    )
    money = estimate_cost(decision)

    nb_vcpu = 512 / 1024
    ram_gb = 1024 / 1024
    expected = round((0.032384 * nb_vcpu + 0.003556 * ram_gb) * 730, 2)
    assert money.amount == expected


# --------------------------------------------------------------------------
# Limit / edge cases
# --------------------------------------------------------------------------


def test_arch_selection_reads_ec2_instance_family() -> None:
    graviton_decision = DecisionResult(
        compute_type="ec2",
        sizing={"instance_type": "t4g.medium"},
        score_breakdown={"ecs": 0.0, "lambda": 0.0, "ec2": 1.0},
    )
    x86_decision = DecisionResult(
        compute_type="ec2",
        sizing={"instance_type": "t3.medium"},
        score_breakdown={"ecs": 0.0, "lambda": 0.0, "ec2": 1.0},
    )
    assert _select_arch_family(graviton_decision) == "arm_graviton"
    assert _select_arch_family(x86_decision) == "x86"


def test_ecs_cost_scales_with_sizing_not_hardcoded() -> None:
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
    assert estimate_cost(small).amount < estimate_cost(large).amount


def test_lambda_cost_changes_with_traffic_context() -> None:
    decision = DecisionResult(
        compute_type="lambda",
        sizing={"memory_mb": 256},
        score_breakdown={"ecs": 0.0, "lambda": 1.0, "ec2": 0.0},
    )
    low_traffic = estimate_cost(decision, CostEstimationContext(monthly_invocations=1_000))
    high_traffic = estimate_cost(decision, CostEstimationContext(monthly_invocations=1_000_000))
    assert low_traffic.amount < high_traffic.amount


def test_pricing_data_is_loaded_once_and_cached() -> None:
    first = _load_pricing_data()
    second = _load_pricing_data()
    assert first is second


# --------------------------------------------------------------------------
# Error cases
# --------------------------------------------------------------------------


def test_unknown_ec2_instance_type_raises_named_error() -> None:
    decision = DecisionResult(
        compute_type="ec2",
        sizing={"instance_type": "x1.mega-does-not-exist"},
        score_breakdown={"ecs": 0.0, "lambda": 0.0, "ec2": 1.0},
    )
    with pytest.raises(MissingPricingDataError) as excinfo:
        estimate_cost(decision)
    assert "ec2_on_demand_hourly.x1.mega-does-not-exist" in str(excinfo.value)


def test_get_pricing_names_the_full_missing_path() -> None:
    pricing = _load_pricing_data()
    with pytest.raises(MissingPricingDataError) as excinfo:
        _get_pricing(pricing, "ecs_fargate", "quantum_computer", "vcpu_per_hour")
    assert excinfo.value.key_path == "ecs_fargate.quantum_computer.vcpu_per_hour"
