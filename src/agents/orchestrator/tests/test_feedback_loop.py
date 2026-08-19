"""
Tests for the Gate-2 feedback / regeneration loop (Phase 6).
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt

from src.agents.orchestrator.agent_adapters import _mock_infracost_with_feedback
from src.agents.orchestrator.human_gates import human_gate_2_impl
from src.agents.orchestrator.nodes import (
    MAX_INFRACOST_ITERATIONS,
    infracost_agent_impl,
    route_after_gate_2,
)
from src.agents.orchestrator.state import OrchestratorState, create_initial_state


@pytest.fixture
def state():
    from src.agents.orchestrator.nodes import (
        mock_codesec_agent_impl,
        mock_infracost_agent_impl,
    )

    s = create_initial_state("https://github.com/test/repo")
    s = mock_codesec_agent_impl(s)
    s = mock_infracost_agent_impl(s)
    return s


def _run_gate2_with(approval: dict, state: OrchestratorState) -> OrchestratorState:
    with patch(
        "src.agents.orchestrator.human_gates.interrupt", return_value=approval
    ):
        return human_gate_2_impl(state)


class TestRouteAfterGate2:
    def test_approved_routes_to_deployops(self, state):
        state["human_gates"]["gate_2_pre_deployops"]["approved"] = True
        assert route_after_gate_2(state) == "deployops_agent"

    def test_approved_with_requested_changes_loops_back_to_infracost(self, state):
        """Regeneration wins over approval: the frontend sends approved=True
        alongside request_regeneration, and the stale artifacts must never be
        deployed. Regression for a live E2E failure where approve+regen went
        straight to DeployOps with the pre-regen result."""
        gate = state["human_gates"]["gate_2_pre_deployops"]
        gate["approved"] = True
        gate["requested_changes"] = "wire PORT to the container port"
        assert route_after_gate_2(state) == "infracost_agent"

    def test_plain_reject_routes_to_end(self, state):
        state["human_gates"]["gate_2_pre_deployops"]["approved"] = False
        assert route_after_gate_2(state) == "end"

    def test_requested_changes_loops_back_to_infracost(self, state):
        gate = state["human_gates"]["gate_2_pre_deployops"]
        gate["approved"] = False
        gate["requested_changes"] = "make it cheaper"
        assert route_after_gate_2(state) == "infracost_agent"

    def test_infracost_feedback_alone_loops_back(self, state):
        state["human_gates"]["gate_2_pre_deployops"]["approved"] = False
        state["infracost_feedback"] = "make it cheaper"
        assert route_after_gate_2(state) == "infracost_agent"

    def test_rejected_status_short_circuits_to_end(self, state):
        state["status"] = "rejected"
        state["human_gates"]["gate_2_pre_deployops"]["approved"] = False
        state["human_gates"]["gate_2_pre_deployops"]["requested_changes"] = "nope"
        assert route_after_gate_2(state) == "end"

    def test_regeneration_cap_stops_the_loop(self, state):
        gate = state["human_gates"]["gate_2_pre_deployops"]
        gate["approved"] = False
        gate["requested_changes"] = "one more try"
        state["infracost_iterations"] = [
            {
                "iteration": i,
                "prompt": "round",
                "result": state["infracost_result"],
                "requested_at": datetime.now(timezone.utc).isoformat(),
            }
            for i in range(1, MAX_INFRACOST_ITERATIONS + 1)
        ]
        assert route_after_gate_2(state) == "end"


class TestHumanGate2Regeneration:
    def test_regenerate_sets_feedback_and_requested_changes(self, state):
        result = _run_gate2_with(
            {
                "approved": False,
                "comment": "switch to lambda",
                "approved_by": "user@example.com",
                "request_regeneration": True,
            },
            state,
        )

        gate = result["human_gates"]["gate_2_pre_deployops"]
        assert gate["approved"] is False
        assert gate["requested_changes"] == "switch to lambda"
        assert result["infracost_feedback"] == "switch to lambda"
        assert result["status"] == "awaiting_approval_gate_2"

    def test_regenerate_with_blank_comment_is_recorded_but_empty(self, state):
        result = _run_gate2_with(
            {"approved": False, "comment": "   ", "request_regeneration": True},
            state,
        )
        assert result["infracost_feedback"] == ""
        assert result["human_gates"]["gate_2_pre_deployops"]["requested_changes"] == ""

    def test_actions_include_regenerate_below_cap(self, state, monkeypatch):
        captured = {}

        def fake_interrupt(payload):
            captured["actions"] = payload.get("actions")
            captured["context"] = payload.get("context")
            return {"approved": True}

        with patch("src.agents.orchestrator.human_gates.interrupt", fake_interrupt):
            human_gate_2_impl(state)

        assert captured["actions"] == ["approve", "reject", "regenerate"]
        assert captured["context"]["max_iterations"] == MAX_INFRACOST_ITERATIONS
        assert captured["context"]["iteration"] == 0

    def test_actions_hide_regenerate_at_cap(self, state, monkeypatch):
        state["infracost_iterations"] = [
            {
                "iteration": i,
                "prompt": "round",
                "result": state["infracost_result"],
                "requested_at": datetime.now(timezone.utc).isoformat(),
            }
            for i in range(1, MAX_INFRACOST_ITERATIONS + 1)
        ]
        captured = {}

        def fake_interrupt(payload):
            captured["actions"] = payload.get("actions")
            return {"approved": True}

        with patch("src.agents.orchestrator.human_gates.interrupt", fake_interrupt):
            human_gate_2_impl(state)

        assert captured["actions"] == ["approve", "reject"]

    def test_plain_reject_halts_workflow(self, state):
        result = _run_gate2_with({"approved": False, "comment": "too expensive"}, state)
        assert result["status"] == "rejected"
        assert result["infracost_feedback"] is None
        assert len(result["error_log"]) == 1
        assert result["error_log"][0]["node"] == "human_gate_2"

    def test_pause_resets_previous_round_decision(self, state):
        """Stale round-0 decision must be cleared BEFORE interrupt() so frontend
        pendingGate (approved === null) finds the gate again."""
        gate = state["human_gates"]["gate_2_pre_deployops"]
        gate["approved"] = False
        gate["comment"] = "make it cheaper"
        gate["requested_changes"] = "make it cheaper"

        with patch(
            "src.agents.orchestrator.human_gates.interrupt",
            side_effect=GraphInterrupt(interrupts=[Interrupt(value={"gate": "gate_2_pre_deployops"})]),
        ):
            with pytest.raises(GraphInterrupt):
                human_gate_2_impl(state)

        gate = state["human_gates"]["gate_2_pre_deployops"]
        assert gate["approved"] is None
        assert gate["comment"] is None
        assert gate["approved_at"] is None
        assert gate["approved_by"] is None
        assert gate["requested_changes"] is None


class TestInfracostNodeIterations:
    def test_no_feedback_does_not_append_iterations(self, state):
        result = infracost_agent_impl(state)
        assert result["infracost_iterations"] == []

    def test_feedback_appends_record_and_is_consumed(self, state):
        state["infracost_feedback"] = "make it cheaper"
        result = infracost_agent_impl(state)

        assert len(result["infracost_iterations"]) == 1
        record = result["infracost_iterations"][0]
        assert record["iteration"] == 1
        assert record["prompt"] == "make it cheaper"
        assert record["result"] == result["infracost_result"]
        assert result["infracost_feedback"] is None

    def test_second_feedback_appends_round_two(self, state):
        state["infracost_feedback"] = "round one"
        result = infracost_agent_impl(state)
        state["infracost_result"] = result["infracost_result"]
        state["infracost_iterations"] = result["infracost_iterations"]
        state["infracost_feedback"] = "round two"
        result = infracost_agent_impl(state)

        assert [r["iteration"] for r in result["infracost_iterations"]] == [1, 2]
        assert result["infracost_iterations"][1]["prompt"] == "round two"


class TestMockInfracostWithFeedback:
    def test_no_feedback_returns_baseline_unchanged(self):
        base = _mock_infracost_with_feedback("job-1", None, None)
        again = _mock_infracost_with_feedback("job-1", None, None)
        assert base == again
        assert base["cost_estimate"]["monthly_cost_usd"] == 145.32

    def test_cheaper_prompt_lowers_cost(self):
        result = _mock_infracost_with_feedback(
            "job-1", "make it cheaper, please", None
        )
        assert result["cost_estimate"]["monthly_cost_usd"] == pytest.approx(145.32 * 0.85, abs=0.01)

    def test_scale_up_prompt_raises_cost(self):
        result = _mock_infracost_with_feedback(
            "job-1", "we need to scale more", None
        )
        assert result["cost_estimate"]["monthly_cost_usd"] == pytest.approx(145.32 * 1.15, abs=0.01)

    def test_lambda_prompt_flips_architecture(self):
        result = _mock_infracost_with_feedback("job-1", "use lambda instead", None)
        assert result["architecture_recommendation"] == "lambda"

    def test_ec2_prompt_flips_architecture(self):
        result = _mock_infracost_with_feedback("job-1", "give me an ec2 vm", None)
        assert result["architecture_recommendation"] == "ec2"

    def test_feedback_is_recorded_in_breakdown_rounds(self):
        result = _mock_infracost_with_feedback(
            "job-1", "make it cheaper", None
        )
        assert result["breakdown_rounds"][-1]["prompt"] == "make it cheaper"
        assert result["justification"].startswith("Regenerated")
