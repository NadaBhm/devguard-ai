"""Mocked tests of DeployOps, run before live AWS testing."""


import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
import httpx
from moto import mock_aws
import boto3

from src.agents.deployops.agent import DeployOpsAgent


@pytest.fixture
def sample_payload():
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
            "docker_images": [
                {
                    "name": "test-app",
                    "dockerfile": "FROM python:3.12-slim\nCOPY . /app",
                    "context": "test_context",
                    "tag": "latest",
                    "platform": "linux/amd64"
                }
            ],
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
    return tmp_path / "deployops" / "test_job_123"


def test_sanitize_and_validate_valid(agent, sample_payload):
    result = agent.sanitize_and_validate(sample_payload)
    assert result["job_id"] == "test_job_123"
    assert "main.tf" in result["artifacts"]["terraform"]["files"]
    assert len(result["artifacts"]["docker_images"]) == 1
    assert result["artifacts"]["docker_images"][0]["name"] == "test-app"
    assert result["aws_config"]["region"] == "us-east-1"


def test_sanitize_and_validate_missing_field(agent):
    payload = {"job_id": "test"}
    with pytest.raises(ValueError, match="Missing required field: 'artifacts'"):
        agent.sanitize_and_validate(payload)


def test_sanitize_and_validate_invalid_job_id(agent, sample_payload):
    payload = sample_payload.copy()
    payload["job_id"] = "job with spaces"
    with pytest.raises(ValueError, match="job_id contains invalid characters"):
        agent.sanitize_and_validate(payload)


def test_sanitize_and_validate_invalid_region(agent, sample_payload):
    payload = sample_payload.copy()
    payload["aws_config"]["region"] = "eu-central-1a"
    with pytest.raises(ValueError, match="Invalid AWS region"):
        agent.sanitize_and_validate(payload)


def test_sanitize_and_validate_invalid_docker_tag(agent, sample_payload):
    payload = sample_payload.copy()
    payload["artifacts"]["docker_images"][0]["tag"] = "latest version"
    with pytest.raises(ValueError, match="docker_image.tag contains invalid characters"):
        agent.sanitize_and_validate(payload)


def test_sanitize_and_validate_invalid_docker_context(agent, sample_payload):
    payload = sample_payload.copy()
    payload["artifacts"]["docker_images"][0]["context"] = "../outside"
    with pytest.raises(ValueError, match="docker_image.context must be a safe relative path"):
        agent.sanitize_and_validate(payload)


def test_sanitize_and_validate_ec2_skips_ecs_requirements(agent, sample_payload):
    payload = sample_payload.copy()
    payload["compute_type"] = "ec2"
    payload["aws_config"] = {
        "region": "us-east-1",
        "ecs_cluster": None,
        "service_name": None,
    }
    result = agent.sanitize_and_validate(payload)
    assert result["compute_type"] == "ec2"
    assert result["aws_config"]["ecs_cluster"] is None
    assert result["aws_config"]["service_name"] is None


def test_sanitize_and_validate_s3_skips_ecs_requirements(agent, sample_payload):
    payload = sample_payload.copy()
    payload["compute_type"] = "s3"
    payload["aws_config"] = {
        "region": "us-east-1",
        "ecs_cluster": None,
        "service_name": None,
        "bucket_name": "devguard-static-abc",
    }
    payload["artifacts"]["docker_images"] = []
    result = agent.sanitize_and_validate(payload)
    assert result["compute_type"] == "s3"
    assert result["aws_config"]["ecs_cluster"] is None
    assert result["aws_config"]["service_name"] is None


def test_sanitize_and_validate_ecs_still_requires_cluster(agent, sample_payload):
    payload = sample_payload.copy()
    payload["aws_config"]["ecs_cluster"] = None
    with pytest.raises(ValueError, match="aws_config.ecs_cluster must be a non-empty string"):
        agent.sanitize_and_validate(payload)


def test_normalize_payload_preserves_deploy_approved(agent, sample_payload):
    payload = sample_payload.copy()
    payload["compute_type"] = "ec2"
    payload["approval"] = {"deploy_approved": True, "approved_by": "test@example.com"}
    payload["aws_config"] = {
        "region": "us-east-1",
        "ecs_cluster": None,
        "service_name": None,
    }
    result = agent.sanitize_and_validate(payload)
    assert result["approval"]["deploy_approved"] is True


