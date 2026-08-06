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
    The exact payload Karim's to_orchestrator_result() produces - his Pydantic
    models dumped as-is. Every one of these five keys carries different field
    names from the schema.
    """

    @pytest.fixture
    def raw_infracost(self):
        money = {"amount": 145.32, "currency": "USD", "range_min": 120.0, "range_max": 170.0}
        return {
            "architecture_recommendation": "ecs_fargate",
            "justification": "FastAPI with moderate traffic",
            "generated_terraform": {
                "files": {"main.tf": "M", "variables.tf": "V", "outputs.tf": "O"},
                "variables": {"region": "us-east-1"},
            },
            "cost_estimate": money,
            "load_scenarios": [
                {"users": 1000, "sizing": {"cpu": 256, "memory": 512},
                 "estimated_monthly_cost": money}
            ],
            "optimizations": [
                {"name": "Graviton processors", "reason": "20% cheaper",
                 "projected_monthly_savings": 25.4, "selected": True}
            ],
            "region_comparison": [{"region": "us-east-1", "estimated_monthly_cost": money}],
        }

    def test_raw_shape_is_rejected(self, validator, raw_infracost):
        """
        The schema must now CATCH the mismatch. Before `required` was added it
        validated cleanly, which is how the drift went unnoticed.
        """
        state = create_initial_state("https://github.com/test/repo")
        state["infracost_result"] = raw_infracost
        assert _errors(validator, state) != []

    def test_normalized_shape_conforms(self, validator, raw_infracost):
        state = create_initial_state("https://github.com/test/repo")
        state["infracost_result"] = normalize_infracost_result(raw_infracost)
        assert _errors(validator, state) == []

    def test_cost_survives_normalization(self, raw_infracost):
        """The bug this all guards against: a $0 figure in the final report."""
        result = normalize_infracost_result(raw_infracost)
        assert result["cost_estimate"]["monthly_cost_usd"] == 145.32

    def test_uncertainty_range_is_preserved(self, raw_infracost):
        """Real information the old schema had no field for; don't drop it."""
        result = normalize_infracost_result(raw_infracost)
        assert result["cost_estimate"]["range_min"] == 120.0
        assert result["cost_estimate"]["range_max"] == 170.0

    def test_terraform_files_are_flattened(self, raw_infracost):
        result = normalize_infracost_result(raw_infracost)
        assert result["generated_terraform"]["main_tf"] == "M"
        assert result["generated_terraform"]["outputs_tf"] == "O"

    def test_llm_strategy_names_are_mapped(self, raw_infracost):
        result = normalize_infracost_result(raw_infracost)
        assert result["optimizations"][0]["strategy"] == "graviton"
        assert result["optimizations"][0]["projected_savings_usd"] == 25.4

    def test_unknown_strategy_is_kept_not_dropped(self):
        """Losing a recommendation is worse than an off-vocabulary label."""
        result = normalize_infracost_result(
            {"optimizations": [{"name": "S3 lifecycle tiering", "reason": "r",
                                "projected_monthly_savings": 5}]}
        )
        assert result["optimizations"][0]["strategy"] == "S3 lifecycle tiering"

    def test_sizing_dict_becomes_prose(self, raw_infracost):
        result = normalize_infracost_result(raw_infracost)
        assumptions = result["load_scenarios"][0]["scaling_assumptions"]
        assert "cpu: 256" in assumptions

    def test_normalization_is_idempotent(self, raw_infracost):
        """The mock is already canonical; normalizing twice must be harmless."""
        once = normalize_infracost_result(raw_infracost)
        twice = normalize_infracost_result(once)
        assert once == twice

    def test_mock_passes_through_unchanged(self):
        mock = build_mock_infracost_result()
        assert normalize_infracost_result(mock)["cost_estimate"]["monthly_cost_usd"] == 145.32
