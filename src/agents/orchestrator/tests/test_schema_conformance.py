"""
Schema conformance tests
Place dans: src/agents/orchestrator/tests/test_schema_conformance.py

docs/api-contracts/orchestrator-input-schema.json is the contract three
people code against (backend persistence, dashboard rendering, this
orchestrator). Nothing enforced it, and it silently drifted: the schema
described an infracost_result shape that shares no field name with what the
real agent emits, and validation stayed green because those sub-properties
were declared without `required`. The visible symptom would have been a
final report telling stakeholders the deployment costs $0/month.

These tests run real orchestrator output against the schema so drift fails
in CI instead of in the demo.

Lancer avec: pytest depuis la racine du repo.
"""

import json
from pathlib import Path

import pytest

jsonschema = pytest.importorskip("jsonschema")

from src.agents.orchestrator.agent_adapters import normalize_infracost_result
from src.agents.orchestrator.graph import run_workflow, resume_workflow
from src.agents.orchestrator.nodes import (
    build_mock_codesec_result,
    build_mock_deployops_result,
    build_mock_infracost_result,
)
from src.agents.orchestrator.state import create_initial_state

SCHEMA_PATH = (
    Path(__file__).resolve().parents[4] / "docs" / "api-contracts" / "orchestrator-input-schema.json"
)

APPROVAL = {"approved": True, "comment": "ok", "approved_by": "hbib@devguard.ai"}


@pytest.fixture(scope="module")
def validator():
    if not SCHEMA_PATH.exists():
        pytest.skip(f"schema not found at {SCHEMA_PATH}")
    return jsonschema.Draft7Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def _errors(validator, payload):
    return [
        f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}"
        for e in validator.iter_errors(payload)
    ]


class TestStateConformance:
    def test_initial_state_conforms(self, validator):
        assert _errors(validator, create_initial_state("https://github.com/test/repo")) == []

    def test_completed_pipeline_conforms(self, validator):
        run_workflow("https://github.com/test/repo", thread_id="schema-1")
        resume_workflow("schema-1", APPROVAL)
        state = resume_workflow("schema-1", APPROVAL)
        assert state["status"] == "completed"
        assert _errors(validator, state) == []

    def test_paused_state_conforms(self, validator):
        """
        LangGraph injects __interrupt__ while a job waits at a human gate.
        With additionalProperties=false and no declaration, every paused job
        would fail validation - which is most of a job's lifetime.
        """
        state = run_workflow("https://github.com/test/repo", thread_id="schema-2")
        assert _errors(validator, state) == []

    def test_rejected_status_conforms(self, validator):
        """A human saying "no" is a legitimate terminal state, not a failure."""
        run_workflow("https://github.com/test/repo", thread_id="schema-3")
        state = resume_workflow(
            "schema-3", {"approved": False, "comment": "too expensive", "approved_by": "x@y.z"}
        )
        assert state["status"] == "rejected"
        assert _errors(validator, state) == []


class TestAgentResultsConformance:
    def test_mock_codesec_conforms(self, validator):
        state = create_initial_state("https://github.com/test/repo")
        state["codesec_result"] = build_mock_codesec_result(state["job_id"], state["repo_url"])
        assert _errors(validator, state) == []

    def test_mock_deployops_conforms(self, validator):
        state = create_initial_state("https://github.com/test/repo")
        state["deployops_result"] = build_mock_deployops_result(state["job_id"])
        assert _errors(validator, state) == []

    def test_mock_infracost_conforms(self, validator):
        state = create_initial_state("https://github.com/test/repo")
        state["infracost_result"] = build_mock_infracost_result()
        assert _errors(validator, state) == []


class TestRealInfraCostShape:
    """
    What call_infracost() returns in real mode: to_orchestrator_result()'s
    output plus a "_deploy_inputs" block, matching the master snapshot merged
    2026-08-08 (core.orchestrator_adapter + run_pipeline_with_context).
    """

    @pytest.fixture
    def infracost_result(self):
        return {
            "architecture_recommendation": "ecs_fargate",
            "justification": "ECS Fargate fits this workload.",
            "generated_terraform": {
                "files": {"main.tf": "M", "variables.tf": "V", "outputs.tf": "O"},
                "variables": {"region": "us-east-1"},
            },
            "cost_estimate": {
                "amount": 145.32, "currency": "USD",   # raw Money shape, as to_orchestrator_result() emits
                "range_min": 120.0, "range_max": 170.0,
            },
            "load_scenarios": [{"users": 1000, "estimated_monthly_cost": {"amount": 145.32}}],
            "optimizations": [{"name": "graviton", "reason": "cheaper", "projected_monthly_savings": 25.4, "selected": True}],
            "region_comparison": [{"region": "us-east-1", "estimated_monthly_cost": {"amount": 145.32}}],
            "_deploy_inputs": {
                "compute_type": "ecs",
                "artifacts": {
                    "terraform": {"files": {"main.tf": "M"}, "variables": {}},
                    "dockerfile": "FROM python:3.12-slim\n",
                    "docker_image": {"name": "devguard-app", "tag": "sha-abc"},
                    "source_code": "/tmp/repo_job-abc",
                },
                "aws_config": {"region": "us-east-1", "ecs": {"cluster": "c", "service_name": "s"}},
                "deployment_config": {"ecs": {"strategy": "rolling"}},
            },
        }

    def test_normalized_shape_conforms_to_schema(self, validator, infracost_result):
        """
        _deploy_inputs is an orchestrator-internal field (Dockerfile content,
        raw terraform/aws blocks DeployOps needs), not part of the public
        orchestrator-input-schema.json contract - validate the public view.
        Must go through normalize_infracost_result() first: the raw
        to_orchestrator_result() output does NOT conform on its own
        (cost_estimate.amount, not .monthly_cost_usd) - this is the drift
        test_schema_conformance.py exists to catch.
        """
        normalized = normalize_infracost_result(infracost_result)
        state = create_initial_state("https://github.com/test/repo")
        state["infracost_result"] = {k: v for k, v in normalized.items() if k != "_deploy_inputs"}
        assert _errors(validator, state) == []

    def test_raw_shape_is_rejected_without_normalization(self, validator, infracost_result):
        """Guards against silently reverting to the un-normalized field name."""
        state = create_initial_state("https://github.com/test/repo")
        state["infracost_result"] = {k: v for k, v in infracost_result.items() if k != "_deploy_inputs"}
        assert _errors(validator, state) != []

    def test_mock_passes_through_unchanged(self):
        mock = build_mock_infracost_result()
        assert normalize_infracost_result(mock) == mock

    def test_nested_money_shapes_are_normalized_for_report(self, infracost_result):
        """The report template reads estimated_monthly_cost_usd /
        monthly_cost_usd / projected_savings_usd; the real agent's Money
        dumps must be flattened so cost tables don't render blank."""
        normalized = normalize_infracost_result(infracost_result)

        assert normalized["load_scenarios"][0]["estimated_monthly_cost_usd"] == 145.32
        assert normalized["region_comparison"][0]["monthly_cost_usd"] == 145.32
        assert normalized["optimizations"][0]["projected_savings_usd"] == 25.4
        assert normalized["optimizations"][0]["strategy"] == "graviton"
        assert normalized["optimizations"][0]["description"] == "cheaper"

    def test_nested_normalization_is_idempotent(self, infracost_result):
        once = normalize_infracost_result(infracost_result)
        twice = normalize_infracost_result(once)
        assert once == twice