def test_normalize_payload_ec2_preserves_flat_health_check(agent, sample_payload):
    """Regression (live rakcha14): translate_infracost_to_deploy_payload emits a FLAT
    deployment_config ("/" + 8000) the refiner already corrected to match the app;
    _normalize_payload must not clobber it with the nested-ec2 default (8080 + "/health") or
    DeployOps probes the wrong port/path, times out, and rolls back a healthy deploy."""
    payload = sample_payload.copy()
    payload["compute_type"] = "ec2"
    payload["aws_config"] = {
        "region": "us-east-1",
        "ecs_cluster": None,
        "service_name": None,
    }
    payload["deployment_config"] = {
        "strategy": "rolling",
        "health_check_path": "/",
        "health_check_port": 8000,
        "timeout_minutes": 5,
        "auto_rollback": True,
    }
    result = agent.sanitize_and_validate(payload)
    assert result["deployment_config"]["health_check_path"] == "/"
    assert result["deployment_config"]["health_check_port"] == 8000


def test_normalize_payload_ec2_nested_config_still_works(agent, sample_payload):
    """Nested deployment_config.ec2 shape (the older raw InfraCost contract)
    must still override the flat defaults."""
    payload = sample_payload.copy()
    payload["compute_type"] = "ec2"
    payload["aws_config"] = {
        "region": "us-east-1",
        "ecs_cluster": None,
        "service_name": None,
    }
    payload["deployment_config"] = {
        "ec2": {
            "strategy": "rolling",
            "health_check_path": "/healthz",
            "health_check_port": 9000,
            "timeout_minutes": 7,
        }
    }
    result = agent.sanitize_and_validate(payload)
    assert result["deployment_config"]["health_check_path"] == "/healthz"
    assert result["deployment_config"]["health_check_port"] == 9000


def test_normalize_payload_ecs_preserves_flat_health_check(agent, sample_payload):
    """Regression (live E2E): translate_infracost_to_deploy_payload emits a FLAT
    deployment_config ("/" + 3000) for ECS too; _normalize_payload's ECS branch must not clobber
    it with the nested-ecs default (8080 + "/health") -- jupyter's healthy app on "/" was probed
    at "/health", timed out, and rolled back a live deployment."""
    payload = sample_payload.copy()
    payload["compute_type"] = "ecs"
    payload["deployment_config"] = {
        "strategy": "rolling",
        "health_check_path": "/",
        "health_check_port": 3000,
        "timeout_minutes": 5,
        "auto_rollback": True,
    }
    result = agent.sanitize_and_validate(payload)
    assert result["deployment_config"]["health_check_path"] == "/"
    assert result["deployment_config"]["health_check_port"] == 3000


def test_normalize_payload_ecs_nested_config_still_works(agent, sample_payload):
    """Nested deployment_config.ecs shape (the older raw InfraCost contract)
    must still override the flat defaults."""
    payload = sample_payload.copy()
    payload["compute_type"] = "ecs"
    payload["deployment_config"] = {
        "ecs": {
            "strategy": "rolling",
            "health_check_path": "/healthz",
            "health_check_port": 9000,
            "timeout_minutes": 7,
        }
    }
    result = agent.sanitize_and_validate(payload)
    assert result["deployment_config"]["health_check_path"] == "/healthz"
    assert result["deployment_config"]["health_check_port"] == 9000


@pytest.mark.asyncio
async def test_write_artifacts(agent, sample_payload, workspace):
    from src.agents.deployops.models import Artifacts
    artifacts = Artifacts(**sample_payload["artifacts"])
    
    with patch("shutil.copytree") as mock_copytree:
        mock_copytree.return_value = None
        
        await agent._write_artifacts(artifacts, workspace)

    tf_dir = workspace / "terraform"
    assert (tf_dir / "main.tf").exists()
    assert (tf_dir / "variables.tf").exists()
    assert (workspace / "test_context" / "Dockerfile").exists()

    content = (tf_dir / "main.tf").read_text()
    assert "aws_s3_bucket" in content

    assert (tf_dir / "terraform.tfvars.json").exists()
    vars_content = json.loads((tf_dir / "terraform.tfvars.json").read_text())
    assert vars_content["region"] == "us-east-1"


