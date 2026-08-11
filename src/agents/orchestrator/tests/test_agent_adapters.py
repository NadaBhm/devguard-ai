"""
Tests for agent_adapters.py (T-2.17 / T-3.16)
Place dans: src/agents/orchestrator/tests/test_agent_adapters.py

Rewritten after discovering Karim's InfraCost API on master is not what an
earlier version of this adapter (and these tests) targeted:
run_pipeline_with_context() and core.orchestrator_adapter don't exist on
master - only run_pipeline() -> InfraCostOutput (see models/output_models.py).
These fixtures use that real shape (compute_type + aws_config.ecs.* +
deployment_config.ecs.* + artifacts.docker_image/dockerfile/source_code).

Lancer avec: pytest depuis la racine du repo.
"""

import pytest

from src.agents.orchestrator.agent_adapters import (
    normalize_infracost_result,
    run_sync,
    translate_deployops_result,
    translate_infracost_to_deploy_payload,
    use_real_codesec,
    use_real_deployops,
    use_real_infracost,
)


@pytest.fixture
def infracost_result():
    """
    What call_infracost() actually returns in real mode: Karim's
    to_orchestrator_result() output (already orchestrator-shaped), plus the
    "_deploy_inputs" block _run_infracost_pipeline() stashes alongside it.
    """
    return {
        "architecture_recommendation": "ecs_fargate",
        "justification": "FastAPI with moderate traffic suits ECS Fargate.",
        "generated_terraform": {
            "files": {
                "main.tf": 'resource "aws_ecs_cluster" "app" {}',
                "variables.tf": 'variable "region" {}',
                "outputs.tf": 'output "alb_dns" {}',
            },
            "variables": {"region": "us-east-1"},
        },
        "cost_estimate": {
            "amount": 145.32, "currency": "USD",
            "range_min": 120.0, "range_max": 170.0,
        },
        "load_scenarios": [
            {"users": 1000, "estimated_monthly_cost": {"amount": 145.32}},
        ],
        "optimizations": [
            {"name": "graviton", "reason": "20% cheaper", "selected": True},
        ],
        "region_comparison": [
            {"region": "us-east-1", "estimated_monthly_cost": {"amount": 145.32}},
        ],
        "_deploy_inputs": {
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
                "dockerfile": "FROM python:3.12-slim\nCOPY . /app\n",
                "docker_image": {"name": "devguard-app", "tag": "sha-abc123"},
                "source_code": "/tmp/repo_job-abc",
            },
            "aws_config": {
                "region": "eu-west-1",
                "ecs": {
                    "cluster": "my-cluster", "service_name": "my-svc",
                    "task_cpu": "256", "task_memory": "512",
                },
                "lambda": None,
                "ec2": None,
            },
            "deployment_config": {
                "ecs": {
                    "strategy": "rolling", "health_check_path": "/healthz",
                    "health_check_port": 8080, "timeout_minutes": 10,
                    "min_healthy_percent": 50, "max_percent": 200,
                },
                "lambda": None,
                "ec2": None,
            },
        },
    }


@pytest.fixture
def deploy_inputs(infracost_result):
    """Just the _deploy_inputs block, as translate_infracost_to_deploy_payload receives it."""
    return infracost_result["_deploy_inputs"]


