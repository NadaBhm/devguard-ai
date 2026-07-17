"""Tests for core.terraform_generator."""

import json
from pathlib import Path

import pytest
from jinja2 import UndefinedError
from pydantic import ValidationError

from core.decision_engine import DecisionResult, decide_architecture
from core.terraform_generator import _ENV, TerraformContext, generate_terraform
from models.input_schema import RepoAnalysisInput

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_analysis(filename: str) -> RepoAnalysisInput:
    raw = json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))
    return RepoAnalysisInput.model_validate(raw)


# --------------------------------------------------------------------------
# Nominal cases
# --------------------------------------------------------------------------


def test_generate_terraform_for_ecs_fixture() -> None:
    analysis = _load_analysis("sample_input.json")
    decision = decide_architecture(analysis)
    assert decision.compute_type == "ecs"
    context = TerraformContext(job_id=analysis.job_id, docker_image="devguard-app:sha-a1b2c3d")

    files = generate_terraform(decision, context)

    assert "aws_ecs_cluster" in files.main_tf
    assert f'cpu                       = "{decision.sizing["task_cpu"]}"' in files.main_tf
    assert f'memory                    = "{decision.sizing["task_memory"]}"' in files.main_tf
    assert "devguard-app:sha-a1b2c3d" in files.main_tf
    assert 'default     = "us-east-1"' in files.variables_tf
    assert "aws_ecs_cluster.this.name" in files.outputs_tf


def test_generate_terraform_for_lambda_fixture() -> None:
    analysis = _load_analysis("sample_input_variant_lambda_candidate.json")
    decision = decide_architecture(analysis)
    assert decision.compute_type == "lambda"
    context = TerraformContext(job_id=analysis.job_id)

    files = generate_terraform(decision, context)

    assert "aws_lambda_function" in files.main_tf
    assert f'memory_size   = {decision.sizing["memory_mb"]}' in files.main_tf
    assert "aws_lambda_function.this.function_name" in files.outputs_tf


def test_generate_terraform_for_node_ecs_fixture_is_valid_too() -> None:
    """Proves generation isn't tied to the FastAPI example specifically."""
    analysis = _load_analysis("sample_input_variant_node_ecs.json")
    decision = decide_architecture(analysis)
    assert decision.compute_type == "ecs"
    context = TerraformContext(job_id=analysis.job_id, docker_image="devguard-app:sha-9988776")

    files = generate_terraform(decision, context)

    assert "aws_ecs_cluster" in files.main_tf
    assert "devguard-app:sha-9988776" in files.main_tf


def test_generate_terraform_for_ec2() -> None:
    """No fixture picks ec2 naturally, so the decision is built by hand here."""
    decision = DecisionResult(
        compute_type="ec2",
        sizing={"instance_type": "t3.small"},
        score_breakdown={"ecs": -3.0, "lambda": 0.0, "ec2": 5.0},
    )
    context = TerraformContext(job_id="job-ec2-test")

    files = generate_terraform(decision, context)

    assert "aws_instance" in files.main_tf
    assert 'instance_type = "t3.small"' in files.main_tf
    assert "aws_instance.this[*].id" in files.outputs_tf


# --------------------------------------------------------------------------
# Limit / edge cases
# --------------------------------------------------------------------------


def test_sizing_is_actually_templated_not_hardcoded() -> None:
    """Two different sizings must produce two genuinely different files."""
    context = TerraformContext(job_id="job-a")
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

    small_files = generate_terraform(small, context)
    large_files = generate_terraform(large, context)

    assert '"256"' in small_files.main_tf and '"512"' in small_files.main_tf
    assert '"1024"' in large_files.main_tf and '"2048"' in large_files.main_tf
    assert small_files.main_tf != large_files.main_tf


def test_each_compute_type_renders_only_its_own_resources() -> None:
    context = TerraformContext(job_id="job-b")
    ecs = generate_terraform(
        DecisionResult(
            compute_type="ecs",
            sizing={"task_cpu": "256", "task_memory": "512"},
            score_breakdown={"ecs": 1.0, "lambda": 0.0, "ec2": 0.0},
        ),
        context,
    )
    lambda_ = generate_terraform(
        DecisionResult(
            compute_type="lambda",
            sizing={"memory_mb": 128},
            score_breakdown={"ecs": 0.0, "lambda": 1.0, "ec2": 0.0},
        ),
        context,
    )
    ec2 = generate_terraform(
        DecisionResult(
            compute_type="ec2",
            sizing={"instance_type": "t3.micro"},
            score_breakdown={"ecs": 0.0, "lambda": 0.0, "ec2": 1.0},
        ),
        context,
    )

    assert "aws_ecs" in ecs.main_tf and "aws_lambda" not in ecs.main_tf and "aws_instance" not in ecs.main_tf
    assert "aws_lambda" in lambda_.main_tf and "aws_ecs" not in lambda_.main_tf and "aws_instance" not in lambda_.main_tf
    assert "aws_instance" in ec2.main_tf and "aws_ecs" not in ec2.main_tf and "aws_lambda" not in ec2.main_tf


def test_terraform_files_round_trip_with_contract_aliases() -> None:
    context = TerraformContext(job_id="job-c")
    decision = DecisionResult(
        compute_type="ecs",
        sizing={"task_cpu": "256", "task_memory": "512"},
        score_breakdown={"ecs": 1.0, "lambda": 0.0, "ec2": 0.0},
    )
    files = generate_terraform(decision, context)
    dumped = files.model_dump(by_alias=True)
    assert set(dumped.keys()) == {"main.tf", "variables.tf", "outputs.tf"}


# --------------------------------------------------------------------------
# Error cases
# --------------------------------------------------------------------------


def test_terraform_context_requires_job_id() -> None:
    with pytest.raises(ValidationError):
        TerraformContext()  # type: ignore[call-arg]


def test_missing_sizing_key_raises_clear_key_error() -> None:
    """A DecisionResult with an incomplete `sizing` dict fails loudly, not silently."""
    decision = DecisionResult(
        compute_type="ecs",
        sizing={"task_cpu": "256"},  # task_memory missing on purpose
        score_breakdown={"ecs": 1.0, "lambda": 0.0, "ec2": 0.0},
    )
    context = TerraformContext(job_id="job-d")
    with pytest.raises(KeyError):
        generate_terraform(decision, context)


def test_template_variable_missing_from_context_raises_clear_error() -> None:
    """StrictUndefined must turn a template typo/gap into a real exception,
    never a silently blank value in the generated Terraform."""
    with pytest.raises(UndefinedError):
        _ENV.get_template("ecs/main.tf.j2").render(cluster_name="only-this-one-is-set")
