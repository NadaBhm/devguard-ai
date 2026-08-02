"""
DevGuard AI - Orchestrator Error Handling
===========================================
Safe wrapper applied to every graph node so that an exception in one node
doesn't crash the whole process - it's caught, logged into error_log, and
the workflow status is marked "failed".

Split out of graph.py (originally Section 2).

Owner: Hbib (Subgroup 2 - Execution & Control)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from langgraph.errors import GraphInterrupt

from .state import ErrorEntry, OrchestratorState

logger = logging.getLogger(__name__)


def safe_node_wrapper(
    node_func: Callable[[OrchestratorState], OrchestratorState],
    node_name: str,
    state: OrchestratorState,
) -> OrchestratorState:
    """
    Safe wrapper for graph nodes.
    Catches exceptions, logs them, marks status as failed, and returns state.
    NO time.sleep() - async-friendly.

    IMPORTANT: Does NOT catch GraphInterrupt - this is LangGraph's internal
    mechanism for human-in-the-loop pauses. Catching it would break the
    interrupt/resume functionality.
    """
    if state.get("status") == "failed":
        logger.warning(f"[{state['job_id']}] Skipping {node_name} - workflow already failed")
        return state

    try:
        result = node_func(state)
        return result
    except GraphInterrupt:
        # Re-raise immediately - this is NOT an error, it's LangGraph's
        # mechanism for pausing the workflow at human approval gates.
        # The orchestrator resumes via graph.invoke(Command(resume={...}), config)
        raise
    except Exception as e:
        logger.error(f"[{state['job_id']}] {node_name} failed: {e}")

        error_entry: ErrorEntry = {
            "node": node_name,
            "attempt": 1,
            "max_attempts": 3,
            "message": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stack_trace": str(e.__traceback__) if e.__traceback__ else None,
            "resolved": False,
        }

        state["error_log"].append(error_entry)
        state["status"] = "failed"
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        return state