async def test_write_artifacts_multi_container_writes_each_dockerfile(agent, sample_payload, workspace):
    from src.agents.deployops.models import Artifacts
    sample_payload["artifacts"]["docker_images"] = [
        {
            "name": "test-app",
            "dockerfile": "FROM python:3.12-slim\nCOPY . /app",
            "context": "backend",
            "tag": "latest",
            "platform": "linux/amd64",
        },
        {
            "name": "test-app-frontend",
            "dockerfile": "FROM nginx:1.27\nCOPY . /usr/share/nginx/html",
            "context": "frontend",
            "tag": "latest",
            "platform": "linux/amd64",
        },
    ]
    artifacts = Artifacts(**sample_payload["artifacts"])

    with patch("shutil.copytree") as mock_copytree:
        mock_copytree.return_value = None
        await agent._write_artifacts(artifacts, workspace)

    backend_df = workspace / "backend" / "Dockerfile"
    frontend_df = workspace / "frontend" / "Dockerfile"
    assert backend_df.exists()
    assert frontend_df.exists()
    assert "python:3.12-slim" in backend_df.read_text()
    assert "nginx:1.27" in frontend_df.read_text()


def test_terraform_env_vars_mapping(monkeypatch):
    from src.agents.deployops.agent import _terraform_env_vars

    monkeypatch.delenv("DEVGUARD_VPC_ID", raising=False)
    monkeypatch.delenv("DEVGUARD_SUBNET_IDS", raising=False)
    monkeypatch.delenv("DEVGUARD_DB_HOST", raising=False)
    monkeypatch.delenv("DEVGUARD_DB_PORT", raising=False)
    monkeypatch.delenv("DEVGUARD_DB_NAME", raising=False)
    monkeypatch.delenv("DEVGUARD_DB_USER", raising=False)
    monkeypatch.delenv("DEVGUARD_DB_PASSWORD", raising=False)
    assert _terraform_env_vars() == {}

    monkeypatch.setenv("DEVGUARD_VPC_ID", "vpc-0123456789abcdef0")
    monkeypatch.setenv("DEVGUARD_SUBNET_IDS", "subnet-a, subnet-b")
    monkeypatch.setenv("DEVGUARD_DB_HOST", "db.devguard.internal")
    monkeypatch.setenv("DEVGUARD_DB_PORT", "5432")
    monkeypatch.setenv("DEVGUARD_DB_NAME", "devguard")
    monkeypatch.setenv("DEVGUARD_DB_USER", "devguard")
    monkeypatch.setenv("DEVGUARD_DB_PASSWORD", "s3cret")

    mapped = _terraform_env_vars()
    assert mapped == {
        "vpc_id": "vpc-0123456789abcdef0",
        "subnet_ids": ["subnet-a", "subnet-b"],
        "subnet_id": "subnet-a",
        "db_host": "db.devguard.internal",
        "db_port": 5432,
        "db_name": "devguard",
        "db_user": "devguard",
        "db_password": "s3cret",
        # standing sandbox DB present -> skip RDS provisioning
        "create_db": False,
    }


@pytest.mark.asyncio
async def test_write_artifacts_merges_env_tfvars(agent, sample_payload, workspace, monkeypatch):
    from src.agents.deployops.models import Artifacts

    monkeypatch.setenv("DEVGUARD_VPC_ID", "vpc-0abcdef1234567890")
    monkeypatch.setenv("DEVGUARD_SUBNET_IDS", "subnet-1,subnet-2")

    artifacts = Artifacts(**sample_payload["artifacts"])
    with patch("shutil.copytree") as mock_copytree:
        mock_copytree.return_value = None
        await agent._write_artifacts(artifacts, workspace)

    tf_dir = workspace / "terraform"
    vars_content = json.loads((tf_dir / "terraform.tfvars.json").read_text())
    assert vars_content["region"] == "us-east-1"
    assert vars_content["vpc_id"] == "vpc-0abcdef1234567890"
    assert vars_content["subnet_ids"] == ["subnet-1", "subnet-2"]
    assert vars_content["subnet_id"] == "subnet-1"


