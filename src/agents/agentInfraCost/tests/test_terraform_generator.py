"""Tests for core.terraform_generator."""

import json
from pathlib import Path

import pytest
from core.decision_engine import DecisionResult, decide_architecture
from core.terraform_generator import _ENV, TerraformContext, generate_terraform
from jinja2 import UndefinedError
from models.input_schema import RepoAnalysisInput
from pydantic import ValidationError

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


def test_ecs_database_detected_adds_connection_placeholders() -> None:
    """Regression test: stack_detection.database used to only influence the
    architecture score, never reach the generated Terraform at all. Fixed
    2026-08-05 (option 2: declare connection variables, never auto-create a
    real, paying database)."""
    analysis = _load_analysis("sample_input.json")
    decision = decide_architecture(analysis)
    context = TerraformContext(
        job_id=analysis.job_id, docker_image="devguard-app:sha-a1b2c3d", database="postgresql"
    )

    files = generate_terraform(decision, context)

    assert 'DB_ENGINE", value = "postgresql"' in files.main_tf
    assert "var.db_host" in files.main_tf
    assert "var.db_password" in files.main_tf
    assert 'variable "db_password"' in files.variables_tf
    assert "sensitive   = true" in files.variables_tf
    # never a real database resource -- only variable declarations
    assert "aws_db_instance" not in files.main_tf


def test_ecs_no_database_detected_adds_no_db_placeholders() -> None:
    analysis = _load_analysis("sample_input.json")
    decision = decide_architecture(analysis)
    context = TerraformContext(job_id=analysis.job_id, docker_image="devguard-app:sha-a1b2c3d")

    files = generate_terraform(decision, context)

    assert "DB_ENGINE" not in files.main_tf
    assert "db_password" not in files.variables_tf


def test_ecs_service_has_working_health_check_and_networking() -> None:
    """Regression test for a real bug: the Fargate service used to have no
    network_configuration block (terraform apply would fail outright) and
    health_check_path never reached the actual Terraform, only the JSON
    output. Flagged by a teammate's review (Oussama), fixed 2026-08-05."""
    analysis = _load_analysis("sample_input.json")
    decision = decide_architecture(analysis)
    context = TerraformContext(job_id=analysis.job_id, docker_image="devguard-app:sha-a1b2c3d")

    files = generate_terraform(decision, context)

    assert "network_configuration" in files.main_tf
    assert "subnets          = var.subnet_ids" in files.main_tf
    assert "aws_lb_target_group" in files.main_tf
    assert 'path                = "/health"' in files.main_tf
    assert "load_balancer {" in files.main_tf
    assert 'variable "vpc_id"' in files.variables_tf
    assert 'variable "subnet_ids"' in files.variables_tf
    assert "load_balancer_dns_name" in files.outputs_tf


def test_ecs_template_is_applyable_with_execution_role_and_logging() -> None:
    """Regression test: the ECS template used to omit the task execution IAM
    role, execution_role_arn and container log configuration — productions
    applied against it failed with "Missing required position
    'executionRoleArn'" and shipped no CloudWatch logging. Fixed 2026-08-11
    (Tier 1, fix B)."""
    analysis = _load_analysis("sample_input.json")
    decision = decide_architecture(analysis)
    context = TerraformContext(job_id=analysis.job_id, docker_image="devguard-app:sha-a1b2c3d")

    files = generate_terraform(decision, context)

    # IAM execution role with the ECS-tasks assume-role policy
    assert "aws_iam_role" in files.main_tf
    assert 'name = "devguard-task-execution-role"' in files.main_tf
    assert 'Principal' in files.main_tf
    assert 'Service = "ecs-tasks.amazonaws.com"' in files.main_tf
    assert "AmazonECSTaskExecutionRolePolicy" in files.main_tf
    # wired onto the task definition, so ECS can pull from ECR and stream logs
    assert "execution_role_arn        = aws_iam_role.ecs_task_execution.arn" in files.main_tf
    # CloudWatch log group + awslogs container configuration pinned to `var.region`
    assert "aws_cloudwatch_log_group" in files.main_tf
    assert 'name              = "/ecs/app-service"' in files.main_tf
    assert 'logDriver = "awslogs"' in files.main_tf
    assert '"awslogs-group"         = "/ecs/app-service"' in files.main_tf
    assert '"awslogs-region"        = var.region' in files.main_tf


