"""Tests for core.orchestrator_adapter.

No OPENROUTER_API_KEY / GEMINI_API_KEY is set in this test environment, so
run_pipeline_with_context always takes the deterministic path — these tests
never make a real network call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.orchestrator_adapter import _ARCHITECTURE_RECOMMENDATION, to_orchestrator_result
from core.pipeline import run_pipeline_with_context
from core.region_comparator import compare_regions
from core.scenario_simulator import simulate_load_scenarios

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_raw(filename: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Nominal cases
# --------------------------------------------------------------------------


def test_to_orchestrator_result_maps_all_seven_fields_for_ecs() -> None:
    context = run_pipeline_with_context(_load_raw("sample_input.json"))

    result = to_orchestrator_result(context.output, context.decision, context.finops)

    assert result["architecture_recommendation"] == "ecs_fargate"
    assert result["justification"] == context.output.enrichment.architecture_explanation
    assert set(result["generated_terraform"].keys()) == {"files", "variables"}
    assert result["cost_estimate"]["amount"] == context.output.aws_config.estimated_monthly_cost.amount
    assert len(result["load_scenarios"]) == 3
    assert len(result["optimizations"]) == 1 + len(context.finops.discarded)
    assert len(result["region_comparison"]) >= 1


def test_to_orchestrator_result_maps_lambda_compute_type() -> None:
    context = run_pipeline_with_context(_load_raw("sample_input_variant_lambda_candidate.json"))

    result = to_orchestrator_result(context.output, context.decision, context.finops)

    assert result["architecture_recommendation"] == "lambda"


# --------------------------------------------------------------------------
# Limit / edge cases
# --------------------------------------------------------------------------


def test_exactly_one_optimization_is_marked_selected() -> None:
    context = run_pipeline_with_context(_load_raw("sample_input.json"))

    result = to_orchestrator_result(context.output, context.decision, context.finops)

    selected = [option for option in result["optimizations"] if option["selected"]]
    assert len(selected) == 1
    assert selected[0]["name"] == context.finops.recommended.name


def test_load_scenarios_match_scenario_simulator_directly() -> None:
    context = run_pipeline_with_context(_load_raw("sample_input.json"))
    expected = [scenario.model_dump() for scenario in simulate_load_scenarios(context.decision)]

    result = to_orchestrator_result(context.output, context.decision, context.finops)

    assert result["load_scenarios"] == expected


def test_region_comparison_matches_region_comparator_directly() -> None:
    context = run_pipeline_with_context(_load_raw("sample_input.json"))
    expected = [region.model_dump() for region in compare_regions(context.decision)]

    result = to_orchestrator_result(context.output, context.decision, context.finops)

    assert result["region_comparison"] == expected


# --------------------------------------------------------------------------
# Error / mapping-table cases
# --------------------------------------------------------------------------


def test_architecture_recommendation_mapping_covers_exactly_the_known_compute_types() -> None:
    """Proves the mapping table can't silently drift from decision_engine's
    ComputeType — every compute_type this agent can ever produce has an
    entry, and nothing extra is defined that could hide a typo."""
    assert set(_ARCHITECTURE_RECOMMENDATION.keys()) == {"ecs", "lambda", "ec2", "s3"}
    assert _ARCHITECTURE_RECOMMENDATION["ecs"] == "ecs_fargate"
    assert _ARCHITECTURE_RECOMMENDATION["lambda"] == "lambda"
    assert _ARCHITECTURE_RECOMMENDATION["ec2"] == "ec2"
    assert _ARCHITECTURE_RECOMMENDATION["s3"] == "s3"