@pytest.mark.asyncio
@patch("httpx.AsyncClient.get", new_callable=AsyncMock)
async def test_health_check_success(mock_get, agent):
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_get.return_value = mock_response

    result = await agent.health_check("http://localhost:8080", max_retries=2, timeout=1)
    assert result["passed"] is True
    assert result["status_code"] == 200
    mock_get.assert_called_with("http://localhost:8080/health")


@pytest.mark.asyncio
@patch("httpx.AsyncClient.get", new_callable=AsyncMock)
async def test_health_check_fails_http_error(mock_get, agent):
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_get.return_value = mock_response

    result = await agent.health_check("http://localhost:8080", max_retries=1)
    assert result["passed"] is False
    assert result["status_code"] == 500


@pytest.mark.asyncio
@patch("httpx.AsyncClient.get", new_callable=AsyncMock)
async def test_health_check_timeout(mock_get, agent):
    mock_get.side_effect = httpx.TimeoutException("timeout")
    result = await agent.health_check("http://localhost:8080", max_retries=1)
    assert result["passed"] is False
    assert result["status_code"] == 0


@pytest.mark.asyncio
async def test_health_check_missing_url(agent):
    result = await agent.health_check(None)
    assert result["passed"] is False
    assert result["status_code"] == 0


@pytest.mark.asyncio
@patch("src.agents.deployops.agent.AWSClient")
async def test_rollback_success(mock_aws_client, agent, sample_payload):
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
    
    mock_ecs.update_service.return_value = {}
    
    mock_waiter = MagicMock()
    mock_ecs.get_waiter.return_value = mock_waiter
    
    aws_instance = MagicMock()
    aws_instance.ecs.return_value = mock_ecs
    mock_aws_client.return_value = aws_instance
    
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
    mock_ecs = MagicMock()
    mock_ecs.describe_services.side_effect = Exception("Service not found")
    
    aws_instance = MagicMock()
    aws_instance.ecs.return_value = mock_ecs
    mock_aws_client.return_value = aws_instance
    
    result = await agent.rollback("test_job_123", sample_payload)
    assert result["status"] == "failed"
    assert "error" in result


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_shell")
@patch("asyncio.create_subprocess_exec")
@patch("src.agents.deployops.agent.AWSClient")
@patch("src.agents.deployops.agent.TerraformRunner")
async def test_deploy_full_success(mock_tf_runner, mock_aws_client, mock_create_subprocess_exec, mock_create_subprocess_shell, agent, sample_payload):
    # Mock preflight to always pass
    agent._preflight_check = AsyncMock(return_value=True)
    tf_instance = MagicMock()
    tf_instance.init.return_value = True
    tf_instance.plan.return_value = {"planned": "changes"}
    tf_instance.apply.return_value = True
    tf_instance.output.return_value = {"frontend_url": {"value": "http://test.com"}}
    mock_tf_runner.return_value = tf_instance

    aws_instance = MagicMock()
    aws_instance.get_account_id.return_value = "123456789012"
    mock_aws_client.return_value = aws_instance

    ecr_client = MagicMock()
    ecr_client.get_authorization_token.return_value = {
        "authorizationData": [
            {
                "authorizationToken": "QVdTOmZha2V0b2tlbg==",
                "proxyEndpoint": "https://123456789012.dkr.ecr.us-east-1.amazonaws.com",
            }
        ]
    }
    aws_instance.session.client.return_value = ecr_client

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_create_subprocess_shell.return_value = mock_proc
    mock_create_subprocess_exec.return_value = mock_proc

    workspace_dir = Path("/tmp/deployops/test_job_123")
    workspace_dir.mkdir(parents=True, exist_ok=True)

    with patch.object(agent, "health_check", new_callable=AsyncMock, return_value={"passed": True}):
        result = await agent.deploy(sample_payload)

    assert result["status"] == "success"
    assert result["job_id"] == "test_job_123"
    assert result["deployed_url"] == "http://test.com"


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_shell")
@patch("asyncio.create_subprocess_exec")
@patch("src.agents.deployops.agent.AWSClient")
@patch("src.agents.deployops.agent.TerraformRunner")
@patch("src.lib.repo.clone_repo")
async def test_deploy_clones_repo_into_workspace(mock_clone, mock_tf_runner, mock_aws_client, mock_create_subprocess_exec, mock_create_subprocess_shell, agent, sample_payload):
    agent._preflight_check = AsyncMock(return_value=True)
    """A payload carrying metadata.repo_url must clone the source into the
    build workspace, otherwise a real image build has no package.json and dies
    (npm ci)."""
    tf_instance = MagicMock()
    tf_instance.init.return_value = True
    tf_instance.plan.return_value = {"planned": "changes"}
    tf_instance.apply.return_value = True
    tf_instance.output.return_value = {"frontend_url": {"value": "http://test.com"}}
    mock_tf_runner.return_value = tf_instance

    aws_instance = MagicMock()
    aws_instance.get_account_id.return_value = "123456789012"
    mock_aws_client.return_value = aws_instance

    ecr_client = MagicMock()
    ecr_client.get_authorization_token.return_value = {
        "authorizationData": [
            {
                "authorizationToken": "QVdTOmZha2V0b2tlbg==",
                "proxyEndpoint": "https://123456789012.dkr.ecr.us-east-1.amazonaws.com",
            }
        ]
    }
    aws_instance.session.client.return_value = ecr_client

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_create_subprocess_shell.return_value = mock_proc
    mock_create_subprocess_exec.return_value = mock_proc

    sample_payload["metadata"] = {"repo_url": "https://github.com/owner/repo"}
    workspace_dir = Path("/tmp/deployops/test_job_123")
    workspace_dir.mkdir(parents=True, exist_ok=True)

    with patch.object(agent, "health_check", new_callable=AsyncMock, return_value={"passed": True}):
        result = await agent.deploy(sample_payload)

    assert result["status"] == "success"
    mock_clone.assert_called_once()
    args, kwargs = mock_clone.call_args
    assert args[0] == "https://github.com/owner/repo"
    assert Path(args[1]).resolve() == workspace_dir.resolve()


