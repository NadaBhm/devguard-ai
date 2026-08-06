"""
Tests for agent_adapters.py (T-2.17 / T-3.16)
Place dans: src/agents/orchestrator/tests/test_agent_adapters.py

Focus is the InfraCost -> DeployOps translation, because that is where the
two agents' contracts genuinely disagree and where a silent mistranslation
would only surface much later, inside AWS.

Lancer avec: pytest depuis la racine du repo.
"""

import pytest

from src.agents.orchestrator.agent_adapters import (
    run_sync,
    translate_deployops_result,
    translate_infracost_to_deploy_payload,
    use_real_codesec,
    use_real_deployops,
    use_real_infracost,
)


@pytest.fixture
def deploy_inputs():
    """The `_deploy_inputs` block as InfraCost's model_dump(by_alias=True) emits it."""
    return {
        "compute_type": "ecs",
        "artifacts": {
            "terraform": {
                "files": {
                    "main.tf": 'resource "aws_ecs_cluster" "app" {}',
                    "variables.tf": 'variable "region" {}',
                    "outputs.tf": 'output "alb_dns" {}',
                },
                "variables": {"region": "us-east-1"},
            },
            "dockerfile": "FROM python:3.12-slim",
            "docker_image": {"name": "devguard-app", "tag": "v1"},
            "source_code": "/tmp/repo_abc",
        },
        "aws_config": {
            "region": "eu-west-1",
            "ecs": {
                "cluster": "my-cluster",
                "service_name": "my-svc",
                "task_cpu": "256",
                "task_memory": "512",
            },
            "lambda": None,
            "ec2": None,
        },
        "deployment_config": {
            "ecs": {
                "strategy": "rolling",
                "health_check_path": "/healthz",
                "health_check_port": 8080,
                "timeout_minutes": 10,
                "min_healthy_percent": 50,
                "max_percent": 200,
            },
            "lambda": None,
            "ec2": None,
        },
    }


class TestFeatureFlags:
    """Mock mode must be the default: real agents aren't merged yet."""

    def test_all_agents_mocked_by_default(self, monkeypatch):
        for var in (
            "DEVGUARD_REAL_AGENTS",
            "DEVGUARD_REAL_CODESEC",
            "DEVGUARD_REAL_INFRACOST",
            "DEVGUARD_REAL_DEPLOYOPS",
        ):
            monkeypatch.delenv(var, raising=False)
        assert use_real_codesec() is False
        assert use_real_infracost() is False
        assert use_real_deployops() is False

    def test_global_switch_turns_on_all(self, monkeypatch):
        monkeypatch.setenv("DEVGUARD_REAL_AGENTS", "1")
        assert use_real_codesec() is True
        assert use_real_infracost() is True
        assert use_real_deployops() is True

    def test_per_agent_switch_is_independent(self, monkeypatch):
        monkeypatch.delenv("DEVGUARD_REAL_AGENTS", raising=False)
        monkeypatch.setenv("DEVGUARD_REAL_CODESEC", "1")
        monkeypatch.delenv("DEVGUARD_REAL_DEPLOYOPS", raising=False)
        assert use_real_codesec() is True
        assert use_real_deployops() is False


class TestInfraCostToDeployPayload:
    """The core contract mismatch: singular docker_image -> docker_images list."""

    def test_docker_image_becomes_a_list(self, deploy_inputs):
        payload = translate_infracost_to_deploy_payload(
            "job-abc", deploy_inputs, approved_by="hbib@test.com"
        )
        images = payload["artifacts"]["docker_images"]
        assert isinstance(images, list)
        # The whole point: DeployOps silently accepts an empty list and then
        # builds/pushes nothing. One image in, one image out.
        assert len(images) == 1
        assert images[0]["name"] == "devguard-app"
        assert images[0]["dockerfile"] == "FROM python:3.12-slim"
        assert images[0]["tag"] == "v1"

    def test_context_and_platform_are_defaulted(self, deploy_inputs):
        """Neither field exists upstream; both are required for a usable build."""
        payload = translate_infracost_to_deploy_payload(
            "job-abc", deploy_inputs, approved_by="hbib@test.com"
        )
        image = payload["artifacts"]["docker_images"][0]
        assert image["context"] == "/tmp/repo_abc"  # from artifacts.source_code
        assert image["platform"] == "linux/amd64"   # Fargate default

    def test_aws_config_is_flattened(self, deploy_inputs):
        """InfraCost nests under aws_config.ecs.cluster; DeployOps wants ecs_cluster."""
        payload = translate_infracost_to_deploy_payload(
            "job-abc", deploy_inputs, approved_by="hbib@test.com"
        )
        assert payload["aws_config"]["ecs_cluster"] == "my-cluster"
        assert payload["aws_config"]["service_name"] == "my-svc"
        assert payload["aws_config"]["region"] == "eu-west-1"

    def test_deployment_config_is_flattened(self, deploy_inputs):
        payload = translate_infracost_to_deploy_payload(
            "job-abc", deploy_inputs, approved_by="hbib@test.com"
        )
        cfg = payload["deployment_config"]
        assert cfg["health_check_path"] == "/healthz"
        assert cfg["health_check_port"] == 8080
        assert cfg["timeout_minutes"] == 10

    def test_blue_green_strategy_is_renamed(self, deploy_inputs):
        """InfraCost says "blue-green"; DeployOps's enum spells it "blue_green"."""
        deploy_inputs["deployment_config"]["ecs"]["strategy"] = "blue-green"
        payload = translate_infracost_to_deploy_payload(
            "job-abc", deploy_inputs, approved_by="hbib@test.com"
        )
        assert payload["deployment_config"]["strategy"] == "blue_green"

    def test_terraform_files_pass_through(self, deploy_inputs):
        payload = translate_infracost_to_deploy_payload(
            "job-abc", deploy_inputs, approved_by="hbib@test.com"
        )
        files = payload["artifacts"]["terraform"]["files"]
        assert set(files) == {"main.tf", "variables.tf", "outputs.tf"}

    def test_approval_is_recorded(self, deploy_inputs):
        payload = translate_infracost_to_deploy_payload(
            "job-abc", deploy_inputs, approved_by="nada@devguard.ai"
        )
        assert payload["approval"]["deploy_approved"] is True
        assert payload["approval"]["approved_by"] == "nada@devguard.ai"

    def test_job_id_is_propagated(self, deploy_inputs):
        payload = translate_infracost_to_deploy_payload(
            "job-abc", deploy_inputs, approved_by="x"
        )
        assert payload["job_id"] == "job-abc"


