"""Tests for core.output_builder."""

import json
import logging
from pathlib import Path

import pytest
from core.cost_estimator import estimate_cost
from core.decision_engine import DecisionResult, decide_architecture
from core.output_builder import build_output, resolve_docker_artifacts
from core.terraform_generator import TerraformContext, generate_terraform
from models.input_schema import ContainerInfo, RepoAnalysisInput, StackDetection
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


def test_deployment_config_health_check_tracks_refined_terraform() -> None:
    """DeployOps's post-deploy health check must hit the same port/path the
    ALB target group ships with. When the refiner rewrites the target group
    (e.g. to the app's real 3000 + /api/health), deployment_config must
    follow — not stay stuck on the template's 8080 + /health."""
    from models.output_schema import TerraformFiles

    analysis = _load_analysis("sample_input.json")
    decision = decide_architecture(analysis)
    context = TerraformContext(job_id=analysis.job_id, docker_image="devguard-app:sha-abc1234")
    base_tf = generate_terraform(decision, context)
    refined_main = base_tf.main_tf.replace(
        "port        = 8080", "port        = 3000"
    ).replace(
        'path                = "/"', 'path                = "/api/health"'
    )
    assert refined_main != base_tf.main_tf
    refined = TerraformFiles(
        main_tf=refined_main,
        variables_tf=base_tf.variables_tf,
        outputs_tf=base_tf.outputs_tf,
    )
    cost = estimate_cost(decision)
    output = build_output(
        analysis, decision, refined, cost, _FALLBACK_ENRICHMENT
    )

    ecs_deploy = output.deployment_config.ecs
    assert ecs_deploy is not None
    assert ecs_deploy.health_check_port == 3000
    assert ecs_deploy.health_check_path == "/api/health"


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


def test_ec2_health_check_reads_refined_terraform() -> None:
    """The EC2 deployment health check must come from the actual rendered
    (possibly refiner-edited) Terraform — the constants (8080 + "/health")
    are only the template default. Reproduces the ECS contract: DeployOps
    probes what the instance really serves, not a hardcoded port/path."""
    analysis = _load_analysis("sample_input_variant_lambda_candidate.json")
    decision = DecisionResult(
        compute_type="ec2",
        sizing={"instance_type": "t3.medium"},
        score_breakdown={"ecs": 0.0, "lambda": 0.0, "ec2": 1.0},
    )
    context = TerraformContext(job_id=analysis.job_id, docker_image="devguard-app:sha-abc1234")
    terraform_files = generate_terraform(decision, context)

    # The Gate-2 refiner legitimately rewrites the rendered locals block to
    # match the app (e.g. a Node server on 3000 with /api/health).
    refined = terraform_files.main_tf.replace(
        'health_check_path = "/"', 'health_check_path = "/api/health"'
    ).replace("health_check_port = 8080", "health_check_port = 3000")
    terraform_files.main_tf = refined

    cost = estimate_cost(decision)
    output = build_output(
        analysis, decision, terraform_files, cost, _FALLBACK_ENRICHMENT
    )

    assert output.deployment_config.ec2 is not None
    assert output.deployment_config.ec2.health_check_port == 3000
    assert output.deployment_config.ec2.health_check_path == "/api/health"


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
        docker_images = resolve_docker_artifacts(analysis, decision)

    assert docker_images[0].tag == "latest"
    assert docker_images[0].dockerfile is not None
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("commit_sha" in w for w in warnings)


def _load_multi_container_analysis() -> RepoAnalysisInput:
    analysis = _load_analysis("sample_input.json")
    stack = StackDetection.model_validate({
        **analysis.stack_detection.model_dump(),
        "containers": [
            {"detected": True, "base_image": "python:3.12-slim",
             "dockerfile_path": "backend/Dockerfile",
             "dockerfile_content": "FROM python:3.12-slim\nEXPOSE 8000\n",
             "compose_detected": False},
            {"detected": True, "base_image": "nginx:1.27",
             "dockerfile_path": "frontend/Dockerfile",
             "dockerfile_content": "FROM nginx:1.27\nEXPOSE 80\n",
             "compose_detected": False},
        ],
    })
    return analysis.model_copy(update={"stack_detection": stack})


def test_multi_container_resolves_one_image_per_dockerfile() -> None:
    analysis = _load_multi_container_analysis()
    decision = decide_architecture(analysis)

    docker_images = resolve_docker_artifacts(analysis, decision)

    assert [img.name for img in docker_images] == ["devguard-app", "devguard-app-frontend"]
    assert [img.context for img in docker_images] == ["backend", "frontend"]
    assert docker_images[0].dockerfile == "FROM python:3.12-slim\nEXPOSE 8000\n"
    assert docker_images[1].dockerfile == "FROM nginx:1.27\nEXPOSE 80\n"
    assert docker_images[0].tag == docker_images[1].tag


def test_multi_container_build_output_plural_and_singular_alias() -> None:
    analysis = _load_multi_container_analysis()
    decision = decide_architecture(analysis)

    output = _build(analysis, decision)

    assert len(output.artifacts.docker_images) == 2
    # Singular alias mirrors the first entry for legacy consumers.
    assert output.artifacts.docker_image.name == "devguard-app"
    assert output.artifacts.dockerfile == "FROM python:3.12-slim\nEXPOSE 8000\n"


def test_lambda_without_container_has_no_dockerfile() -> None:
    analysis = _load_analysis("sample_input_variant_lambda_candidate.json")
    assert analysis.stack_detection.container.detected is False
    decision = decide_architecture(analysis)

    docker_images = resolve_docker_artifacts(analysis, decision)

    assert docker_images == []


def test_output_is_json_serializable_with_correct_alias_keys() -> None:
    analysis = _load_analysis("sample_input.json")
    decision = decide_architecture(analysis)
    output = _build(analysis, decision)

    dumped = output.model_dump(by_alias=True)

    assert set(dumped["aws_config"].keys()) == {"region", "estimated_monthly_cost", "ecs", "lambda", "ec2", "s3"}
    assert set(dumped["deployment_config"].keys()) == {"ecs", "lambda", "ec2", "s3"}


def test_region_and_environment_are_threaded_instead_of_hardcoded() -> None:
    """Regression test (Tier 1, fix A): region/environment used to be
    hardcoded ("us-east-1"/"dev") here while the LLM deployment advisor and
    terraform_generator treated them as decided values. The JSON contract
    must agree with the rendered Terraform, for whatever region/environment
    were decided."""
    analysis = _load_analysis("sample_input.json")
    decision = decide_architecture(analysis)

    default_output = _build(analysis, decision)
    custom_output = _build(analysis, decision, region="eu-west-1", environment="staging")

    assert default_output.aws_config.region == "us-east-1"
    assert default_output.artifacts.terraform.variables == {
        "region": "us-east-1",
        "environment": "dev",
    }
    assert custom_output.aws_config.region == "eu-west-1"
    assert custom_output.artifacts.terraform.variables == {
        "region": "eu-west-1",
        "environment": "staging",
    }


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
