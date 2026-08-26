"""Regression tests for DeployOpsAgent._deploy_existing_ecs_revision.

This is the "update deployment" path: redeploy the current commit's code
onto an already-live ECS service, skipping Terraform entirely. Before the
fix this method left containerDefinitions[].image untouched -- these tests
assert the new image is actually shipped, and that a build/push failure
never touches the live service.

Uses moto to fake ECS (no real AWS account touched); docker build/push and
git clone are monkeypatched out since neither can run under moto.
"""

import os

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import boto3
import pytest
from moto import mock_aws

from src.agents.deployops.agent import DeployOpsAgent
from src.agents.deployops.models import (
    Artifacts,
    AWSConfig,
    DeployPayload,
    DockerImageConfig,
    TerraformArtifacts,
)

CLUSTER = "devguard-cluster"
SERVICE = "devguard-api"
OLD_IMAGE = "123456789012.dkr.ecr.us-east-1.amazonaws.com/app:old"
NEW_IMAGE = "123456789012.dkr.ecr.us-east-1.amazonaws.com/app:new"


def _seed_ecs_service(ecs) -> None:
    ec2 = boto3.client("ec2", region_name="us-east-1")
    vpc_id = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    subnet_id = ec2.create_subnet(VpcId=vpc_id, CidrBlock="10.0.0.0/24")["Subnet"]["SubnetId"]

    ecs.create_cluster(clusterName=CLUSTER)
    ecs.register_task_definition(
        family=f"{CLUSTER}-{SERVICE}",
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu="256",
        memory="512",
        containerDefinitions=[
            {"name": "app", "image": OLD_IMAGE, "portMappings": [{"containerPort": 80}]}
        ],
    )
    ecs.create_service(
        cluster=CLUSTER,
        serviceName=SERVICE,
        taskDefinition=f"{CLUSTER}-{SERVICE}",
        desiredCount=1,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": [subnet_id],
                "securityGroups": [],
                "assignPublicIp": "ENABLED",
            }
        },
    )


def _make_payload() -> DeployPayload:
    return DeployPayload(
        job_id="update-test-job",
        artifacts=Artifacts(
            terraform=TerraformArtifacts(files={"main.tf": "# unused on this path"}),
            docker_images=[
                DockerImageConfig(name="app", dockerfile="FROM scratch", context=".", tag="new")
            ],
        ),
        aws_config=AWSConfig(region="us-east-1", ecs_cluster=CLUSTER, service_name=SERVICE),
        metadata={"repo_url": "https://github.com/x/y", "ecs_update_only": True, "deployment_revision": "42"},
    )


async def _noop_write_artifacts(*args, **kwargs):
    return None


def _noop_clone_repo(repo_url, target_dir, **kwargs):
    from pathlib import Path
    Path(target_dir).mkdir(parents=True, exist_ok=True)
    return Path(target_dir)


class _NoopWaiter:
    def wait(self, **kwargs):
        return None


def _ecs_with_noop_waiter(self):
    """moto's ECS simulation never converges runningCount==desiredCount for
    a forceNewDeployment update, so the real `services_stable` waiter just
    polls for real (minutes) until MaxAttempts is exhausted. Everything else
    (describe/register/update calls) still goes through the real, moto-
    intercepted client -- only the wait-for-steady-state polling is faked
    out, since that part is infrastructure-simulation fidelity, not
    something this test is trying to verify."""
    import boto3 as _boto3
    client = _boto3.Session().client("ecs", region_name=self.region)
    real_get_waiter = client.get_waiter

    def get_waiter(name):
        return _NoopWaiter() if name == "services_stable" else real_get_waiter(name)

    client.get_waiter = get_waiter
    return client


@pytest.fixture
def deployops_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("DEPLOYOPS_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("src.lib.repo.clone_repo", _noop_clone_repo)
    monkeypatch.setattr("src.lib.aws.client.AWSClient.ecs", _ecs_with_noop_waiter)
    agent = DeployOpsAgent()
    monkeypatch.setattr(agent, "_write_artifacts", _noop_write_artifacts)
    # Never touched by this code path -- fail loudly if it ever is, instead
    # of silently re-provisioning infrastructure that's supposed to stay put.
    monkeypatch.setattr(
        "src.agents.deployops.agent.TerraformRunner.__init__",
        lambda self, *a, **kw: (_ for _ in ()).throw(AssertionError("Terraform must not run on the update path")),
    )
    return agent


@mock_aws
def test_update_ships_new_image_and_skips_terraform(deployops_agent, monkeypatch):
    ecs = boto3.client("ecs", region_name="us-east-1")
    _seed_ecs_service(ecs)

    async def fake_build_and_push(docker_image, aws_config, job_id, health_check_port=8080):
        assert docker_image.name == "app"
        return NEW_IMAGE

    monkeypatch.setattr(deployops_agent, "_build_and_push_image", fake_build_and_push)

    import asyncio
    result = asyncio.run(deployops_agent._deploy_existing_ecs_revision(_make_payload()))

    assert result["status"] == "success", result
    task = ecs.describe_task_definition(taskDefinition=result["task_definition"])["taskDefinition"]
    containers = task["containerDefinitions"]
    assert len(containers) == 1
    assert containers[0]["image"] == NEW_IMAGE

    service = ecs.describe_services(cluster=CLUSTER, services=[SERVICE])["services"][0]
    assert service["taskDefinition"] == result["task_definition"]


@mock_aws
def test_update_build_failure_leaves_live_service_untouched(deployops_agent, monkeypatch):
    ecs = boto3.client("ecs", region_name="us-east-1")
    _seed_ecs_service(ecs)
    original_task_def = ecs.describe_services(cluster=CLUSTER, services=[SERVICE])["services"][0]["taskDefinition"]

    async def failing_build_and_push(docker_image, aws_config, job_id, health_check_port=8080):
        return None

    monkeypatch.setattr(deployops_agent, "_build_and_push_image", failing_build_and_push)

    import asyncio
    result = asyncio.run(deployops_agent._deploy_existing_ecs_revision(_make_payload()))

    assert result["status"] == "failed"
    assert "app" in result["error"]

    # The live service must be exactly as it was -- a failed build/push must
    # never touch register_task_definition / update_service.
    service = ecs.describe_services(cluster=CLUSTER, services=[SERVICE])["services"][0]
    assert service["taskDefinition"] == original_task_def
    task = ecs.describe_task_definition(taskDefinition=service["taskDefinition"])["taskDefinition"]
    assert task["containerDefinitions"][0]["image"] == OLD_IMAGE
