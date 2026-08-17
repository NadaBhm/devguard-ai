"""
    mock tests of deployOps before actual  AWS testing

"""




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
    """Regression (live rakcha14): translate_infracost_to_deploy_payload emits a
    FLAT deployment_config (health_check_path="/", health_check_port=8000) that
    the refiner already corrected to match the app. _normalize_payload must not
    clobber it with the nested-ec2 default of 8080 + "/health", or DeployOps
    probes the wrong port/path, times out, and rolls back a healthy deploy."""
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
    """Every docker command must run with BuildKit enabled. The legacy builder
    cannot pull multi-arch images for a multi-stage Dockerfile built with
    --platform linux/amd64 when a base image is already cached for another
    platform (e.g. arm64 on Apple Silicon) — it dies with "image ... does not
    provide the specified platform". DOCKER_BUILDKIT=1 fixes the cross-platform
    multi-stage pull/build (confirmed live with the exact DeployOps
    Dockerfile)."""
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
    # The throwaway DOCKER_CONFIG dir must expose the real cli-plugins (buildx).
    # Otherwise `docker buildx build --load` fails from the backend subprocess
    # with "unknown command: docker buildx" while working fine interactively.
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
