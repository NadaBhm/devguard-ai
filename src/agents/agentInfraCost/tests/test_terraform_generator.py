import pytest

from agentInfraCost.core.terraform_generator import generate_terraform
from agentInfraCost.models.internal_models import (
    DecisionResult,
    Ec2Sizing,
    EcsSizing,
    LambdaSizing,
    ScoreBreakdown,
)

EXPECTED_FILES = {"main.tf", "variables.tf", "outputs.tf"}


def _score_breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(ecs_score=0.0, lambda_score=0.0, ec2_score=0.0, signals={})


def _ecs_decision(service_name: str = "repo-name-service") -> DecisionResult:
    return DecisionResult(
        compute_type="ecs",
        ecs=EcsSizing(
            cluster="devguard-cluster",
            service_name=service_name,
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


def _lambda_decision() -> DecisionResult:
    return DecisionResult(
        compute_type="lambda",
        lambda_=LambdaSizing(
            function_name="small-cli-tool",
            runtime="python3.12",
            memory_mb=256,
            timeout_seconds=30,
            handler="main.handler",
            reserved_concurrency=None,
        ),
        score_breakdown=_score_breakdown(),
    )


def _ec2_decision() -> DecisionResult:
    return DecisionResult(
        compute_type="ec2",
        ec2=Ec2Sizing(
            instance_type="t3.small",
            ami_id="ami-0000000000000000",
            instance_count=2,
            key_pair_name="devguard-keypair",
            health_check_path="/status",
            health_check_port=80,
            timeout_minutes=10,
        ),
        score_breakdown=_score_breakdown(),
    )


def _assert_balanced_braces(content: str) -> None:
    assert content.count("{") == content.count("}"), "unbalanced braces: not valid HCL"


class TestEcsGeneration:
    def test_nominal_produces_the_three_expected_files(self) -> None:
        result = generate_terraform(
            _ecs_decision(), region="us-east-1", docker_image_uri="repo-name:a1b2c3d4e5f6"
        )
        assert set(result.files.keys()) == EXPECTED_FILES
        for content in result.files.values():
            _assert_balanced_braces(content)

    def test_interpolates_sizing_and_image_into_main_tf(self) -> None:
        result = generate_terraform(
            _ecs_decision(), region="us-east-1", docker_image_uri="repo-name:a1b2c3d4e5f6"
        )
        main_tf = result.files["main.tf"]
        assert 'cluster_name = "devguard-cluster"' in main_tf
        assert 'family                   = "repo-name-service"' in main_tf
        assert 'cpu                      = "512"' in main_tf
        assert 'memory                   = "1024"' in main_tf
        assert 'image     = "repo-name:a1b2c3d4e5f6"' in main_tf
        assert "deployment_minimum_healthy_percent = 50" in main_tf
        assert "deployment_maximum_percent         = 200" in main_tf

    def test_variables_dict_contains_region_and_environment(self) -> None:
        result = generate_terraform(
            _ecs_decision(),
            region="eu-west-1",
            environment="prod",
            docker_image_uri="repo-name:sha",
        )
        assert result.variables == {"region": "eu-west-1", "environment": "prod"}

    def test_missing_docker_image_uri_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="docker_image_uri is required"):
            generate_terraform(_ecs_decision(), region="us-east-1")

    def test_missing_sizing_block_raises_value_error(self) -> None:
        decision = DecisionResult(compute_type="ecs", ecs=None, score_breakdown=_score_breakdown())
        with pytest.raises(ValueError, match="decision.ecs is None"):
            generate_terraform(decision, region="us-east-1", docker_image_uri="x:y")


class TestLambdaGeneration:
    def test_nominal_produces_the_three_expected_files(self) -> None:
        result = generate_terraform(_lambda_decision(), region="us-east-1")
        assert set(result.files.keys()) == EXPECTED_FILES
        for content in result.files.values():
            _assert_balanced_braces(content)

    def test_interpolates_function_config_into_main_tf(self) -> None:
        result = generate_terraform(_lambda_decision(), region="us-east-1")
        main_tf = result.files["main.tf"]
        assert 'function_name = "small-cli-tool"' in main_tf
        assert 'handler       = "main.handler"' in main_tf
        assert 'runtime       = "python3.12"' in main_tf
        assert "memory_size   = 256" in main_tf
        assert "timeout       = 30" in main_tf
        assert "reserved_concurrent_executions" not in main_tf

    def test_reserved_concurrency_is_emitted_when_set(self) -> None:
        decision = DecisionResult(
            compute_type="lambda",
            lambda_=LambdaSizing(
                function_name="fn",
                runtime="python3.12",
                memory_mb=256,
                timeout_seconds=30,
                handler="main.handler",
                reserved_concurrency=5,
            ),
            score_breakdown=_score_breakdown(),
        )
        result = generate_terraform(decision, region="us-east-1")
        assert "reserved_concurrent_executions = 5" in result.files["main.tf"]

    def test_docker_image_uri_is_ignored_for_lambda(self) -> None:
        with_image = generate_terraform(
            _lambda_decision(), region="us-east-1", docker_image_uri="ignored:tag"
        )
        without_image = generate_terraform(_lambda_decision(), region="us-east-1")
        assert with_image.files == without_image.files

    def test_missing_sizing_block_raises_value_error(self) -> None:
        decision = DecisionResult(
            compute_type="lambda", lambda_=None, score_breakdown=_score_breakdown()
        )
        with pytest.raises(ValueError, match="decision.lambda_ is None"):
            generate_terraform(decision, region="us-east-1")


class TestEc2Generation:
    def test_nominal_produces_the_three_expected_files(self) -> None:
        result = generate_terraform(_ec2_decision(), region="us-east-1")
        assert set(result.files.keys()) == EXPECTED_FILES
        for content in result.files.values():
            _assert_balanced_braces(content)

    def test_interpolates_instance_config_into_main_tf(self) -> None:
        result = generate_terraform(_ec2_decision(), region="us-east-1")
        main_tf = result.files["main.tf"]
        assert "count         = 2" in main_tf
        assert 'ami           = "ami-0000000000000000"' in main_tf
        assert 'instance_type = "t3.small"' in main_tf
        assert 'key_name      = "devguard-keypair"' in main_tf
        assert "user_data" not in main_tf

    def test_docker_image_uri_adds_user_data_block(self) -> None:
        result = generate_terraform(
            _ec2_decision(), region="us-east-1", docker_image_uri="legacy-app:c9d8e7f6a5b4"
        )
        main_tf = result.files["main.tf"]
        assert "user_data" in main_tf
        assert "docker run -d -p 80:80 legacy-app:c9d8e7f6a5b4" in main_tf

    def test_missing_sizing_block_raises_value_error(self) -> None:
        decision = DecisionResult(compute_type="ec2", ec2=None, score_breakdown=_score_breakdown())
        with pytest.raises(ValueError, match="decision.ec2 is None"):
            generate_terraform(decision, region="us-east-1")


class TestHclStringEscaping:
    def test_double_quote_in_value_does_not_break_hcl(self) -> None:
        decision = _ecs_decision(service_name='evil"name')
        result = generate_terraform(
            decision, region="us-east-1", docker_image_uri="repo:tag"
        )
        main_tf = result.files["main.tf"]
        assert 'evil\\"name' in main_tf
        _assert_balanced_braces(main_tf)

    def test_backslash_in_value_is_escaped(self) -> None:
        decision = _ecs_decision(service_name="path\\to\\thing")
        result = generate_terraform(
            decision, region="us-east-1", docker_image_uri="repo:tag"
        )
        assert "path\\\\to\\\\thing" in result.files["main.tf"]


class TestUnknownComputeType:
    def test_raises_value_error(self) -> None:
        decision = _ecs_decision().model_copy(update={"compute_type": "unknown"})
        with pytest.raises(ValueError, match="Unknown compute_type"):
            generate_terraform(decision, region="us-east-1", docker_image_uri="x:y")