@pytest.mark.asyncio
@patch("asyncio.create_subprocess_shell")
@patch("asyncio.create_subprocess_exec")
@patch("src.agents.deployops.agent.AWSClient")
@patch("src.agents.deployops.agent.TerraformRunner")
async def test_deploy_health_check_fails_calls_rollback(mock_tf_runner, mock_aws_client, mock_create_subprocess_exec, mock_create_subprocess_shell, agent, sample_payload):
    agent._preflight_check = AsyncMock(return_value=True)
    tf_instance = MagicMock()
    tf_instance.init.return_value = True
    tf_instance.plan.return_value = {"planned": "changes"}
    tf_instance.apply.return_value = True
    tf_instance.output.return_value = {"frontend_url": {"value": "http://test.com"}}
    mock_tf_runner.return_value = tf_instance

    aws_instance = MagicMock()
    aws_instance.get_account_id.return_value = "123456789012"
    mock_aws_client.return_value = aws_instance

    ecr_client = MagicMock()
    ecr_client.get_authorization_token.return_value = {
        "authorizationData": [
            {
                "authorizationToken": "QVdTOmZha2V0b2tlbg==",
                "proxyEndpoint": "https://123456789012.dkr.ecr.us-east-1.amazonaws.com",
            }
        ]
    }
    aws_instance.session.client.return_value = ecr_client

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_create_subprocess_shell.return_value = mock_proc
    mock_create_subprocess_exec.return_value = mock_proc

    workspace_dir = Path("/tmp/deployops/test_job_123")
    workspace_dir.mkdir(parents=True, exist_ok=True)

    with patch.object(agent, "health_check", new_callable=AsyncMock, return_value={"passed": False}):
        with patch.object(agent, "rollback", new_callable=AsyncMock, return_value={"status": "success", "message": "rolled back"}) as mock_rollback:
            result = await agent.deploy(sample_payload)

    assert result["status"] == "failed"
    assert result["error"] == "health check failed"
    mock_rollback.assert_called_once()


@pytest.mark.asyncio
async def test_deploy_not_approved(agent, sample_payload):
    payload = sample_payload.copy()
    payload["approval"]["deploy_approved"] = False
    result = await agent.deploy(payload)
    assert result["status"] == "failed"
    assert "not approved" in result["error"]


# Real boto3 against moto for the AWS boundaries the pipeline touches.
# Waiters are avoided because moto never reaches a stable service state.