def test_ecs_multi_container_renders_one_definition_per_image() -> None:
    """Multi-container ECS: every image becomes its own container_definition,
    co-scheduled in one Fargate task. The ALB fronts the primary (first)
    container; secondary containers share the task's localhost."""
    decision = DecisionResult(
        compute_type="ecs",
        sizing={"task_cpu": 512, "task_memory": 1024},
        score_breakdown={"ecs": 1.0, "lambda": 0.0, "ec2": 0.0, "s3": 0.0},
    )
    context = TerraformContext(
        job_id="job-multi-1",
        docker_images=[
            {"name": "devguard-app", "image": "1111.dkr.ecr.us-east-1.amazonaws.com/devguard-app:sha-abc",
             "port": 8000, "context": "."},
            {"name": "devguard-app-frontend",
             "image": "1111.dkr.ecr.us-east-1.amazonaws.com/devguard-app-frontend:sha-abc",
             "port": 80, "context": "frontend"},
        ],
        health_check_port=8000,
    )

    files = generate_terraform(decision, context)

    assert files.main_tf.count("portMappings = [") == 1
    assert "name  = \"devguard-app\"" in files.main_tf
    assert "name  = \"devguard-app-frontend\"" in files.main_tf
    # The secondary container shares the task's localhost, so it must NOT map
    # a host port — one Fargate task has a single ENI, and two containers on
    # the same port made RegisterTaskDefinition fail with "TCP host port
    # '3000' is mapped multiple times in task" (verified live on mean-docker).
    assert "containerPort = 80\n" not in files.main_tf
    assert "containerPort = 8000" in files.main_tf
    # ALB + SG + service block all target the primary container.
    assert "container_name   = \"devguard-app\"" in files.main_tf
    assert "container_port   = 8000" in files.main_tf
    assert "port        = 8000" in files.main_tf
    # No database on the context -> no DB env placeholders at all.
    assert "DB_HOST" not in files.main_tf


def test_ecs_multi_container_db_env_only_on_primary() -> None:
    decision = DecisionResult(
        compute_type="ecs",
        sizing={"task_cpu": 512, "task_memory": 1024},
        score_breakdown={"ecs": 1.0},
    )
    context = TerraformContext(
        job_id="job-multi-2",
        database="postgresql",
        docker_images=[
            {"name": "devguard-app", "image": "x/devguard-app:1", "port": 8000, "context": "."},
            {"name": "devguard-app-frontend", "image": "x/devguard-app-frontend:1", "port": 80, "context": "frontend"},
        ],
    )

    files = generate_terraform(decision, context)

    assert 'name = "DB_ENGINE", value = "postgresql"' in files.main_tf
    # Only one container carries the DB placeholders.
    assert files.main_tf.count("DB_ENGINE") == 1


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
    # AL2's bundled Docker 25.x (installed via amazon-linux-extras) handles the
    # mixed Docker/OCI manifests this pipeline pushes. Docker CE from the
    # official centos/7 repo is unusable on AL2: it needs container-selinux /
    # slirp4netns / fuse-overlayfs that no longer resolve (EL7 EPEL archived).
    assert "amazon-linux-extras install -y docker" in files.main_tf
    assert "docker-ce" not in files.main_tf
    assert "yum install -y -q amazon-linux-extras jq yum-utils" in files.main_tf


def test_ec2_template_omits_key_name_when_no_key_pair_configured() -> None:
    """The EC2 template must NOT hardcode a key pair that may not exist in the
    target account (InvalidKeyPair.NotFound broke real applies). With the
    default empty DEVGUARD_KEY_PAIR_NAME, no key_name attribute is rendered.
    """
    decision = DecisionResult(
        compute_type="ec2",
        sizing={"instance_type": "t3.small"},
        score_breakdown={"ecs": -3.0, "lambda": 0.0, "ec2": 5.0},
    )
    context = TerraformContext(job_id="job-ec2-test")

    files = generate_terraform(decision, context)

    assert "key_name" not in files.main_tf
    assert "devguard-key" not in files.main_tf


def test_ec2_resource_names_are_suffixed_with_job_id() -> None:
    """EC2 IAM role/profile/SG names derive from instance_name, which must be
    job-suffixed like ECS so retries and concurrent jobs never collide on
    EntityAlreadyExists from partial-apply leftovers.
    """
    decision = DecisionResult(
        compute_type="ec2",
        sizing={"instance_type": "t3.small"},
        score_breakdown={"ecs": -3.0, "lambda": 0.0, "ec2": 5.0},
    )
    files = generate_terraform(decision, TerraformContext(job_id="job-ec2-test"))

    assert "devguard-app-job-ec2" in files.main_tf
    assert 'resource "aws_iam_role" "instance"' in files.main_tf
    assert 'resource "aws_security_group" "instance"' in files.main_tf


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
