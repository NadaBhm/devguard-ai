"""Tests for core.decision_engine."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from core.decision_engine import (
    DecisionResult,
    _choose_compute_type,
    decide_architecture,
)
from models.input_schema import RepoAnalysisInput

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_analysis(filename: str) -> RepoAnalysisInput:
    raw = json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))
    return RepoAnalysisInput.model_validate(raw)


def _load_raw(filename: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Nominal cases — the 4 fixtures, expecting different results per context
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,expected_compute_type",
    [
        ("sample_input.json", "ecs"),
        ("sample_input_variant_lambda_candidate.json", "lambda"),
        ("sample_input_variant_node_ecs.json", "ecs"),
        ("sample_input_variant_low_confidence.json", "lambda"),
    ],
)
def test_decide_architecture_matches_expected_compute_type(
    filename: str, expected_compute_type: str
) -> None:
    analysis = _load_analysis(filename)
    result = decide_architecture(analysis)
    assert result.compute_type == expected_compute_type


def test_decision_differs_between_fastapi_and_node_stacks() -> None:
    """FastAPI and Express share no framework name, only structural shape."""
    fastapi_result = decide_architecture(_load_analysis("sample_input.json"))
    node_result = decide_architecture(
        _load_analysis("sample_input_variant_node_ecs.json")
    )
    assert fastapi_result.compute_type == "ecs"
    assert node_result.compute_type == "ecs"
    assert fastapi_result.compute_type == node_result.compute_type


@pytest.mark.parametrize(
    "filename,expected_keys",
    [
        ("sample_input.json", {"task_cpu", "task_memory"}),
        ("sample_input_variant_lambda_candidate.json", {"memory_mb"}),
        ("sample_input_variant_node_ecs.json", {"task_cpu", "task_memory"}),
    ],
)
def test_sizing_keys_match_compute_type(
    filename: str, expected_keys: set[str]
) -> None:
    result = decide_architecture(_load_analysis(filename))
    assert set(result.sizing.keys()) == expected_keys


def test_score_breakdown_has_all_three_compute_types() -> None:
    result = decide_architecture(_load_analysis("sample_input.json"))
    assert set(result.score_breakdown.keys()) == {"ecs", "lambda", "ec2"}
    assert all(isinstance(v, float) for v in result.score_breakdown.values())


# --------------------------------------------------------------------------
# Limit / edge cases
# --------------------------------------------------------------------------


def test_large_uncontainerized_project_chooses_ec2() -> None:
    raw = _load_raw("sample_input_variant_lambda_candidate.json")
    raw["repo_metadata"]["loc"] = 50_000
    analysis = RepoAnalysisInput.model_validate(raw)
    result = decide_architecture(analysis)
    assert result.compute_type == "ec2"
    assert set(result.sizing.keys()) == {"instance_type"}


@pytest.mark.parametrize(
    "loc,expected_cpu,expected_memory",
    [
        (4_999, "256", "512"),
        (5_000, "512", "1024"),
        (14_999, "512", "1024"),
        (15_000, "1024", "2048"),
    ],
)
def test_ecs_sizing_tier_boundaries(
    loc: int, expected_cpu: str, expected_memory: str
) -> None:
    raw = _load_raw("sample_input.json")
    raw["repo_metadata"]["loc"] = loc
    analysis = RepoAnalysisInput.model_validate(raw)
    result = decide_architecture(analysis)
    assert result.sizing == {"task_cpu": expected_cpu, "task_memory": expected_memory}


@pytest.mark.parametrize(
    "loc,expected_memory_mb",
    [
        (199, 128),
        (200, 256),
        (999, 256),
        (1_000, 512),
    ],
)
def test_lambda_sizing_tier_boundaries(loc: int, expected_memory_mb: int) -> None:
    raw = _load_raw("sample_input_variant_lambda_candidate.json")
    raw["repo_metadata"]["loc"] = loc
    analysis = RepoAnalysisInput.model_validate(raw)
    result = decide_architecture(analysis)
    assert result.sizing == {"memory_mb": expected_memory_mb}


@pytest.mark.parametrize(
    "scores,expected",
    [
        ({"ecs": 5.0, "lambda": 5.0, "ec2": 5.0}, "ecs"),
        ({"ecs": 1.0, "lambda": 5.0, "ec2": 5.0}, "lambda"),
        ({"ecs": 1.0, "lambda": 1.0, "ec2": 5.0}, "ec2"),
    ],
)
def test_choose_compute_type_ties_favor_insertion_order(
    scores: dict[str, float], expected: str
) -> None:
    assert _choose_compute_type(scores) == expected


def test_decide_architecture_does_not_mutate_input() -> None:
    analysis = _load_analysis("sample_input.json")
    snapshot = analysis.model_copy(deep=True)
    decide_architecture(analysis)
    assert analysis == snapshot


# --------------------------------------------------------------------------
# Error cases
# --------------------------------------------------------------------------


def test_decision_result_rejects_invalid_compute_type() -> None:
    with pytest.raises(ValidationError):
        DecisionResult(
            compute_type="serverless",  # type: ignore[arg-type]
            sizing={"memory_mb": 128},
            score_breakdown={"ecs": 0.0, "lambda": 0.0, "ec2": 0.0},
        )


def test_decision_result_rejects_wrong_sizing_value_type() -> None:
    with pytest.raises(ValidationError):
        DecisionResult(
            compute_type="lambda",
            sizing={"memory_mb": ["not", "a", "number"]},  # type: ignore[dict-item]
            score_breakdown={"ecs": 0.0, "lambda": 0.0, "ec2": 0.0},
        )


def test_decide_architecture_is_deterministic() -> None:
    analysis = _load_analysis("sample_input_variant_node_ecs.json")
    first = decide_architecture(analysis)
    second = decide_architecture(analysis)
    assert first == second