@pytest.fixture
def moto_ecs():
    with mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
        subnet = ec2.create_subnet(VpcId=vpc, CidrBlock="10.0.1.0/24")["Subnet"]["SubnetId"]
        sg = ec2.create_security_group(GroupName="sg1", Description="sg", VpcId=vpc)["GroupId"]

        ecs = boto3.client("ecs", region_name="us-east-1")
        ecs.create_cluster(clusterName="moto-cluster")

        def _td(family):
            return {
                "family": family,
                "containerDefinitions": [
                    {"name": "web", "image": "nginx:latest", "memory": 256, "cpu": 128}
                ],
                "cpu": "256",
                "memory": "512",
                "networkMode": "awsvpc",
                "requiresCompatibilities": ["FARGATE"],
            }

        def _service(cluster, name, family):
            ecs.create_service(
                cluster=cluster,
                serviceName=name,
                taskDefinition=family,
                desiredCount=1,
                launchType="FARGATE",
                networkConfiguration={
                    "awsvpcConfiguration": {"subnets": [subnet], "securityGroups": [sg]}
                },
            )

        yield {
            "ecs": ecs,
            "cluster": "moto-cluster",
            "network": {"awsvpcConfiguration": {"subnets": [subnet], "securityGroups": [sg]}},
            "register_td": _td,
            "create_service": _service,
        }


@pytest.mark.asyncio
async def test_moto_get_account_id(agent):
    from src.agents.deployops.agent import AWSClient
    with mock_aws():
        assert AWSClient(region="us-east-1").get_account_id() == "123456789012"


@pytest.mark.asyncio
async def test_moto_ecr_authorization_token(agent):
    import base64

    from src.agents.deployops.agent import AWSClient

    with mock_aws():
        client = AWSClient(region="us-east-1")
        data = client.session.client("ecr").get_authorization_token()["authorizationData"][0]
        assert data["proxyEndpoint"] == "https://123456789012.dkr.ecr.us-east-1.amazonaws.com"
        token = base64.b64decode(data["authorizationToken"]).decode()
        assert token.startswith("AWS:")


@pytest.mark.asyncio
async def test_moto_list_revisions_real_ecs(agent, moto_ecs):
    ecs = moto_ecs["ecs"]
    ecs.register_task_definition(**moto_ecs["register_td"]("app-task"))
    ecs.register_task_definition(**moto_ecs["register_td"]("app-task"))
    ecs.create_cluster(clusterName="app-cluster")
    moto_ecs["create_service"]("app-cluster", "app-dev-web", "app-task")

    result = await agent.list_revisions("app", "dev", "web", region="us-east-1")

    assert result["status"] == "success"
    assert result["service"] == "app-dev-web"
    assert len(result["versions"]) == 2
    current = [v for v in result["versions"] if v["is_current"]]
    assert len(current) == 1
    assert current[0]["revision"] == 2
    assert {v["revision"] for v in result["versions"]} == {1, 2}


@pytest.mark.asyncio
async def test_moto_promote_copies_primary_revision(agent, moto_ecs):
    ecs = moto_ecs["ecs"]
    ecs.register_task_definition(**moto_ecs["register_td"]("app-task"))
    moto_ecs["create_service"]("moto-cluster", "app-dev-web", "app-task")
    ecs.create_cluster(clusterName="moto-cluster-prod")
    moto_ecs["create_service"]("moto-cluster-prod", "app-prod-web", "app-task")

    result = await agent.promote({
        "app_name": "app",
        "source_cluster": "moto-cluster",
        "source_service": "app-dev-web",
        "target_cluster": "moto-cluster-prod",
        "target_service": "app-prod-web",
        "region": "us-east-1",
    })

    assert result["status"] == "success"
    assert "app-task" in result["source_task_definition"]
    assert result["target_service"] == "app-prod-web"


@pytest.mark.asyncio
async def test_moto_rollback_no_previous_real_ecs(agent, moto_ecs, sample_payload):
    ecs = moto_ecs["ecs"]
    ecs.register_task_definition(**moto_ecs["register_td"]("app-task"))
    moto_ecs["create_service"]("moto-cluster", "test-service", "app-task")

    payload = sample_payload.copy()
    payload["aws_config"]["ecs_cluster"] = "moto-cluster"
    payload["aws_config"]["service_name"] = "test-service"

    result = await agent.rollback("test_job_123", payload)

    assert result["status"] == "failed"
    assert "No previous deployment to rollback to" in result["error"]


