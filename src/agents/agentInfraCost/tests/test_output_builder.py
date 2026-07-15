import json
import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentInfraCost.core.output_builder import build_output, resolve_docker_image
from agentInfraCost.models.internal_models import (
    DecisionResult,
    Ec2Sizing,
    EcsSizing,
    LambdaSizing,
    ScoreBreakdown,
)
from agentInfraCost.models.output_models import (
    Approval,
    Enrichment,
    EstimatedMonthlyCost,
    InfraCostOutput,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _score_breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(
        ecs_score=0.0,
        lambda_score=0.0,
        ec2_score=0.0,
        signals={"container_detected": True},
    )


def _enrichment() -> Enrichment:
    return Enrichment(
        architecture_explanation="explanation",
        cost_summary="summary",
        finops_justification="justification",
        enrichment_source="fallback",
    )


def _cost() -> EstimatedMonthlyCost:
    return EstimatedMonthlyCost(amount=18.02, currency="USD", range_min=14.42, range_max=21.62)


def _common_kwargs(**overrides):
    kwargs = dict(
        job_id="job-1",
        estimated_monthly_cost=_cost(),
        terraform_files={"main.tf": "x", "variables.tf": "y", "outputs.tf": "z"},
        terraform_variables={"region": "us-east-1", "environment": "dev"},
        region="us-east-1",
        repo_name="repo-name",
        commit_sha="a1b2c3d4e5f6",
        container_detected=True,
        dockerfile_content="FROM python:3.12-slim",
        source_code_path="/tmp/repo_job-1",
        approval=Approval(status="pending"),
        enrichment=_enrichment(),
    )
    kwargs.update(overrides)
    return kwargs


class TestResolveDockerImage:
    def test_uses_commit_sha_when_available(self) -> None:
        image = resolve_docker_image(repo_name="repo-name", commit_sha="a1b2c3d4e5f6789")
        assert image.name == "repo-name"
        assert image.tag == "a1b2c3d4e5f6"  # truncated to 12 chars

    def test_falls_back_to_latest_and_warns_when_no_commit_sha(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING):
            image = resolve_docker_image(repo_name="repo-name", commit_sha=None)
        assert image.tag == "latest"
        assert any("latest" in record.message for record in caplog.records)


class TestBuildOutputNominal:
    def test_ecs_nominal(self) -> None:
        decision = DecisionResult(
            compute_type="ecs",
            ecs=EcsSizing(
                cluster="devguard-cluster",
                service_name="repo-name-service",
                task_cpu=512,
                task_memory=1024,
                health_check_path="/health",
                health_check_port=8080,
                timeout_minutes=5,
                min_healthy_percent=50,
                max_percent=200,
            ),
            score_breakdown=_score_breakdown(),
        )
        result = build_output(decision=decision, **_common_kwargs())

        assert result.compute_type == "ecs"
        assert result.aws_config.ecs is not None
        assert result.aws_config.ecs.task_cpu == "512"
        assert result.aws_config.lambda_ is None
        assert result.aws_config.ec2 is None
        assert result.deployment_config.ecs is not None
        assert result.deployment_config.ecs.min_healthy_percent == 50
        assert result.deployment_config.lambda_ is None
        assert result.deployment_config.ec2 is None
        assert result.artifacts.docker_image is not None
        assert result.artifacts.docker_image.tag == "a1b2c3d4e5f6"

    def test_lambda_nominal_without_container(self) -> None:
        decision = DecisionResult(
            compute_type="lambda",
            lambda_=LambdaSizing(
                function_name="small-cli-tool",
                runtime="python3.12",
                memory_mb=256,
                timeout_seconds=30,
                handler="handler.handler",
            ),
            score_breakdown=_score_breakdown(),
        )
        result = build_output(
            decision=decision,
            **_common_kwargs(
                container_detected=False,
                dockerfile_content=None,
                commit_sha="f1e2d3c4b5a6",
            ),
        )

        assert result.compute_type == "lambda"
        assert result.aws_config.lambda_ is not None
        assert result.aws_config.lambda_.memory_mb == 256
        assert result.aws_config.ecs is None
        assert result.aws_config.ec2 is None
        assert result.deployment_config.lambda_ is not None
        assert result.deployment_config.ecs is None
        assert result.deployment_config.ec2 is None
        # No container detected -> both dockerfile and docker_image stay null,
        # even though a commit_sha was available.
        assert result.artifacts.dockerfile is None
        assert result.artifacts.docker_image is None

    def test_ec2_nominal(self) -> None:
        decision = DecisionResult(
            compute_type="ec2",
            ec2=Ec2Sizing(
                instance_type="t3.small",
                ami_id="ami-0abcdef1234567890",
                instance_count=2,
                key_pair_name="devguard-keypair",
                health_check_path="/status",
                health_check_port=80,
                timeout_minutes=10,
            ),
            score_breakdown=_score_breakdown(),
        )
        result = build_output(decision=decision, **_common_kwargs())

        assert result.compute_type == "ec2"
        assert result.aws_config.ec2 is not None
        assert result.aws_config.ec2.instance_count == 2
        assert result.aws_config.ecs is None
        assert result.aws_config.lambda_ is None
        assert result.deployment_config.ec2 is not None
        assert result.deployment_config.ecs is None
        assert result.deployment_config.lambda_ is None


class TestBuildOutputEdgeCases:
    def test_container_detected_but_no_commit_sha_falls_back_to_latest(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        decision = DecisionResult(
            compute_type="ecs",
            ecs=EcsSizing(
                cluster="devguard-cluster",
                service_name="repo-name-service",
                task_cpu=256,
                task_memory=512,
                health_check_path="/health",
                health_check_port=8080,
                timeout_minutes=5,
                min_healthy_percent=50,
                max_percent=200,
            ),
            score_breakdown=_score_breakdown(),
        )
        with caplog.at_level(logging.WARNING):
            result = build_output(decision=decision, **_common_kwargs(commit_sha=None))

        assert result.artifacts.docker_image is not None
        assert result.artifacts.docker_image.tag == "latest"
        assert any("latest" in record.message for record in caplog.records)

    def test_output_is_serializable_and_matches_ecs_fixture_shape(self) -> None:
        decision = DecisionResult(
            compute_type="ecs",
            ecs=EcsSizing(
                cluster="devguard-cluster",
                service_name="repo-name-service",
                task_cpu=512,
                task_memory=1024,
                health_check_path="/health",
                health_check_port=8080,
                timeout_minutes=5,
                min_healthy_percent=50,
                max_percent=200,
            ),
            score_breakdown=_score_breakdown(),
        )
        result = build_output(decision=decision, **_common_kwargs(job_id="550e8400-e29b-41d4-a716-446655440000"))
        dumped = result.model_dump(mode="json", by_alias=True)

        fixture = json.loads((FIXTURES_DIR / "sample_output_ecs.json").read_text())
        assert dumped["compute_type"] == fixture["compute_type"]
        assert dumped["aws_config"]["ecs"]["cluster"] == fixture["aws_config"]["ecs"]["cluster"]
        assert dumped["aws_config"]["lambda"] is None
        assert dumped["aws_config"]["ec2"] is None
        assert set(dumped["aws_config"].keys()) == set(fixture["aws_config"].keys())


class TestBuildOutputErrors:
    def test_raises_when_compute_type_has_no_matching_sizing_block(self) -> None:
        decision = DecisionResult(
            compute_type="ecs",
            ecs=None,  # inconsistent: compute_type says ecs but no sizing was produced
            score_breakdown=_score_breakdown(),
        )
        with pytest.raises(ValueError, match="matching sizing block is missing"):
            build_output(decision=decision, **_common_kwargs())

    def test_cannot_construct_output_with_mismatched_compute_type(self) -> None:
        """InfraCostOutput itself must reject an inconsistent aws_config/deployment_config,
        independent of output_builder — this is the schema-level guarantee."""
        from agentInfraCost.models.output_models import (
            Artifacts,
            AwsConfig,
            DeploymentConfig,
            EcsAwsConfig,
            EcsDeploymentConfig,
            LambdaAwsConfig,
            TerraformArtifacts,
        )

        with pytest.raises(ValidationError):
            InfraCostOutput(
                job_id="job-1",
                compute_type="lambda",
                artifacts=Artifacts(
                    terraform=TerraformArtifacts(files={}, variables={}),
                    source_code="/tmp/x",
                ),
                aws_config=AwsConfig(
                    region="us-east-1",
                    ecs=EcsAwsConfig(
                        cluster="c", service_name="s", task_cpu="256", task_memory="512"
                    ),
                    lambda_=LambdaAwsConfig(
                        function_name="f",
                        runtime="python3.12",
                        memory_mb=128,
                        timeout_seconds=10,
                        handler="h",
                    ),
                    estimated_monthly_cost=_cost(),
                ),
                deployment_config=DeploymentConfig(
                    ecs=EcsDeploymentConfig(
                        health_check_path="/health",
                        health_check_port=80,
                        timeout_minutes=5,
                        min_healthy_percent=50,
                        max_percent=200,
                    )
                ),
                approval=Approval(status="pending"),
                enrichment=_enrichment(),
            )

    def test_missing_required_field_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            EstimatedMonthlyCost(amount=10.0, range_min=8.0)  # missing range_max


@pytest.mark.parametrize(
    "fixture_name",
    ["sample_output_ecs.json", "sample_output_lambda.json", "sample_output_ec2.json"],
)
def test_all_output_fixtures_validate_against_schema(fixture_name: str) -> None:
    payload = json.loads((FIXTURES_DIR / fixture_name).read_text())
    output = InfraCostOutput.model_validate(payload)
    assert output.compute_type in {"ecs", "lambda", "ec2"}
