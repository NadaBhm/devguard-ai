"""Tests for core.output_builder."""

import json
import logging
from pathlib import Path

import pytest

from core.cost_estimator import estimate_cost
from core.decision_engine import DecisionResult, decide_architecture
from core.output_builder import resolve_docker_artifacts, build_output
from core.terraform_generator import TerraformContext, generate_terraform
from models.input_schema import RepoAnalysisInput
from models.output_schema import (
    Ec2InfraCostOutput,
    EcsInfraCostOutput,
    Enrichment,
    LambdaInfraCostOutput,
    Money,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"

_FALLBACK_ENRICHMENT = Enrichment(
    architecture_explanation="n/a",
    cost_summary="n/a",
    finops_justification="n/a",
    enrichment_source="fallback",
)


def _load_analysis(filename: str) -> RepoAnalysisInput:
    raw = json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))
    return RepoAnalysisInput.model_validate(raw)


def _build(analysis: RepoAnalysisInput, decision: DecisionResult, **kwargs):
    context = TerraformContext(job_id=analysis.job_id, docker_image="devguard-app:sha-abc1234")
    terraform_files = generate_terraform(decision, context)
    cost = estimate_cost(decision)
    return build_output(analysis, decision, terraform_files, cost, _FALLBACK_ENRICHMENT, **kwargs)


# --------------------------------------------------------------------------
# Nominal cases
# --------------------------------------------------------------------------


def test_ecs_fixture_builds_ecs_variant_with_nulls_elsewhere() -> None:
    analysis = _load_analysis("sample_input.json")
    decision = decide_architecture(analysis)

    output = _build(analysis, decision)

    assert isinstance(output, EcsInfraCostOutput)
    assert output.aws_config.ecs is not None
    assert output.aws_config.lambda_ is None
    assert output.aws_config.ec2 is None
    assert output.deployment_config.ecs is not None
    assert output.deployment_config.lambda_ is None
    assert output.deployment_config.ec2 is None
    assert output.artifacts.dockerfile is not None
    assert output.artifacts.docker_image.tag == "sha-a1b2c3d"


def test_lambda_fixture_builds_lambda_variant_without_docker() -> None:
    analysis = _load_analysis("sample_input_variant_lambda_candidate.json")
    decision = decide_architecture(analysis)

    output = _build(analysis, decision)

    assert isinstance(output, LambdaInfraCostOutput)
    assert output.aws_config.lambda_ is not None
    assert output.aws_config.ecs is None
    assert output.aws_config.ec2 is None
    assert output.artifacts.dockerfile is None
    assert output.artifacts.docker_image is None


def test_ec2_decision_builds_ec2_variant() -> None:
    analysis = _load_analysis("sample_input_variant_lambda_candidate.json")
    decision = DecisionResult(
        compute_type="ec2",
        sizing={"instance_type": "t3.medium"},
        score_breakdown={"ecs": 0.0, "lambda": 0.0, "ec2": 1.0},
    )
    output = _build(analysis, decision)

    assert isinstance(output, Ec2InfraCostOutput)
    assert output.aws_config.ec2 is not None
    assert output.aws_config.ecs is None
    assert output.aws_config.lambda_ is None


def test_approval_status_defaults_to_pending_and_can_be_overridden() -> None:
    analysis = _load_analysis("sample_input.json")
    decision = decide_architecture(analysis)

    default_output = _build(analysis, decision)
    approved_output = _build(analysis, decision, approval_status="approved")

    assert default_output.approval.status == "pending"
    assert approved_output.approval.status == "approved"


# --------------------------------------------------------------------------
# Limit / edge cases
# --------------------------------------------------------------------------


def test_docker_tag_falls_back_to_latest_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    raw = json.loads((FIXTURES_DIR / "sample_input.json").read_text(encoding="utf-8"))
    raw["repo_metadata"]["commit_sha"] = ""
    analysis = RepoAnalysisInput.model_validate(raw)
    decision = decide_architecture(analysis)

    with caplog.at_level(logging.WARNING, logger="core.output_builder"):
        dockerfile, docker_image = resolve_docker_artifacts(analysis, decision)

    assert docker_image.tag == "latest"
    assert dockerfile is not None
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("commit_sha" in w for w in warnings)


def test_lambda_without_container_has_no_dockerfile() -> None:
    analysis = _load_analysis("sample_input_variant_lambda_candidate.json")
    assert analysis.stack_detection.container.detected is False
    decision = decide_architecture(analysis)

    dockerfile, docker_image = resolve_docker_artifacts(analysis, decision)

    assert dockerfile is None
    assert docker_image is None


def test_output_is_json_serializable_with_correct_alias_keys() -> None:
    analysis = _load_analysis("sample_input.json")
    decision = decide_architecture(analysis)
    output = _build(analysis, decision)

    dumped = output.model_dump(by_alias=True)

    assert set(dumped["aws_config"].keys()) == {"region", "estimated_monthly_cost", "ecs", "lambda", "ec2"}
    assert set(dumped["deployment_config"].keys()) == {"ecs", "lambda", "ec2"}


# --------------------------------------------------------------------------
# Error cases
# --------------------------------------------------------------------------


def test_unknown_compute_type_raises_key_error() -> None:
    """DecisionResult's own Literal type prevents this in practice, but the
    dispatch dict must fail loudly, not silently, if it ever happened."""
    analysis = _load_analysis("sample_input.json")
    decision = decide_architecture(analysis)
    decision.compute_type = "serverless-mystery"  # type: ignore[assignment]
    context = TerraformContext(job_id=analysis.job_id)
    terraform_files = generate_terraform(
        DecisionResult(compute_type="ecs", sizing=decision.sizing, score_breakdown=decision.score_breakdown),
        context,
    )
    cost = Money(amount=1.0, currency="USD", range_min=0.8, range_max=1.2)

    with pytest.raises(KeyError):
        build_output(analysis, decision, terraform_files, cost, _FALLBACK_ENRICHMENT)


def test_missing_required_sizing_key_raises_key_error() -> None:
    analysis = _load_analysis("sample_input.json")
    decision = DecisionResult(
        compute_type="ecs",
        sizing={"task_cpu": "512"},  # task_memory missing
        score_breakdown={"ecs": 1.0, "lambda": 0.0, "ec2": 0.0},
    )
    context = TerraformContext(job_id=analysis.job_id)
    with pytest.raises(KeyError):
        generate_terraform(decision, context)