@pytest.mark.asyncio
async def test_moto_rollback_uses_prior_revision(agent, moto_ecs, sample_payload):
    """Exercises the list_task_definitions fallback; waiter stubbed since moto never stabilizes."""
    ecs = moto_ecs["ecs"]
    ecs.register_task_definition(**moto_ecs["register_td"]("app-task"))
    ecs.register_task_definition(**moto_ecs["register_td"]("app-task"))
    moto_ecs["create_service"]("moto-cluster", "test-service", "app-task")

    payload = sample_payload.copy()
    payload["aws_config"]["ecs_cluster"] = "moto-cluster"
    payload["aws_config"]["service_name"] = "test-service"

    real_ecs = boto3.client("ecs", region_name="us-east-1")
    real_ecs.get_waiter = MagicMock(return_value=MagicMock(wait=MagicMock()))

    with patch("src.agents.deployops.agent.AWSClient.ecs", return_value=real_ecs):
        result = await agent.rollback("test_job_123", payload)

    assert result["status"] == "success"
    assert "Rolled back to" in result["message"]


@pytest.mark.asyncio
async def test_run_docker_cmd_forces_buildkit(agent, monkeypatch, tmp_path):
    """Every docker command must run with BuildKit: the legacy builder cannot pull multi-arch
    images for a multi-stage Dockerfile with --platform linux/amd64 when a base image is cached
    for another platform ("does not provide the specified platform"); DOCKER_BUILDKIT=1 fixes it."""
    docker_config = str(tmp_path / "docker-config")
    Path(docker_config).mkdir(parents=True, exist_ok=True)
    captured_env = {}

    async def fake_exec(*cmd, env=None, **kwargs):
        captured_env.update(env or {})
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"", b""))
        return proc

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    code, out, err = await agent._run_docker_cmd(
        ["docker", "build", "-t", "x:y", "."],
        docker_config=docker_config,
    )

    assert code == 0
    assert captured_env.get("DOCKER_BUILDKIT") == "1"
    assert captured_env.get("DOCKER_CONFIG") == docker_config
    # The throwaway DOCKER_CONFIG dir must expose the real cli-plugins (buildx); otherwise
    # backend-subprocess `docker buildx build --load` dies with "unknown command".
    plugins = Path(docker_config) / "cli-plugins"
    assert plugins.is_symlink(), "cli-plugins must be symlinked into DOCKER_CONFIG"
    assert plugins.resolve().exists(), "cli-plugins symlink must point at the real dir"
    assert plugins.resolve().name == "cli-plugins"


def test_prepare_docker_config_creates_config_and_links_plugins(tmp_path):
    from src.agents.deployops.agent import DeployOpsAgent
    cfg_dir = tmp_path / "docker-config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    config_json = cfg_dir / "config.json"
    assert not config_json.exists()
    DeployOpsAgent()._prepare_docker_config(str(cfg_dir))
    assert config_json.exists()
    assert (cfg_dir / "cli-plugins").is_symlink()