class TestFeatureFlags:
    """Mock mode must be the default: real agents aren't merged yet."""

    def test_all_agents_mocked_by_default(self, monkeypatch):
        for var in (
            "DEVGUARD_REAL_AGENTS", "DEVGUARD_REAL_CODESEC",
            "DEVGUARD_REAL_INFRACOST", "DEVGUARD_REAL_DEPLOYOPS",
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


class TestNormalizeInfraCostResult:
    """
    Defensive layer on top of Karim's to_orchestrator_result(), which
    correctly renames 6 of 7 fields but leaves cost_estimate as a raw Money
    dump ({amount,...}) instead of the schema's {monthly_cost_usd,...}.
    """

    def test_fixes_cost_estimate_field_name(self, infracost_result):
        infracost_result["cost_estimate"] = {
            "amount": 99.5, "currency": "USD", "range_min": 80.0, "range_max": 120.0,
        }
        result = normalize_infracost_result(infracost_result)
        assert result["cost_estimate"]["monthly_cost_usd"] == 99.5
        assert result["cost_estimate"]["range_min"] == 80.0

    def test_already_normalized_cost_is_untouched(self, infracost_result):
        infracost_result["cost_estimate"] = {"monthly_cost_usd": 50.0, "currency": "USD"}
        result = normalize_infracost_result(infracost_result)
        assert result["cost_estimate"]["monthly_cost_usd"] == 50.0

    def test_other_fields_pass_through_unchanged(self, infracost_result):
        result = normalize_infracost_result(infracost_result)
        assert result["architecture_recommendation"] == "ecs_fargate"
        assert result["_deploy_inputs"] == infracost_result["_deploy_inputs"]

    def test_mock_passes_through_unchanged(self):
        from src.agents.orchestrator.nodes import build_mock_infracost_result
        mock = build_mock_infracost_result()
        assert normalize_infracost_result(mock) == mock


class TestInfraCostToDeployPayload:
    def test_docker_image_becomes_a_list(self, deploy_inputs):
        payload = translate_infracost_to_deploy_payload(
            "job-abc", deploy_inputs, approved_by="hbib@test.com"
        )
        images = payload["artifacts"]["docker_images"]
        assert len(images) == 1
        assert images[0]["name"] == "devguard-app"
        assert images[0]["dockerfile"] == "FROM python:3.12-slim\nCOPY . /app\n"
        assert images[0]["tag"] == "sha-abc123"

    def test_platform_is_defaulted(self, deploy_inputs):
        payload = translate_infracost_to_deploy_payload(
            "job-abc", deploy_inputs, approved_by="hbib@test.com"
        )
        assert payload["artifacts"]["docker_images"][0]["platform"] == "linux/amd64"

    def test_aws_config_is_flattened(self, deploy_inputs):
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
        deploy_inputs["deployment_config"]["ecs"]["strategy"] = "blue-green"
        payload = translate_infracost_to_deploy_payload(
            "job-abc", deploy_inputs, approved_by="hbib@test.com"
        )
        assert payload["deployment_config"]["strategy"] == "blue_green"

    def test_terraform_files_pass_through(self, deploy_inputs):
        payload = translate_infracost_to_deploy_payload(
            "job-abc", deploy_inputs, approved_by="hbib@test.com"
        )
        assert set(payload["artifacts"]["terraform"]["files"]) == {
            "main.tf", "variables.tf", "outputs.tf"
        }

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

    def test_non_ecs_compute_type_is_rejected(self, deploy_inputs):
        deploy_inputs["compute_type"] = "lambda"
        deploy_inputs["aws_config"]["ecs"] = None
        with pytest.raises(ValueError, match="only supports ECS"):
            translate_infracost_to_deploy_payload("j", deploy_inputs, approved_by="x")

    def test_missing_dockerfile_content_is_rejected(self, deploy_inputs):
        """
        This InfraCost build generates Dockerfile content itself, but must
        still fail loudly (not silently build an empty image) if that ever
        regresses to empty/None - e.g. for a Lambda-only deployment.
        """
        deploy_inputs["artifacts"]["dockerfile"] = None
        with pytest.raises(ValueError, match="no Dockerfile content"):
            translate_infracost_to_deploy_payload("j", deploy_inputs, approved_by="x")

    def test_missing_terraform_is_rejected(self, deploy_inputs):
        deploy_inputs["artifacts"]["terraform"]["files"] = {}
        with pytest.raises(ValueError, match="no Terraform files"):
            translate_infracost_to_deploy_payload("j", deploy_inputs, approved_by="x")

    def test_missing_cluster_is_rejected(self, deploy_inputs):
        deploy_inputs["aws_config"]["ecs"]["cluster"] = None
        with pytest.raises(ValueError, match="missing cluster/service_name"):
            translate_infracost_to_deploy_payload("j", deploy_inputs, approved_by="x")


class TestDeployOpsResultTranslation:
    """DeployOps returns its own flat dict, not the orchestrator's shape."""

    def test_success_is_mapped(self):
        raw = {
            "status": "success", "job_id": "job-1",
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
        assert result["job_id"] == "job-2"

    def test_health_check_failure_flags_rollback(self):
        raw = {"status": "failed", "error": "health check failed"}
        result = translate_deployops_result(raw, "job-3")
        assert result["rollback_triggered"] is True
        assert result["rollback_reason"] == "health check failed"

    def test_preserves_actual_health_check_metrics(self):
        raw = {
            "status": "success",
            "health_check": {
                "passed": True,
                "response_time_ms": 123,
                "status_code": 200,
                "checked_at": "2026-08-11T12:00:00Z",
            },
        }
        result = translate_deployops_result(raw, "job-4")
        assert result["health_check"]["passed"] is True
        assert result["health_check"]["response_time_ms"] == 123
        assert result["health_check"]["status_code"] == 200
        assert result["health_check"]["checked_at"] == "2026-08-11T12:00:00Z"


class TestRunSyncBridge:
    def test_runs_a_coroutine_from_sync_code(self):
        async def coro():
            return {"ok": True}
        assert run_sync(coro()) == {"ok": True}

    def test_propagates_exceptions(self):
        async def boom():
            raise ValueError("agent exploded")
        with pytest.raises(ValueError, match="agent exploded"):
            run_sync(boom())
