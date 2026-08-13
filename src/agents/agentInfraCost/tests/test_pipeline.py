"""Tests for core.pipeline."""

import json
from pathlib import Path

import pytest

from core.input_validator import LowConfidenceError
from core.pipeline import PipelineStageError, run_pipeline, run_pipeline_with_context
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


def test_run_pipeline_with_context_returns_same_output_as_run_pipeline() -> None:
    """The two entry points must agree — run_pipeline_with_context is a
    richer view into the same pipeline, never a second implementation."""
    raw = _load_raw("sample_input.json")

    plain_output = run_pipeline(raw)
    context = run_pipeline_with_context(raw)

    assert context.output == plain_output
    assert context.decision.compute_type == plain_output.compute_type


def test_run_pipeline_with_context_exposes_decision_and_finops() -> None:
    context = run_pipeline_with_context(_load_raw("sample_input.json"))

    assert context.decision.compute_type == "ecs"
    assert context.finops.recommended is not None
    assert context.output.aws_config.estimated_monthly_cost.amount > 0
    assert context.output.enrichment.enrichment_source == "fallback"  # no GEMINI_API_KEY in tests


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

    monkeypatch.setattr("core.pipeline.decide_architecture_via_llm", _boom)

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


# --------------------------------------------------------------------------
# Gate-2 regeneration: whole-repo context
# --------------------------------------------------------------------------


def test_gate2_repo_digest_is_computed_and_reaches_architecture_advisor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When Gate 2 requests regeneration, the pipeline digests the re-cloned
    repo and feeds the result into the OpenRouter architecture prompt."""
    raw = _load_raw("sample_input.json")
    raw["user_feedback"] = "make it cheaper"
    raw["repo_path"] = str(tmp_path)

    captured: dict = {}

    def _fake_ingest(repo_path, job_id, *, commit_sha=None):
        captured["path"] = str(repo_path)
        return "port 8000, health check /health, FastAPI + Postgres"

    def _fake_arch_llm(*args, **kwargs):
        captured["prompt"] = kwargs.get("prompt", args[0] if args else None)
        return json.dumps({"compute_type": "ecs", "reasoning": "repo facts say so"})

    monkeypatch.setattr("core.pipeline.ingest_repo", _fake_ingest)
    monkeypatch.setattr("core.llm_architecture_advisor.call_llm", _fake_arch_llm)

    run_pipeline(raw)

    assert captured["path"] == str(tmp_path)
    assert "=== CONTEXTE DU DÉPÔT" in captured["prompt"]
    assert "port 8000" in captured["prompt"]


def test_gate2_without_repo_path_never_digests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No re-cloned repo path, no digest call — regression runs untouched."""
    raw = _load_raw("sample_input.json")
    raw["user_feedback"] = "cheaper please"

    called = {"n": 0}

    def _fake_ingest(*args, **kwargs):
        called["n"] += 1
        return "never"

    monkeypatch.setattr("core.pipeline.ingest_repo", _fake_ingest)

    run_pipeline(raw)

    assert called["n"] == 0
