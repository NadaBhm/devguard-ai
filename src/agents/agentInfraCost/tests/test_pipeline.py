"""Tests for core.pipeline."""

import json
from pathlib import Path

import pytest

from core.input_validator import LowConfidenceError
from core.pipeline import PipelineStageError, run_pipeline
from models.output_schema import Ec2InfraCostOutput, EcsInfraCostOutput, LambdaInfraCostOutput

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_raw(filename: str) -> dict:
    return json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Nominal cases
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,expected_type",
    [
        ("sample_input.json", EcsInfraCostOutput),
        ("sample_input_variant_lambda_candidate.json", LambdaInfraCostOutput),
        ("sample_input_variant_node_ecs.json", EcsInfraCostOutput),
    ],
)
def test_run_pipeline_end_to_end(filename: str, expected_type: type) -> None:
    output = run_pipeline(_load_raw(filename))

    assert isinstance(output, expected_type)
    assert output.aws_config.estimated_monthly_cost.amount > 0
    assert output.enrichment.enrichment_source == "fallback"  # no GEMINI_API_KEY in tests


def test_run_pipeline_ecs_has_real_terraform() -> None:
    output = run_pipeline(_load_raw("sample_input.json"))
    assert "aws_ecs_cluster" in output.artifacts.terraform.files.main_tf


# --------------------------------------------------------------------------
# Limit / edge cases
# --------------------------------------------------------------------------


def test_low_confidence_propagates_unwrapped_not_as_pipeline_stage_error() -> None:
    """Module 1's own typed exceptions must reach the caller directly —
    wrapping them in PipelineStageError would hide their .job_id."""
    with pytest.raises(LowConfidenceError) as excinfo:
        run_pipeline(_load_raw("sample_input_variant_low_confidence.json"))
    assert excinfo.value.job_id == "job-variant-003"
    assert not isinstance(excinfo.value, PipelineStageError)


def test_pipeline_stage_error_names_decision_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(analysis):
        raise ValueError("simulated decision_engine crash")

    monkeypatch.setattr("core.pipeline.decide_architecture", _boom)

    with pytest.raises(PipelineStageError) as excinfo:
        run_pipeline(_load_raw("sample_input.json"))

    assert excinfo.value.stage == "decision_engine"
    assert isinstance(excinfo.value.original_exception, ValueError)


def test_pipeline_stage_error_names_cost_estimator(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(decision, context=None):
        raise RuntimeError("simulated cost_estimator crash")

    monkeypatch.setattr("core.pipeline.estimate_cost", _boom)

    with pytest.raises(PipelineStageError) as excinfo:
        run_pipeline(_load_raw("sample_input.json"))

    assert excinfo.value.stage == "cost_estimator"


# --------------------------------------------------------------------------
# Error cases
# --------------------------------------------------------------------------


def test_pipeline_stage_error_names_output_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated output_builder crash")

    monkeypatch.setattr("core.pipeline.build_output", _boom)

    with pytest.raises(PipelineStageError) as excinfo:
        run_pipeline(_load_raw("sample_input.json"))

    assert excinfo.value.stage == "output_builder"
    assert "simulated output_builder crash" in str(excinfo.value)


def test_ec2_synthetic_large_project_end_to_end() -> None:
    """No fixture naturally picks ec2 — build one from a large, container-less project."""
    raw = _load_raw("sample_input_variant_lambda_candidate.json")
    raw["job_id"] = "job-ec2-pipeline-test"
    raw["repo_metadata"]["loc"] = 50_000
    raw["repo_metadata"]["total_files"] = 500

    output = run_pipeline(raw)

    assert isinstance(output, Ec2InfraCostOutput)