class TestTranslationFailsLoudly:
    """Every one of these would otherwise be a silent no-op deep inside AWS."""

    def test_lambda_is_rejected(self, deploy_inputs):
        deploy_inputs["compute_type"] = "lambda"
        deploy_inputs["aws_config"]["ecs"] = None
        with pytest.raises(ValueError, match="only supports ECS"):
            translate_infracost_to_deploy_payload("j", deploy_inputs, approved_by="x")

    def test_missing_dockerfile_is_rejected(self, deploy_inputs):
        deploy_inputs["artifacts"]["dockerfile"] = None
        with pytest.raises(ValueError, match="requires a Dockerfile"):
            translate_infracost_to_deploy_payload("j", deploy_inputs, approved_by="x")

    def test_missing_terraform_is_rejected(self, deploy_inputs):
        deploy_inputs["artifacts"]["terraform"]["files"] = {}
        with pytest.raises(ValueError, match="no Terraform files"):
            translate_infracost_to_deploy_payload("j", deploy_inputs, approved_by="x")

    def test_missing_cluster_is_rejected(self, deploy_inputs):
        """DeployOps.rollback() does payload["aws_config"]["ecs_cluster"] unguarded."""
        deploy_inputs["aws_config"]["ecs"]["cluster"] = None
        with pytest.raises(ValueError, match="missing cluster/service_name"):
            translate_infracost_to_deploy_payload("j", deploy_inputs, approved_by="x")


class TestDeployOpsResultTranslation:
    """DeployOps returns its own flat dict, not the orchestrator's shape."""

    def test_success_is_mapped(self):
        raw = {
            "status": "success",
            "job_id": "job-1",
            "deployed_url": "https://x.elb.amazonaws.com",
            "resources": {
                "ecs_cluster_name": {"value": "c"},
                "service_name": {"value": "s"},
                "alb_dns": {"value": "d"},
            },
        }
        result = translate_deployops_result(raw, "job-1")
        assert result["deployment_status"] == "success"
        assert result["health_check"]["passed"] is True
        assert result["terraform_outputs"]["ecs_cluster_name"] == "c"
        assert result["rollback_triggered"] is False

    def test_failure_is_mapped(self):
        raw = {"status": "failed", "error": "terraform apply failed"}
        result = translate_deployops_result(raw, "job-2")
        assert result["deployment_status"] == "failed"
        assert result["health_check"]["passed"] is False
        assert result["error"] == "terraform apply failed"
        assert result["job_id"] == "job-2"  # falls back to the orchestrator's id

    def test_health_check_failure_flags_rollback(self):
        raw = {"status": "failed", "error": "health check failed"}
        result = translate_deployops_result(raw, "job-3")
        assert result["rollback_triggered"] is True
        assert result["rollback_reason"] == "health check failed"


class TestRunSyncBridge:
    """The async->sync bridge that lets sync LangGraph nodes call async agents."""

    def test_runs_a_coroutine_from_sync_code(self):
        async def coro():
            return {"ok": True}

        assert run_sync(coro()) == {"ok": True}

    def test_propagates_exceptions(self):
        async def boom():
            raise ValueError("agent exploded")

        with pytest.raises(ValueError, match="agent exploded"):
            run_sync(boom())
