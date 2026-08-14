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
    """Temporary workspace for artifact writing."""
    return tmp_path / "deployops" / "test_job_123"


# ---------- Validation Tests ----------

def test_sanitize_and_validate_valid(agent, sample_payload):
    result = agent.sanitize_and_validate(sample_payload)
    assert result["job_id"] == "test_job_123"
    assert "main.tf" in result["artifacts"]["terraform"]["files"]
    assert len(result["artifacts"]["docker_images"]) == 1
    assert result["artifacts"]["docker_images"][0]["name"] == "test-app"
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


# ---------- Artifact Writing Tests ----------

@pytest.mark.asyncio
async def test_write_artifacts(agent, sample_payload, workspace):
    from src.agents.deployops.models import Artifacts
    artifacts = Artifacts(**sample_payload["artifacts"])
    
    # Mock the modules copy to avoid overwriting test files
    with patch("shutil.copytree") as mock_copytree:
        # Make copytree do nothing
        mock_copytree.return_value = None
        
        await agent._write_artifacts(artifacts, workspace)

    # Check files exist
    tf_dir = workspace / "terraform"
    assert (tf_dir / "main.tf").exists()
    assert (tf_dir / "variables.tf").exists()
    # Dockerfile should be in the context directory
    assert (workspace / "test_context" / "Dockerfile").exists()

    # Check content
    content = (tf_dir / "main.tf").read_text()
    assert "aws_s3_bucket" in content

    # Variables file should exist if variables provided
    assert (tf_dir / "terraform.tfvars.json").exists()
    vars_content = json.loads((tf_dir / "terraform.tfvars.json").read_text())
    assert vars_content["region"] == "us-east-1"


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
        "db_host": "db.devguard.internal",
        "db_port": 5432,
        "db_name": "devguard",
        "db_user": "devguard",
        "db_password": "s3cret",
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


# ---------- Health Check Tests ----------

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
    tf_instance.output.return_value = {"frontend_url": {"value": "http://test.com"}}
    mock_tf_runner.return_value = tf_instance

    # Mock AWSClient
    aws_instance = MagicMock()
    aws_instance.get_account_id.return_value = "123456789012"
    mock_aws_client.return_value = aws_instance

    # Mock ECR authorization token (agent base64-decodes it for docker login)
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

    # Mock subprocess calls
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_create_subprocess_shell.return_value = mock_proc
    mock_create_subprocess_exec.return_value = mock_proc

    # CREATE THE DIRECTORY FIRST
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
async def test_deploy_health_check_fails_calls_rollback(mock_tf_runner, mock_aws_client, mock_create_subprocess_exec, mock_create_subprocess_shell, agent, sample_payload):
    """Test deploy when health check fails -> rollback called."""
    # Setup similar mocks
    tf_instance = MagicMock()
    tf_instance.init.return_value = True
    tf_instance.plan.return_value = {"planned": "changes"}
    tf_instance.apply.return_value = True
    tf_instance.output.return_value = {"frontend_url": {"value": "http://test.com"}}
    mock_tf_runner.return_value = tf_instance

    aws_instance = MagicMock()
    aws_instance.get_account_id.return_value = "123456789012"
    mock_aws_client.return_value = aws_instance

    # Mock ECR authorization token (agent base64-decodes it for docker login)
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


# ---------- Additional Edge Cases ----------

@pytest.mark.asyncio
async def test_deploy_not_approved(agent, sample_payload):
    """Deploy returns early if not approved."""
    payload = sample_payload.copy()
    payload["approval"]["deploy_approved"] = False
    result = await agent.deploy(payload)
    assert result["status"] == "failed"
    assert "not approved" in result["error"]


# ---------- moto-grounded AWS tests (real boto3 against moto) ----------
#
# These run the agent's real AWS client code against moto's in-memory AWS.
# They replace the MagicMock-based unit tests above for the AWS boundaries
# that the real pipeline touches (ECR auth, STS account, ECS revisions,
# promote, rollback fallback). Waiters are intentionally avoided because
# moto never reaches a stable service state.

@pytest.fixture
def moto_ecs():
    """Set up a real (moto-backed) VPC + ECS cluster + task definitions + service."""
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
    """Real STS call returns moto's sandbox account id."""
    from src.agents.deployops.agent import AWSClient
    with mock_aws():
        assert AWSClient(region="us-east-1").get_account_id() == "123456789012"


@pytest.mark.asyncio
async def test_moto_ecr_authorization_token(agent):
    """Real ECR get_authorization_token returns a base64 token for docker login."""
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
    """list_revisions reads the real ECS task-definition history, flagging the current one."""
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
    """promote pushes the source service's PRIMARY task definition onto the target."""
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
    """rollback returns a clean failure when there is no prior task-definition revision."""
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
    """rollback falls back to the prior registered task-definition revision and updates the service.

    A single deployment plus a second registered revision exercises the
    list_task_definitions fallback path. The waiter is stubbed since moto
    never reports a stable service.
    """
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
