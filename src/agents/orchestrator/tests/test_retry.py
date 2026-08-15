"""
Tests for the retry/backoff policy (T-2.18 / US-2.2.4)

US-2.2.4: "Given a failed agent call, When error occurs, Then orchestrator
retries up to 3 times with exponential backoff before escalating."

Sleeps are patched out (monkeypatching error_handlers.time.sleep) so the
suite stays fast; the delays themselves are asserted separately against
_backoff_delay rather than by wall-clock timing, which would be flaky in CI.
"""

import pytest
from langgraph.errors import GraphInterrupt
from langgraph.types import Interrupt

from src.agents.orchestrator import error_handlers
from src.agents.orchestrator.error_handlers import (
    BACKOFF_BASE_SECONDS,
    MAX_ATTEMPTS,
    RETRYABLE_NODES,
    _backoff_delay,
    _is_retryable,
    safe_node_wrapper,
)
from src.agents.orchestrator.state import create_initial_state


@pytest.fixture
def state():
    return create_initial_state("https://github.com/test/repo")


@pytest.fixture
def slept(monkeypatch):
    delays: list[float] = []
    monkeypatch.setattr(error_handlers.time, "sleep", delays.append)
    return delays


class TestBackoffCurve:
    def test_delays_are_exponential(self):
        assert _backoff_delay(1) == BACKOFF_BASE_SECONDS
        assert _backoff_delay(2) == BACKOFF_BASE_SECONDS * 2
        assert _backoff_delay(3) == BACKOFF_BASE_SECONDS * 4

    def test_max_attempts_is_three_per_cdc(self):
        """US-2.2.4 says "up to 3 times"."""
        assert MAX_ATTEMPTS == 3


class TestWhatIsRetryable:
    def test_agent_nodes_are_retryable(self):
        assert RETRYABLE_NODES == {"codesec_agent", "infracost_agent", "deployops_agent"}

    def test_transient_error_on_agent_node_is_retryable(self):
        assert _is_retryable("codesec_agent", ConnectionError("503")) is True

    def test_deterministic_error_is_not_retryable(self):
        """Retrying a bad payload just reproduces it three times."""
        assert _is_retryable("deployops_agent", ValueError("no Dockerfile")) is False
        assert _is_retryable("deployops_agent", KeyError("ecs_cluster")) is False
        assert _is_retryable("deployops_agent", TypeError("bad shape")) is False

    def test_non_agent_node_is_never_retried(self):
        assert _is_retryable("generate_report", ConnectionError("x")) is False
        assert _is_retryable("human_gate_1", ConnectionError("x")) is False


class TestRetrySucceeds:
    def test_recovers_on_third_attempt(self, state, slept):
        calls = {"n": 0}

        def flaky(s):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("AWS API 503")
            s["status"] = "analyzing"
            return s

        result = safe_node_wrapper(flaky, "codesec_agent", state)

        assert calls["n"] == 3
        assert result["status"] == "analyzing"
        assert slept == [1.0, 2.0]

    def test_earlier_failures_are_logged_as_resolved(self, state, slept):
        """Kept for diagnostics, but must not read as a workflow failure."""
        calls = {"n": 0}

        def flaky(s):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("blip")
            return s

        result = safe_node_wrapper(flaky, "infracost_agent", state)

        assert len(result["error_log"]) == 2
        assert all(e["resolved"] is True for e in result["error_log"])
        assert [e["attempt"] for e in result["error_log"]] == [1, 2]

    def test_state_is_not_polluted_by_failed_attempts(self, state, slept):
        """
        A node that dies mid-way has already mutated the state it was given.
        Without isolation, attempt 2 would append to nodes_executed a second
        time - corrupting the very metadata the dashboard reads for progress.
        """
        calls = {"n": 0}

        def flaky(s):
            calls["n"] += 1
            s["orchestrator_metadata"]["nodes_executed"].append("codesec_agent")
            if calls["n"] < 3:
                raise ConnectionError("dies after mutating")
            return s

        result = safe_node_wrapper(flaky, "codesec_agent", state)

        assert result["orchestrator_metadata"]["nodes_executed"] == ["codesec_agent"]


class TestRetryExhausted:
    def test_escalates_after_three_attempts(self, state, slept):
        calls = {"n": 0}

        def always_down(s):
            calls["n"] += 1
            raise ConnectionError("network down")

        result = safe_node_wrapper(always_down, "deployops_agent", state)

        assert calls["n"] == MAX_ATTEMPTS
        assert result["status"] == "failed"
        assert slept == [1.0, 2.0]

    def test_every_attempt_is_logged_unresolved(self, state, slept):
        def always_down(s):
            raise ConnectionError("network down")

        result = safe_node_wrapper(always_down, "deployops_agent", state)

        assert len(result["error_log"]) == MAX_ATTEMPTS
        assert all(e["resolved"] is False for e in result["error_log"])
        assert [e["attempt"] for e in result["error_log"]] == [1, 2, 3]
        assert all(e["max_attempts"] == MAX_ATTEMPTS for e in result["error_log"])


class TestNoPointlessRetry:
    def test_deterministic_error_escalates_immediately(self, state, slept):
        """No burning ~3s of backoff to reproduce the same ValueError twice."""
        calls = {"n": 0}

        def bad_payload(s):
            calls["n"] += 1
            raise ValueError("ECS deployment requires a Dockerfile")

        result = safe_node_wrapper(bad_payload, "deployops_agent", state)

        assert calls["n"] == 1
        assert result["status"] == "failed"
        assert slept == []

    def test_non_agent_node_is_not_retried(self, state, slept):
        calls = {"n": 0}

        def report_fails(s):
            calls["n"] += 1
            raise ConnectionError("x")

        result = safe_node_wrapper(report_fails, "generate_report", state)

        assert calls["n"] == 1
        assert result["status"] == "failed"
        assert result["error_log"][0]["max_attempts"] == 1
        assert slept == []


class TestGraphInterruptIsNeverRetried:
    def test_interrupt_propagates_on_first_raise(self, state, slept):
        """Retrying a human gate would break approve/reject entirely."""
        calls = {"n": 0}

        def gate(s):
            calls["n"] += 1
            raise GraphInterrupt(interrupts=[Interrupt(value={"gate": "gate_1"})])

        with pytest.raises(GraphInterrupt):
            safe_node_wrapper(gate, "codesec_agent", state)

        assert calls["n"] == 1
        assert slept == []
        assert state["status"] != "failed"
        assert state["error_log"] == []


class TestAlreadyFailedWorkflow:
    def test_node_is_skipped_entirely(self, state, slept):
        state["status"] = "failed"

        def should_not_run(s):
            raise AssertionError("must not be called")

        result = safe_node_wrapper(should_not_run, "codesec_agent", state)

        assert result["status"] == "failed"
        assert result["error_log"] == []
