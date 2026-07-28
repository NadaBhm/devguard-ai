"""
    mock tests of deployOps before actual  AWS testing

"""




# tests/test_deployops.py
import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

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

@pytest.mark.asyncio
async def test_write_artifacts(agent, sample_payload, workspace):
    await agent._write_artifacts(sample_payload["artifacts"], workspace)

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

@pytest.mark.asyncio
@patch("httpx.AsyncClient.get", new_callable=AsyncMock)
async def test_health_check_success(mock_get, agent):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_get.return_value = mock_response

    result = await agent.health_check("http://localhost:8080", max_retries=2, timeout=1)
    assert result is True
    mock_get.assert_called_with("http://localhost:8080/health")


@pytest.mark.asyncio
@patch("httpx.AsyncClient.get", new_callable=AsyncMock)
async def test_health_check_fails_http_error(mock_get, agent):
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_get.return_value = mock_response

    result = await agent.health_check("http://localhost:8080", max_retries=1)
    assert result is False


@pytest.mark.asyncio
@patch("httpx.AsyncClient.get", new_callable=AsyncMock)
async def test_health_check_timeout(mock_get, agent):
    mock_get.side_effect = httpx.TimeoutException("timeout")
    result = await agent.health_check("http://localhost:8080", max_retries=1)
    assert result is False


@pytest.mark.asyncio
async def test_health_check_missing_url(agent):
    result = await agent.health_check(None)
    assert result is False


# ---------- Rollback Tests (using moto) ----------

@pytest.mark.asyncio
@patch("src.agents.deployops.agent.AWSClient")
async def test_rollback_success(mock_aws_client, agent, sample_payload):
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
    result = await agent.rollback("test_job_123", sample_payload)
    
    assert result["status"] == "success"
    assert "Rolled back" in result["message"]
    mock_ecs.update_service.assert_called_once_with(
        cluster=sample_payload["aws_config"]["ecs_cluster"],
        service=sample_payload["aws_config"]["service_name"],
        taskDefinition="arn:aws:ecs:us-east-1:123:task-definition/app-task:2",
        forceNewDeployment=True
    )

@patch("src.agents.deployops.agent.AWSClient")
@pytest.mark.asyncio
async def test_rollback_no_previous(mock_aws_client, agent, sample_payload):
    """Rollback fails if only one deployment exists."""
    # Mock ECS response with only 1 deployment
    mock_ecs = MagicMock()
    mock_ecs.describe_services.return_value = {
        "services": [{
            "deployments": [
                {
                    "taskDefinition": "arn:aws:ecs:us-east-1:123:task-definition/app-task:3",
                    "status": "PRIMARY"
                }
            ]
        }]
    }
    
    aws_instance = MagicMock()
    aws_instance.ecs.return_value = mock_ecs
    mock_aws_client.return_value = aws_instance
    
    result = await agent.rollback("test_job_123", sample_payload)
    assert result["status"] == "failed"
    assert "No previous deployment" in result["error"]


@patch("src.agents.deployops.agent.AWSClient")
@pytest.mark.asyncio
async def test_rollback_service_not_found(mock_aws_client, agent, sample_payload):
    """Rollback fails if service doesn't exist."""
    mock_ecs = MagicMock()
    mock_ecs.describe_services.side_effect = Exception("Service not found")
    
    aws_instance = MagicMock()
    aws_instance.ecs.return_value = mock_ecs
    mock_aws_client.return_value = aws_instance
    
    result = await agent.rollback("test_job_123", sample_payload)
    assert result["status"] == "failed"
    assert "error" in result


# ---------- Mocked Deploy Test (full flow with mocks) ----------

@pytest.mark.asyncio
@patch("asyncio.create_subprocess_shell")
@patch("asyncio.create_subprocess_exec")
@patch("src.agents.deployops.agent.AWSClient")
@patch("src.agents.deployops.agent.TerraformRunner")
async def test_deploy_full_success(mock_tf_runner, mock_aws_client, mock_create_subprocess_exec, mock_create_subprocess_shell, agent, sample_payload):
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

    # Mock subprocess calls
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_create_subprocess_shell.return_value = mock_proc
    mock_create_subprocess_exec.return_value = mock_proc

    # CREATE THE DIRECTORY FIRST
    workspace_dir = Path("/tmp/deployops/test_job_123")
    workspace_dir.mkdir(parents=True, exist_ok=True)

    with patch.object(agent, "health_check", new_callable=AsyncMock, return_value=True):
        result = await agent.deploy(sample_payload)

    assert result["status"] == "success"
    assert result["job_id"] == "test_job_123"
    assert result["deployed_url"] == "http://test.com"


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_shell")
@patch("asyncio.create_subprocess_exec")
@patch("src.agents.deployops.agent.AWSClient")
@patch("src.agents.deployops.agent.TerraformRunner")
async def test_deploy_health_check_fails_calls_rollback(mock_tf_runner, mock_aws_client, mock_create_subprocess_exec, mock_create_subprocess_shell, agent, sample_payload):
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

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_create_subprocess_shell.return_value = mock_proc
    mock_create_subprocess_exec.return_value = mock_proc

    workspace_dir = Path("/tmp/deployops/test_job_123")
    workspace_dir.mkdir(parents=True, exist_ok=True)

    with patch.object(agent, "health_check", new_callable=AsyncMock, return_value=False):
        with patch.object(agent, "rollback", new_callable=AsyncMock, return_value={"status": "success", "message": "rolled back"}) as mock_rollback:
            result = await agent.deploy(sample_payload)

    assert result["status"] == "failed"
    assert result["error"] == "health check failed"
    mock_rollback.assert_called_once()


# ---------- Additional Edge Cases ----------

@pytest.mark.asyncio
async def test_deploy_not_approved(agent, sample_payload):
    """Deploy returns early if not approved."""
    payload = sample_payload.copy()
    payload["approval"]["deploy_approved"] = False
    result = await agent.deploy(payload)
    assert result["status"] == "failed"
    assert "not approved" in result["error"]