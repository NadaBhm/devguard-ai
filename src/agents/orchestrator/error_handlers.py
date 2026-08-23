"""
DevGuard AI - Orchestrator Error Handling & Retry
Safe wrapper for graph nodes with exponential backoff.
"""

from __future__ import annotations

import copy
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from langgraph.errors import GraphInterrupt

from .state import ErrorEntry, OrchestratorState

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_FACTOR = 2.0

RETRYABLE_NODES = frozenset({"codesec_agent", "infracost_agent", "deployops_agent"})
NON_RETRYABLE_EXCEPTIONS = (
    ValueError,
    TypeError,
    KeyError,
    AttributeError,
    NotImplementedError,
    ImportError,
)


def _backoff_delay(attempt: int) -> float:
    return BACKOFF_BASE_SECONDS * (BACKOFF_FACTOR ** (attempt - 1))


def _is_retryable(node_name: str, exc: Exception) -> bool:
    if node_name not in RETRYABLE_NODES:
        return False
    if isinstance(exc, NON_RETRYABLE_EXCEPTIONS):
        return False
    return True


def _record_error(
    state: OrchestratorState,
    node_name: str,
    exc: Exception,
    attempt: int,
    *,
    resolved: bool,
) -> None:
    entry: ErrorEntry = {
        "node": node_name,
        "attempt": attempt,
        "max_attempts": MAX_ATTEMPTS if node_name in RETRYABLE_NODES else 1,
        "message": str(exc),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stack_trace": str(exc.__traceback__) if exc.__traceback__ else None,
        "resolved": resolved,
    }
    state["error_log"].append(entry)


def safe_node_wrapper(
    node_func: Callable[[OrchestratorState], OrchestratorState],
    node_name: str,
    state: Any,
) -> OrchestratorState:
    """
    Run a graph node with error capture and retry/backoff for agent nodes.
    Does NOT catch GraphInterrupt (human-in-the-loop pauses).
    """
    if state.get("status") == "failed":
        logger.warning(f"[{state['job_id']}] Skipping {node_name} - workflow already failed")
        return state

    job_id = state["job_id"]
    max_attempts = MAX_ATTEMPTS if node_name in RETRYABLE_NODES else 1
    pending_errors: list[tuple[Exception, int]] = []

    for attempt in range(1, max_attempts + 1):
        attempt_state = copy.deepcopy(state) if max_attempts > 1 else state

        try:
            result = node_func(attempt_state)
        except GraphInterrupt:
            raise
        except Exception as exc:
            retryable = _is_retryable(node_name, exc)
            is_last = attempt >= max_attempts

            if retryable and not is_last:
                delay = _backoff_delay(attempt)
                logger.warning(
                    f"[{job_id}] {node_name} failed (attempt {attempt}/{max_attempts}): "
                    f"{exc} - retrying in {delay:.1f}s"
                )
                pending_errors.append((exc, attempt))
                time.sleep(delay)
                continue

            if not retryable and node_name in RETRYABLE_NODES:
                logger.error(
                    f"[{job_id}] {node_name} failed with a non-retryable "
                    f"{type(exc).__name__}: {exc} - escalating without retry"
                )
            else:
                logger.error(f"[{job_id}] {node_name} failed after {attempt} attempt(s): {exc}")

            for earlier_exc, earlier_attempt in pending_errors:
                _record_error(state, node_name, earlier_exc, earlier_attempt, resolved=False)
            _record_error(state, node_name, exc, attempt, resolved=False)

            state["status"] = "failed"
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            return state

        if pending_errors:
            logger.info(f"[{job_id}] {node_name} recovered on attempt {attempt}/{max_attempts}")
            for earlier_exc, earlier_attempt in pending_errors:
                _record_error(result, node_name, earlier_exc, earlier_attempt, resolved=True)

        return result

    return state