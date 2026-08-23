"""
Tests for agent_adapters.py (T-2.17 / T-3.16).

These fixtures use the real InfraCost shape from master:
run_pipeline() -> InfraCostOutput (models/output_models.py) with
compute_type + aws_config.ecs.* + deployment_config.ecs.* +
artifacts.docker_image/dockerfile/source_code.
"""

import pytest

from src.agents.orchestrator.agent_adapters import (
    call_infracost,
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
    return infracost_result["_deploy_inputs"]


class TestFeatureFlags:
    pass

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
    pass

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

    def test_multi_container_payload_keeps_all_images(self, deploy_inputs):
        deploy_inputs["artifacts"]["docker_images"] = [
            {
                "name": "devguard-app",
                "dockerfile": "FROM python:3.12-slim\nEXPOSE 8000\n",
                "context": "backend",
                "tag": "sha-abc123",
                "platform": "linux/amd64",
            },
            {
                "name": "devguard-app-frontend",
                "dockerfile": "FROM nginx:1.27\nEXPOSE 80\n",
                "context": "frontend",
                "tag": "sha-abc123",
                "platform": "linux/amd64",
            },
        ]
        payload = translate_infracost_to_deploy_payload(
            "job-abc", deploy_inputs, approved_by="hbib@test.com"
        )
        images = payload["artifacts"]["docker_images"]
        assert len(images) == 2
        assert images[0]["name"] == "devguard-app"
        assert images[0]["context"] == "backend"
        assert images[1]["name"] == "devguard-app-frontend"
        assert images[1]["context"] == "frontend"
        assert images[1]["dockerfile"] == "FROM nginx:1.27\nEXPOSE 80\n"

    def test_multi_container_entry_missing_dockerfile_is_rejected(self, deploy_inputs):
        deploy_inputs["artifacts"]["docker_images"] = [
            {
                "name": "devguard-app",
                "dockerfile": "FROM python:3.12-slim\nEXPOSE 8000\n",
                "context": "backend",
                "tag": "sha-abc123",
            },
            {
                "name": "devguard-app-frontend",
                "context": "frontend",
                "tag": "sha-abc123",
            },
        ]
        with pytest.raises(ValueError, match="has no dockerfile content"):
            translate_infracost_to_deploy_payload(
                "job-abc", deploy_inputs, approved_by="hbib@test.com"
            )

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

    def test_repo_url_is_forwarded_when_given(self, deploy_inputs):
        payload = translate_infracost_to_deploy_payload(
            "job-abc", deploy_inputs, approved_by="x",
            repo_url="https://github.com/owner/repo",
        )
        assert payload["metadata"]["repo_url"] == "https://github.com/owner/repo"

    def test_repo_url_omitted_when_not_given(self, deploy_inputs):
        payload = translate_infracost_to_deploy_payload(
            "job-abc", deploy_inputs, approved_by="x"
        )
        assert payload["metadata"] == {}

    def test_ec2_compute_type_uses_docker_but_no_cluster(self, deploy_inputs):
        deploy_inputs["compute_type"] = "ec2"
        deploy_inputs["aws_config"] = {
            "region": "eu-west-1",
            "ecs": None,
            "ec2": {"instance_type": "t3.small"},
            "s3": None,
        }
        deploy_inputs["deployment_config"] = {
            "ec2": {"strategy": "rolling", "health_check_path": "/health", "health_check_port": 8080},
            "ecs": None,
            "s3": None,
        }
        payload = translate_infracost_to_deploy_payload(
            "job-abc", deploy_inputs, approved_by="x"
        )
        assert payload["compute_type"] == "ec2"
        assert payload["aws_config"]["ecs_cluster"] is None
        assert payload["aws_config"]["service_name"] is None
        assert len(payload["artifacts"]["docker_images"]) == 1
        assert payload["deployment_config"]["health_check_port"] == 8080

    def test_s3_compute_type_skips_docker_and_sets_bucket(self, deploy_inputs):
        deploy_inputs["compute_type"] = "s3"
        deploy_inputs["aws_config"] = {
            "region": "eu-west-1",
            "ecs": None,
            "ec2": None,
            "s3": {"bucket_name": "devguard-static-abc"},
        }
        deploy_inputs["deployment_config"] = {
            "s3": {"strategy": "static", "health_check_path": "/", "timeout_minutes": 5},
            "ecs": None,
            "ec2": None,
        }
        payload = translate_infracost_to_deploy_payload(
            "job-abc", deploy_inputs, approved_by="x"
        )
        assert payload["compute_type"] == "s3"
        assert payload["aws_config"]["bucket_name"] == "devguard-static-abc"
        assert payload["artifacts"]["docker_images"] == []
        assert payload["deployment_config"]["health_check_path"] == "/"
        assert payload["deployment_config"]["timeout_minutes"] == 5


class TestTranslationFailsLoudly:
    pass

    def test_non_ecs_compute_type_is_rejected(self, deploy_inputs, monkeypatch):
        deploy_inputs["compute_type"] = "lambda"
        deploy_inputs["aws_config"]["ecs"] = None
        monkeypatch.setenv("DEVGUARD_FORCE_COMPUTE_ECS", "0")
        with pytest.raises(ValueError, match="only supports ecs, ec2, and s3"):
            translate_infracost_to_deploy_payload("j", deploy_inputs, approved_by="x")

    def test_lambda_falls_back_to_ec2_when_guard_enabled(self, deploy_inputs):
        """When the guard is on, a lambda recommendation is remapped to ec2
        so the real pipeline does not deterministically land on an undeployable
        type (the deterministic scorer already excludes lambda under the same
        guard)."""
        deploy_inputs["compute_type"] = "lambda"
        deploy_inputs["artifacts"]["docker_images"] = []
        deploy_inputs["artifacts"]["dockerfile"] = None
        result = translate_infracost_to_deploy_payload("j", deploy_inputs, approved_by="x")
        assert result["compute_type"] == "ec2"
        assert len(result["artifacts"]["docker_images"]) == 1

    def test_missing_dockerfile_content_is_rejected(self, deploy_inputs):
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
    pass

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


class TestCallInfraCostRepoClone:
    """Real InfraCost path re-clones the repo so the pipeline can digest
    the whole codebase (CodeSec deletes its clone when analysis finishes)."""

    @pytest.fixture(autouse=True)
    def _real_infracost(self, monkeypatch):
        monkeypatch.setenv("DEVGUARD_REAL_INFRACOST", "1")
        monkeypatch.delenv("DEVGUARD_REAL_AGENTS", raising=False)

    def _minimal_codesec_result(self):
        return {
            "job_id": "job-1",
            "status": "completed",
            "repo_url": "https://github.com/owner/repo",
            "repo_metadata": {"commit_sha": "abc123"},
            "stack_detection": {
                "primary_language": "python",
                "confidence": 0.9,
                "container": {"detected": True},
            },
        }

    async def test_clones_repo_and_passes_repo_path_on_feedback(self, monkeypatch):
        captured = {}

        def _fake_clone(repo_url, target_dir, **_kwargs):
            captured["url"] = repo_url
            captured["target"] = target_dir
            from pathlib import Path
            Path(target_dir).mkdir(parents=True, exist_ok=True)

        def _fake_pipeline(raw_input):
            captured["repo_path"] = raw_input.get("repo_path")
            captured["feedback"] = raw_input.get("user_feedback")
            return {"cost_estimate": {"monthly_cost_usd": 10.0, "currency": "USD"}}

        monkeypatch.setattr("src.lib.repo.clone_repo", _fake_clone)
        monkeypatch.setattr(
            "src.agents.orchestrator.agent_adapters._run_infracost_pipeline",
            _fake_pipeline,
        )

        result = await call_infracost(
            self._minimal_codesec_result(), "job-1", feedback="make it cheaper"
        )

        assert captured["url"] == "https://github.com/owner/repo"
        assert captured["feedback"] == "make it cheaper"
        assert captured["repo_path"] is not None
        assert result["cost_estimate"]["monthly_cost_usd"] == 10.0

    async def test_does_not_clone_without_feedback(self, monkeypatch):
        def _boom(*args, **_kwargs):
            raise AssertionError("clone_repo must not be called without feedback")

        captured = {}

        def _fake_pipeline(raw_input):
            captured["repo_path"] = raw_input.get("repo_path")
            return {"cost_estimate": {"monthly_cost_usd": 10.0, "currency": "USD"}}

        monkeypatch.setattr("src.lib.repo.clone_repo", _boom)
        monkeypatch.setattr(
            "src.agents.orchestrator.agent_adapters._run_infracost_pipeline",
            _fake_pipeline,
        )

        await call_infracost(self._minimal_codesec_result(), "job-1")

        assert captured["repo_path"] is None

    async def test_clone_failure_is_fail_soft(self, monkeypatch):
        """Failed re-clone must not break regeneration; pipeline runs without repo context."""
        captured = {}

        def _fake_clone(*_args, **_kwargs):
            raise RuntimeError("git exploded")

        def _fake_pipeline(raw_input):
            captured["repo_path"] = raw_input.get("repo_path")
            return {"cost_estimate": {"monthly_cost_usd": 10.0, "currency": "USD"}}

        monkeypatch.setattr("src.lib.repo.clone_repo", _fake_clone)
        monkeypatch.setattr(
            "src.agents.orchestrator.agent_adapters._run_infracost_pipeline",
            _fake_pipeline,
        )

        result = await call_infracost(
            self._minimal_codesec_result(), "job-1", feedback="cheaper"
        )

        assert captured["repo_path"] is None
        assert result["cost_estimate"]["monthly_cost_usd"] == 10.0

    async def test_iteration_number_is_threaded_into_raw_input(self, monkeypatch):
        """Adapter must forward 1-based regen round so pipeline can gate first-regen fix."""
        captured = {}

        def _fake_pipeline(raw_input):
            captured["regen_iteration"] = raw_input.get("regen_iteration")
            return {"cost_estimate": {"monthly_cost_usd": 10.0, "currency": "USD"}}

        monkeypatch.setattr(
            "src.agents.orchestrator.agent_adapters._run_infracost_pipeline",
            _fake_pipeline,
        )

        await call_infracost(
            self._minimal_codesec_result(),
            "job-1",
            feedback="bigger",
            iteration_number=1,
        )

        assert captured["regen_iteration"] == 1

    async def test_iteration_number_not_set_without_value(self, monkeypatch):
        captured = {}

        def _fake_pipeline(raw_input):
            captured["regen_iteration"] = raw_input.get("regen_iteration")
            return {"cost_estimate": {"monthly_cost_usd": 10.0, "currency": "USD"}}

        monkeypatch.setattr(
            "src.agents.orchestrator.agent_adapters._run_infracost_pipeline",
            _fake_pipeline,
        )

        await call_infracost(self._minimal_codesec_result(), "job-1", feedback="bigger")

        assert captured["regen_iteration"] is None
