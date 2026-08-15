"""
DevGuard AI - Orchestrator Error Handling & Retry
===================================================
Safe wrapper applied to every graph node, plus the retry/backoff policy
required by US-2.2.4 (T-2.18).

Split out of graph.py (originally Section 2); retry logic added in T-2.18.

Owner: Hbib (Subgroup 2 - Execution & Control)

RETRY POLICY (US-2.2.4)
-----------------------
"Given a failed agent call, When error occurs, Then orchestrator retries up
to 3 times with exponential backoff before escalating."

Three things matter here, and the third is the one that's easy to get wrong:

1. WHAT gets retried. Only the three agent nodes. Retrying human_gate_*
   would be meaningless (they don't fail, they pause), and retrying
   generate_report would just repeat a pure local computation.

2. WHICH errors get retried. Retrying is only useful for TRANSIENT failures
   (network blips, rate limits, an AWS API 503). Retrying a deterministic
   failure - a malformed payload, a repo with no Dockerfile, an unsupported
   compute type - burns ~7 seconds of backoff to reproduce the exact same
   error three times. Those raise ValueError/TypeError/KeyError and are
   escalated immediately. See _is_retryable().

3. HOW state is handled between attempts. Nodes mutate the state dict they
   are handed. A node that fails halfway leaves partial mutations behind
   (status flipped to "deploying", node name already appended to
   nodes_executed). Attempt 2 would then start from that dirty state and,
   for nodes_executed, append a second time - the very metadata the
   dashboard uses to show progress. Each attempt therefore runs against a
   deep copy, and only a SUCCESSFUL attempt's state is kept.
"""

from __future__ import annotations

import copy
import logging
import time
from datetime import datetime, timezone
from typing import Callable

from langgraph.errors import GraphInterrupt

from .state import ErrorEntry, OrchestratorState

logger = logging.getLogger(__name__)


# =============================================================================
# RETRY CONFIGURATION
# =============================================================================

MAX_ATTEMPTS = 3

# Exponential backoff: 1s, then 2s, then 4s (the last one is never actually
# slept, since there's no attempt after the final failure).
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_FACTOR = 2.0

# Nodes whose failures are worth retrying. Human gates and report generation
# are deliberately absent - see the module docstring.
RETRYABLE_NODES = frozenset({"codesec_agent", "infracost_agent", "deployops_agent"})

# Deterministic failures: retrying reproduces them exactly. Escalate at once.
NON_RETRYABLE_EXCEPTIONS = (
    ValueError,      # bad payload, unsupported compute type, missing Dockerfile
    TypeError,       # contract mismatch between agents
    KeyError,        # missing required field
    AttributeError,  # calling something an agent doesn't expose
    NotImplementedError,
    ImportError,     # a real agent module isn't merged into this branch yet
)


def _backoff_delay(attempt: int) -> float:
    """Seconds to wait before the attempt following `attempt` (1-indexed)."""
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
    """Append one attempt's failure to the error log.

    `resolved=True` marks a failure that a later attempt recovered from -
    it stays in the log for diagnostics but must not be read as a workflow
    failure by the dashboard.
    """
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


# =============================================================================
# NODE WRAPPER
# =============================================================================

def safe_node_wrapper(
    node_func: Callable[[OrchestratorState], OrchestratorState],
    node_name: str,
    state: OrchestratorState,
) -> OrchestratorState:
    """
    Run a graph node with error capture and, for agent nodes, retry/backoff.

    Catches exceptions, retries transient ones up to MAX_ATTEMPTS with
    exponential backoff, and on final failure logs the error and marks the
    workflow "failed" rather than letting the exception escape and kill the
    whole process.

    IMPORTANT: Does NOT catch GraphInterrupt - that is LangGraph's mechanism
    for human-in-the-loop pauses, not an error. Catching it (or retrying it)
    would break approve/reject entirely.
    """
    if state.get("status") == "failed":
        logger.warning(f"[{state['job_id']}] Skipping {node_name} - workflow already failed")
        return state

    job_id = state["job_id"]
    max_attempts = MAX_ATTEMPTS if node_name in RETRYABLE_NODES else 1
    pending_errors: list[tuple[Exception, int]] = []

    for attempt in range(1, max_attempts + 1):
        # Each attempt gets a clean copy, so a half-mutated state from a
        # failed attempt can't leak into the next one (duplicated entries in
        # nodes_executed, a status left mid-flight, etc.).
        attempt_state = copy.deepcopy(state) if max_attempts > 1 else state

        try:
            result = node_func(attempt_state)
        except GraphInterrupt:
            # Human gate pausing the workflow. Never an error, never retried.
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

            # Escalate: either non-retryable, or attempts exhausted.
            if not retryable and node_name in RETRYABLE_NODES:
                logger.error(
                    f"[{job_id}] {node_name} failed with a non-retryable "
                    f"{type(exc).__name__}: {exc} - escalating without retry"
                )
            else:
                logger.error(
                    f"[{job_id}] {node_name} failed after {attempt} attempt(s): {exc}"
                )

            # Earlier attempts are logged as unresolved too: the node never
            # recovered, so every attempt genuinely failed.
            for earlier_exc, earlier_attempt in pending_errors:
                _record_error(state, node_name, earlier_exc, earlier_attempt, resolved=False)
            _record_error(state, node_name, exc, attempt, resolved=False)

            state["status"] = "failed"
            state["updated_at"] = datetime.now(timezone.utc).isoformat()
            return state

        # Success. Carry over any earlier failures for diagnostics, flagged
        # resolved so nothing downstream mistakes them for a real failure.
        if pending_errors:
            logger.info(
                f"[{job_id}] {node_name} recovered on attempt {attempt}/{max_attempts}"
            )
            for earlier_exc, earlier_attempt in pending_errors:
                _record_error(result, node_name, earlier_exc, earlier_attempt, resolved=True)

        return result

    # Unreachable: the loop either returns or escalates.
    return state
