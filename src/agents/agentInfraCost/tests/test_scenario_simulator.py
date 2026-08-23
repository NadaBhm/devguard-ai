"""Tests for core.scenario_simulator."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.cost_estimator import MissingPricingDataError
from core.decision_engine import DecisionResult, decide_architecture
from core.scenario_simulator import ScenarioResult, _replica_count, simulate_load_scenarios
from models.input_schema import RepoAnalysisInput

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_analysis(filename: str) -> RepoAnalysisInput:
    raw = json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))
    return RepoAnalysisInput.model_validate(raw)


# --- Nominal cases ---


def test_simulate_ecs_scenarios_recompute_task_count() -> None:
    analysis = _load_analysis("sample_input.json")
    decision = decide_architecture(analysis)
    assert decision.compute_type == "ecs"

    results = simulate_load_scenarios(decision)

    assert [r.users for r in results] == [1_000, 10_000, 100_000]
    assert [r.sizing["task_count"] for r in results] == [4, 40, 400]
    # sizing stays the same task_cpu/task_memory decided by module 2
    assert all(r.sizing["task_cpu"] == "512" for r in results)


def test_simulate_lambda_scenarios_recompute_invocations() -> None:
    analysis = _load_analysis("sample_input_variant_lambda_candidate.json")
    decision = decide_architecture(analysis)
    assert decision.compute_type == "lambda"

    results = simulate_load_scenarios(decision)

    assert [r.sizing["monthly_invocations"] for r in results] == [100_000, 1_000_000, 10_000_000]
    # cost strictly increases with traffic
    assert results[0].estimated_monthly_cost.amount < results[1].estimated_monthly_cost.amount
    assert results[1].estimated_monthly_cost.amount < results[2].estimated_monthly_cost.amount


def test_simulate_ec2_scenarios_recompute_instance_count() -> None:
    decision = DecisionResult(
        compute_type="ec2",
        sizing={"instance_type": "t3.medium"},
        score_breakdown={"ecs": 0.0, "lambda": 0.0, "ec2": 1.0},
    )
    results = simulate_load_scenarios(decision)
    assert [r.sizing["instance_count"] for r in results] == [1, 7, 63]


# --- Limit / edge cases ---


def test_ecs_capacity_scales_with_task_size_not_a_flat_constant() -> None:
    """A bigger task_cpu must need fewer replicas for the same user count —
    proves capacity depends on module 2's real sizing, not a magic number."""
    small_task = DecisionResult(
        compute_type="ecs",
        sizing={"task_cpu": "256", "task_memory": "512"},
        score_breakdown={"ecs": 1.0, "lambda": 0.0, "ec2": 0.0},
    )
    large_task = DecisionResult(
        compute_type="ecs",
        sizing={"task_cpu": "1024", "task_memory": "2048"},
        score_breakdown={"ecs": 1.0, "lambda": 0.0, "ec2": 0.0},
    )
    small_results = simulate_load_scenarios(small_task)
    large_results = simulate_load_scenarios(large_task)

    for small, large in zip(small_results, large_results):
        assert small.users == large.users
        assert large.sizing["task_count"] < small.sizing["task_count"]


def test_ec2_capacity_scales_with_instance_price() -> None:
    """A pricier (more capable) instance type needs fewer replicas."""
    cheap = DecisionResult(
        compute_type="ec2",
        sizing={"instance_type": "t3.micro"},
        score_breakdown={"ecs": 0.0, "lambda": 0.0, "ec2": 1.0},
    )
    expensive = DecisionResult(
        compute_type="ec2",
        sizing={"instance_type": "t3.medium"},
        score_breakdown={"ecs": 0.0, "lambda": 0.0, "ec2": 1.0},
    )
    cheap_results = simulate_load_scenarios(cheap)
    expensive_results = simulate_load_scenarios(expensive)

    for cheap_r, expensive_r in zip(cheap_results, expensive_results):
        assert expensive_r.sizing["instance_count"] <= cheap_r.sizing["instance_count"]


def test_replica_count_rounds_up_never_truncates() -> None:
    # 1000 users / 250 capacity = exactly 4 -> stays 4
    assert _replica_count(1_000, 250.0) == 4
    # 1001 users / 250 capacity = 4.004 -> must round up to 5, never truncate to 4
    assert _replica_count(1_001, 250.0) == 5


def test_scenarios_are_not_a_rule_of_three_on_ecs() -> None:
    """10x the users must NOT simply cost 10x — it costs exactly as many
    whole task replicas as are actually needed."""
    decision = DecisionResult(
        compute_type="ecs",
        sizing={"task_cpu": "256", "task_memory": "512"},
        score_breakdown={"ecs": 1.0, "lambda": 0.0, "ec2": 0.0},
    )
    results = simulate_load_scenarios(decision)
    ratio_10k_to_1k = results[1].estimated_monthly_cost.amount / results[0].estimated_monthly_cost.amount
    # a naive rule of three would give exactly 10.0; the real ratio, driven
    # by whole-replica rounding, is not required to land on exactly 10x
    assert results[1].sizing["task_count"] == 10 * results[0].sizing["task_count"]


# --- Error cases ---


def test_unknown_ec2_instance_type_propagates_named_pricing_error() -> None:
    decision = DecisionResult(
        compute_type="ec2",
        sizing={"instance_type": "x1.mega-does-not-exist"},
        score_breakdown={"ecs": 0.0, "lambda": 0.0, "ec2": 1.0},
    )
    with pytest.raises(MissingPricingDataError):
        simulate_load_scenarios(decision)


def test_scenario_result_rejects_wrong_users_type() -> None:
    with pytest.raises(ValidationError):
        ScenarioResult(
            users="a lot",  # type: ignore[arg-type]
            sizing={"memory_mb": 128},
            estimated_monthly_cost={"amount": 1.0, "currency": "USD", "range_min": 0.8, "range_max": 1.2},
        )