@pytest.mark.asyncio
async def test_health_check_falls_back_to_root_when_configured_path_404s():
    """Regression (live E2E): the app serves "/" but DeployOps probed the
    configured path first and rolled back a healthy deploy. The multi-path
    probe must pass on the first candidate that returns 200."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    ok_paths = {"/"}
    recorded = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            recorded.append(self.path)
            self.send_response(200 if self.path in ok_paths else 404)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), H)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        agent = DeployOpsAgent()
        result = await agent.health_check(
            f"http://127.0.0.1:{server.server_port}",
            max_retries=2,
            timeout=10,
            health_check_path="/health",
            retry_delay=0,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result["passed"] is True
    assert result["status_code"] == 200
    assert result["health_check_path"] == "/"
    assert "/health" in recorded, "configured path must be tried first"
    assert "/" in recorded, "fallback candidate must be reached"


@pytest.mark.asyncio
async def test_health_check_passes_on_standard_health_route():
    """An app that only exposes /healthz (not /) must still pass."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    ok_paths = {"/healthz"}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path in ok_paths else 404)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), H)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        agent = DeployOpsAgent()
        result = await agent.health_check(
            f"http://127.0.0.1:{server.server_port}",
            max_retries=2,
            timeout=10,
            health_check_path="/",
            retry_delay=0,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result["passed"] is True
    assert result["status_code"] == 200
    assert result["health_check_path"] == "/healthz"


@pytest.mark.asyncio
async def test_health_check_fails_when_all_candidates_404():
    """No candidate responds 200 -> every path exhausts retries -> fail."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), H)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        agent = DeployOpsAgent()
        result = await agent.health_check(
            f"http://127.0.0.1:{server.server_port}",
            max_retries=2,
            timeout=10,
            health_check_path="/health",
            retry_delay=0,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert result["passed"] is False
    assert result["status_code"] == 404


def test_db_check_returns_clear_error_when_db_required_but_env_missing(
    tmp_path, monkeypatch
):
    """ECS template declares required db_* vars (no defaults) whenever a database is detected;
    without DEVGUARD_DB_* set, terraform plan fails on a cryptic "required variable" error after
    a full build/push. _check_db_vars_available must fail fast with a clear message."""
    for env in ("DEVGUARD_DB_HOST", "DEVGUARD_DB_PORT", "DEVGUARD_DB_NAME",
                "DEVGUARD_DB_USER", "DEVGUARD_DB_PASSWORD"):
        monkeypatch.delenv(env, raising=False)

    tf_dir = tmp_path / "terraform"
    tf_dir.mkdir(parents=True, exist_ok=True)
    (tf_dir / "variables.tf").write_text(
        'variable "db_host" {\n  description = "Hostname of an existing database"\n  type = string\n}\n'
    )

    err = DeployOpsAgent._check_db_vars_available(tf_dir)
    assert err is not None
    assert "database" in err
    assert "DEVGUARD_DB_" in err
    assert "cannot run without it" in err


def test_db_check_none_when_all_env_present(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVGUARD_DB_HOST", "db.example.com")
    monkeypatch.setenv("DEVGUARD_DB_PORT", "5432")
    monkeypatch.setenv("DEVGUARD_DB_NAME", "appdb")
    monkeypatch.setenv("DEVGUARD_DB_USER", "appuser")
    monkeypatch.setenv("DEVGUARD_DB_PASSWORD", "secret")

    tf_dir = tmp_path / "terraform"
    tf_dir.mkdir(parents=True, exist_ok=True)
    (tf_dir / "variables.tf").write_text(
        'variable "db_host" {\n  type = string\n}\n'
    )

    assert DeployOpsAgent._check_db_vars_available(tf_dir) is None


def test_db_check_none_when_no_db_in_template(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVGUARD_DB_HOST", raising=False)
    tf_dir = tmp_path / "terraform"
    tf_dir.mkdir(parents=True, exist_ok=True)
    (tf_dir / "variables.tf").write_text(
        'variable "vpc_id" {\n  type = string\n}\n'
    )
    assert DeployOpsAgent._check_db_vars_available(tf_dir) is None


def test_db_check_none_when_variables_tf_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVGUARD_DB_HOST", raising=False)
    tf_dir = tmp_path / "terraform"
    tf_dir.mkdir(parents=True, exist_ok=True)
    assert DeployOpsAgent._check_db_vars_available(tf_dir) is None


def test_static_source_dir_prefers_build_output(tmp_path):
    """A repo with dist/public/_site holding index.html must sync the build
    output, not the whole workspace (which contains source, tests, config)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.jsx").write_text("export const App = () => <div/>")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "index.html").write_text("<html></html>")
    (tmp_path / "dist" / "main.js").write_text("console.log('hi')")
    assert DeployOpsAgent._static_source_dir(tmp_path) == tmp_path / "dist"


def test_static_source_dir_skips_empty_build_dir(tmp_path):
    """A build dir that exists but holds no index document is not the site."""
    (tmp_path / "dist").mkdir()
    assert DeployOpsAgent._static_source_dir(tmp_path) == tmp_path


def test_static_source_dir_falls_back_to_workspace(tmp_path):
    """Bare 'just HTML in a folder' repos have no build dir -> whole workspace."""
    (tmp_path / "index.html").write_text("<html></html>")
    assert DeployOpsAgent._static_source_dir(tmp_path) == tmp_path
