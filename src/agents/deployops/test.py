"""
    mock tests of deployOps before actual  AWS testing

"""




# tests/test_deployops.py
import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import httpx
from moto import mock_aws
import boto3

# Adjust import to your actual module
from src.agents.deployops.agent import DeployOpsAgent


# ---------- Fixtures ----------

@pytest.fixture
def sample_payload():
    """Minimal valid payload for testing."""
    return {
        "job_id": "test_job_123",
        "artifacts": {
            "terraform": {
                "files": {
                    "main.tf": 'resource "aws_s3_bucket" "test" { bucket = "my-bucket" }',
                    "variables.tf": 'variable "region" { default = "us-east-1" }',
                },
                "variables": {"region": "us-east-1"},
            },
            "dockerfile": "FROM python:3.12-slim\nCOPY . /app",
            "docker_image": {"name": "test-app", "tag": "latest"},
        },
        "aws_config": {
            "region": "us-east-1",
            "ecs_cluster": "test-cluster",
            "service_name": "test-service",
            "task_cpu": "256",
            "task_memory": "512",
        },
        "deployment_config": {
            "strategy": "rolling",
            "health_check_path": "/health",
            "health_check_port": 8080,
            "timeout_minutes": 5,
            "min_healthy_percent": 50,
            "max_percent": 200,
        },
        "approval": {"deploy_approved": True, "approved_by": "test@example.com"},
    }


@pytest.fixture
def agent():
    return DeployOpsAgent()


@pytest.fixture
def workspace(tmp_path):
    """Temporary workspace for artifact writing."""
    return tmp_path / "deployops" / "test_job_123"


# ---------- Validation Tests ----------

def test_sanitize_and_validate_valid(agent, sample_payload):
    result = agent.sanitize_and_validate(sample_payload)
    assert result["job_id"] == "test_job_123"
    assert "main.tf" in result["artifacts"]["terraform"]["files"]
    assert result["artifacts"]["dockerfile"] == sample_payload["artifacts"]["dockerfile"]
    assert result["aws_config"]["region"] == "us-east-1"


def test_sanitize_and_validate_missing_field(agent):
    payload = {"job_id": "test"}  # missing required
    with pytest.raises(ValueError, match="Missing required field: 'artifacts'"):
        agent.sanitize_and_validate(payload)


def test_sanitize_and_validate_invalid_job_id(agent, sample_payload):
    payload = sample_payload.copy()
    payload["job_id"] = "job with spaces"
    with pytest.raises(ValueError, match="job_id contains invalid characters"):
        agent.sanitize_and_validate(payload)


def test_sanitize_and_validate_invalid_region(agent, sample_payload):
    payload = sample_payload.copy()
    payload["aws_config"]["region"] = "eu-central-1a"  # invalid format
    with pytest.raises(ValueError, match="Invalid AWS region"):
        agent.sanitize_and_validate(payload)


# ---------- Artifact Writing Tests ----------

def test_write_artifacts(agent, sample_payload, workspace):
    agent._write_artifacts(sample_payload["artifacts"], workspace)

    # Check files exist
    tf_dir = workspace / "terraform"
    assert (tf_dir / "main.tf").exists()
    assert (tf_dir / "variables.tf").exists()
    assert (workspace / "Dockerfile").exists()

    # Check content
    content = (tf_dir / "main.tf").read_text()
    assert "aws_s3_bucket" in content

    # Variables file should exist if variables provided
    assert (tf_dir / "terraform.tfvars.json").exists()
    vars_content = json.loads((tf_dir / "terraform.tfvars.json").read_text())
    assert vars_content["region"] == "us-east-1"


# ---------- Health Check Tests ----------

@patch("httpx.get")
def test_health_check_success(mock_get, agent):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_get.return_value = mock_response

    result = agent.health_check("http://localhost:8080", max_retries=2, timeout=1)
    assert result is True
    mock_get.assert_called_with("http://localhost:8080/health", timeout=1)


@patch("httpx.get")
def test_health_check_fails_http_error(mock_get, agent):
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_get.return_value = mock_response

    result = agent.health_check("http://localhost:8080", max_retries=1)
    assert result is False


@patch("httpx.get")
def test_health_check_timeout(mock_get, agent):
    mock_get.side_effect = httpx.TimeoutException("timeout")
    result = agent.health_check("http://localhost:8080", max_retries=1)
    assert result is False


def test_health_check_missing_url(agent):
    result = agent.health_check(None)
    assert result is False


# ---------- Rollback Tests (using moto) ----------

@patch("src.agents.deployops.agent.AWSClient")
def test_rollback_success(mock_aws_client, agent, sample_payload):
    """Test rollback uses previous task definition."""
    
    # Mock ECS response with 2 deployments
    mock_ecs = MagicMock()
    mock_ecs.describe_services.return_value = {
    "services": [{
        "deployments": [
            {
                "taskDefinition": "arn:aws:ecs:us-east-1:123:task-definition/app-task:2",
                "status": "INACTIVE"
            },
            {
                "taskDefinition": "arn:aws:ecs:us-east-1:123:task-definition/app-task:3",
                "status": "PRIMARY"
            }
        ]
    }]
    }
    
    # Mock update_service
    mock_ecs.update_service.return_value = {}
    
    # Mock waiter
    mock_waiter = MagicMock()
    mock_ecs.get_waiter.return_value = mock_waiter
    
    # Set up AWS client mock
    aws_instance = MagicMock()
    aws_instance.ecs.return_value = mock_ecs
    mock_aws_client.return_value = aws_instance
    
    # Call rollback
    result = agent.rollback("test_job_123", sample_payload)
    
    assert result["status"] == "success"
    assert "Rolled back" in result["message"]
    mock_ecs.update_service.assert_called_once_with(
        cluster=sample_payload["aws_config"]["ecs_cluster"],
        service=sample_payload["aws_config"]["service_name"],
        taskDefinition="arn:aws:ecs:us-east-1:123:task-definition/app-task:2",
        forceNewDeployment=True
    )

@mock_aws
def test_rollback_no_previous(agent, sample_payload):
    """Rollback fails if only one deployment exists."""
    ecs = boto3.client("ecs", region_name="us-east-1")
    cluster = sample_payload["aws_config"]["ecs_cluster"]
    service = sample_payload["aws_config"]["service_name"]

    ecs.create_cluster(clusterName=cluster)
    td = ecs.register_task_definition(
        family="app-task",
        containerDefinitions=[{"name": "app", "image": "app:v1", "cpu": 256, "memory": 512}],
    )["taskDefinition"]["taskDefinitionArn"]

    ecs.create_service(
        cluster=cluster,
        serviceName=service,
        taskDefinition=td,
        desiredCount=1,
    )

    result = agent.rollback("test_job_123", sample_payload)
    assert result["status"] == "failed"
    assert "No previous deployment" in result["error"]


@mock_aws
def test_rollback_service_not_found(agent, sample_payload):
    """Rollback fails if service doesn't exist."""
    result = agent.rollback("test_job_123", sample_payload)
    assert result["status"] == "failed"
    assert "error" in result


# ---------- Mocked Deploy Test (full flow with mocks) ----------

@patch("subprocess.run")
@patch("src.agents.deployops.agent.AWSClient")
@patch("src.agents.deployops.agent.TerraformRunner")
def test_deploy_full_success(mock_tf_runner, mock_aws_client, mock_subprocess_run, agent, sample_payload):
    """Test full deploy with all steps mocked to success."""
    # Mock TerraformRunner methods
    tf_instance = MagicMock()
    tf_instance.init.return_value = True
    tf_instance.plan.return_value = {"planned": "changes"}
    tf_instance.apply.return_value = True
    tf_instance.output.return_value = {"service_url": {"value": "http://test.com"}}
    mock_tf_runner.return_value = tf_instance

    # Mock AWSClient
    aws_instance = MagicMock()
    aws_instance.get_account_id.return_value = "123456789012"
    mock_aws_client.return_value = aws_instance

    def subprocess_side_effect(cmd, **kwargs):
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = ""
        mock.stderr = ""
        return mock

    mock_subprocess_run.side_effect = subprocess_side_effect

    # CREATE THE DIRECTORY FIRST
    workspace_dir = Path("/tmp/deployops/test_job_123")
    workspace_dir.mkdir(parents=True, exist_ok=True)

    with patch.object(agent, "health_check", return_value=True):
        agent.payload = sample_payload
        result = agent.deploy()

    assert result["status"] == "success"
    assert result["job_id"] == "test_job_123"
    assert result["deployed_url"] == "http://test.com"


@patch("subprocess.run")
@patch("src.agents.deployops.agent.AWSClient")
@patch("src.agents.deployops.agent.TerraformRunner")
def test_deploy_health_check_fails_calls_rollback(mock_tf_runner, mock_aws_client, mock_subprocess_run, agent, sample_payload):
    """Test deploy when health check fails -> rollback called."""
    # Setup similar mocks
    tf_instance = MagicMock()
    tf_instance.init.return_value = True
    tf_instance.plan.return_value = {"planned": "changes"}
    tf_instance.apply.return_value = True
    tf_instance.output.return_value = {"service_url": {"value": "http://test.com"}}
    mock_tf_runner.return_value = tf_instance

    aws_instance = MagicMock()
    aws_instance.get_account_id.return_value = "123456789012"
    mock_aws_client.return_value = aws_instance

    def subprocess_side_effect(cmd, **kwargs):
        mock = MagicMock()
        mock.returncode = 0
        return mock
    mock_subprocess_run.side_effect = subprocess_side_effect

    # Health check returns False
    with patch.object(agent, "health_check", return_value=False):
        # Mock rollback to avoid actual AWS calls (though rollback has its own mocks)
        with patch.object(agent, "rollback", return_value={"status": "success"}) as mock_rollback:
            agent.payload = sample_payload
            result = agent.deploy()

    assert result["status"] == "failed"
    assert "health check failed" in result["error"]
    mock_rollback.assert_called_once()

# ---------- Additional Edge Cases ----------

def test_deploy_not_approved(agent, sample_payload):
    """Deploy returns early if not approved."""
    payload = sample_payload.copy()
    payload["approval"]["deploy_approved"] = False
    agent.payload = payload
    result = agent.deploy()
    assert result["status"] == "failed"
    assert "not approved" in result["error"]