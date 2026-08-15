"""
DevGuard AI - Orchestrator Agent
LangGraph-based workflow orchestrator for the DevSecOps pipeline.

This module wires the graph together: nodes (nodes.py), human gates
(human_gates.py), state shape (state.py), and error handling
(error_handlers.py). It also exposes the public API used by the backend:
run_workflow() and resume_workflow().

- CodeSec Agent (Nada) -> Security analysis
- InfraCost Agent (Karim) -> Infrastructure & cost estimation
- DeployOps Agent (Oussema) -> Deployment & health checks
- Human Approval Gates -> Human-in-the-loop validation
- Chat LLM -> Conversational interface with job context
- Error Handler -> Retry logic with exponential backoff

IMPORTANT: codesec_result uses the EXACT payload format from Nada's
codesec-mock-schema.json (Option 1: "Accept everything from Nada" - no
transformation, direct passthrough).

CDC Reference: Section 4.2 (Epic 2.2: Orchestrator Agent)

Owner: Hbib (Subgroup 2 - Execution & Control)
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from langgraph.graph import StateGraph, END
import sqlite3
from langgraph.checkpoint.sqlite import SqliteSaver
from .state import GRAPH_VERSION, OrchestratorState, create_initial_state
from .error_handlers import safe_node_wrapper
from .human_gates import human_gate_1_impl, human_gate_2_impl
from .nodes import (
    codesec_agent_impl,
    infracost_agent_impl,
    deployops_agent_impl,
    health_check_impl,
    generate_report_impl,
    route_after_codesec,
    route_after_gate_1,
    route_after_infracost,
    route_after_gate_2,
    route_after_deployops,
    route_after_health_check,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# =============================================================================
# PERSISTENT CHECKPOINTER (replaces MemorySaver - T-4.3)
# =============================================================================
# MemorySaver kept every paused job's state only inside the current process:
# any backend restart (a crash, a redeploy, even `uvicorn --reload`) silently
# lost every job waiting at a human gate, and a fresh process had no way to
# resume a thread_id checkpointed by a dead process.
#
# SqliteSaver persists the same checkpoints to a file on disk, so a freshly
# built graph in a brand new process can resume exactly where an earlier
# process left off. Chosen over PostgresSaver because it works immediately
# without a running Postgres (and matches devguard.db, the backend's own
# SQLite dev database); both implement BaseCheckpointSaver, so swapping to
# PostgresSaver later (T-5.12) is a one-line change.

CHECKPOINT_DB_PATH = os.getenv("DEVGUARD_CHECKPOINT_DB", "orchestrator_checkpoints.sqlite")

def _build_checkpointer() -> SqliteSaver:
    """
    check_same_thread=False: FastAPI's sync endpoints (jobs.py's create_job /
    approve_job) run in a threadpool, so different requests may reach this
    connection from different threads. WAL mode reduces "database is locked"
    contention under that - still SQLite-grade concurrency, not meant for
    heavy production load.
    """
    conn = sqlite3.connect(CHECKPOINT_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    saver = SqliteSaver(conn)
    saver.setup()
    return saver

# =============================================================================
# SECTION 7: GRAPH CONSTRUCTION
# =============================================================================


def build_orchestrator_graph() -> StateGraph:
    builder = StateGraph(OrchestratorState)

    builder.add_node(
        "codesec_agent",
        lambda s: safe_node_wrapper(codesec_agent_impl, "codesec_agent", s)
    )
    builder.add_node(
        "human_gate_1",
        lambda s: safe_node_wrapper(human_gate_1_impl, "human_gate_1", s)
    )
    builder.add_node(
        "infracost_agent",
        lambda s: safe_node_wrapper(infracost_agent_impl, "infracost_agent", s)
    )
    builder.add_node(
        "human_gate_2",
        lambda s: safe_node_wrapper(human_gate_2_impl, "human_gate_2", s)
    )
    builder.add_node(
        "deployops_agent",
        lambda s: safe_node_wrapper(deployops_agent_impl, "deployops_agent", s)
    )
    builder.add_node(
        "health_check",
        lambda s: safe_node_wrapper(health_check_impl, "health_check", s)
    )
    builder.add_node(
        "generate_report",
        lambda s: safe_node_wrapper(generate_report_impl, "generate_report", s)
    )

    builder.set_entry_point("codesec_agent")

    builder.add_conditional_edges(
        "codesec_agent",
        route_after_codesec,
        {"human_gate_1": "human_gate_1", "end": END}
    )
    builder.add_conditional_edges(
        "human_gate_1",
        route_after_gate_1,
        {"infracost_agent": "infracost_agent", "end": END}
    )
    builder.add_conditional_edges(
        "infracost_agent",
        route_after_infracost,
        {"human_gate_2": "human_gate_2", "end": END}
    )
    builder.add_conditional_edges(
        "human_gate_2",
        route_after_gate_2,
        {
            "deployops_agent": "deployops_agent",
            "infracost_agent": "infracost_agent",
            "end": END,
        }
    )
    builder.add_conditional_edges(
        "deployops_agent",
        route_after_deployops,
        {"health_check": "health_check", "end": END}
    )
    builder.add_conditional_edges(
        "health_check",
        route_after_health_check,
        {"generate_report": "generate_report", "end": END}
    )
    builder.add_edge("generate_report", END)

    graph = builder.compile(checkpointer=_build_checkpointer())

    logger.info("Orchestrator graph compiled successfully (v%s).", GRAPH_VERSION)
    return graph


# =============================================================================
# SECTION 7.1: GRAPH SINGLETON
# =============================================================================
# The graph (and its checkpointer) must be built ONCE and reused: rebuilding
# per call creates a fresh, empty checkpoint store, so a job paused at an
# interrupt() would become unresumable. Built lazily on first use (typically
# once at FastAPI app startup).

_graph_singleton: Optional[StateGraph] = None


def get_orchestrator_graph() -> StateGraph:
    """
    Return the single shared, compiled graph, building it once (lazily) so
    checkpoints survive across run_workflow / resume_workflow calls in the
    same process. In FastAPI, prefer building once in a startup/lifespan
    hook rather than relying on this lazy fallback.
    """
    global _graph_singleton
    if _graph_singleton is None:
        _graph_singleton = build_orchestrator_graph()
    return _graph_singleton


def reset_orchestrator_graph() -> None:
    """
    Force the next get_orchestrator_graph() call to rebuild a fresh graph and
    checkpointer. Useful for tests that need isolated checkpoint state.
    """
    global _graph_singleton
    _graph_singleton = None


# =============================================================================
# SECTION 8: PUBLIC API
# =============================================================================

def run_workflow(
    repo_url: str,
    thread_id: Optional[str] = None,
    on_node_progress: Optional[Callable[[str, dict], None]] = None,
) -> OrchestratorState:
    """
    Run the complete orchestrator workflow for a repository.

    For human gates, execution pauses at interrupt points; resume with
    resume_workflow(thread_id, resume_data) below - do NOT call run_workflow()
    again for the same job, it would start a brand new job with a new job_id.

    on_node_progress: optional callback invoked as each graph node completes,
    (node_name, state_snapshot). The backend uses it to stream per-node
    progress over WebSocket instead of only coarse milestones.
    """
    graph = get_orchestrator_graph()
    state = create_initial_state(repo_url, job_id=thread_id)
    config = {"configurable": {"thread_id": thread_id or state["job_id"]}}

    logger.info(f"Starting workflow for job {state['job_id']} | repo: {repo_url}")

    try:
        final_state = None
        for event in graph.stream(state, config):
            for node_name, node_state in event.items():
                if node_name == "__interrupt__":
                    if final_state is None:
                        final_state = dict(state)
                    final_state["__interrupt__"] = list(node_state)
                    if on_node_progress:
                        on_node_progress("human_gate", dict(node_state=list(node_state)))
                else:
                    final_state = node_state
                    if on_node_progress:
                        on_node_progress(node_name, node_state)
        if final_state is None:
            final_state = dict(state)
        logger.info(f"Workflow completed for job {state['job_id']} | status: {final_state['status']}")
        return final_state
    except Exception as e:
        logger.error(f"Workflow failed for job {state['job_id']}: {e}")
        state["status"] = "failed"
        state["error_log"].append({
            "node": "orchestrator",
            "attempt": 1,
            "max_attempts": 1,
            "message": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stack_trace": str(e.__traceback__) if e.__traceback__ else None,
            "resolved": False,
        })
        return state


def resume_workflow(
    thread_id: str,
    resume_data: dict,
    on_node_progress: Optional[Callable[[str, dict], None]] = None,
) -> OrchestratorState:
    """
    Resume a workflow paused at a human gate (interrupt()).

    thread_id must match the one used in the original run_workflow() call
    (defaults to the job_id if none was passed explicitly). resume_data is
    handed back as the return value of interrupt(...) inside the paused node,
    e.g. {"approved": True, "comment": "OK", "approved_by": "user@email.com"}.
    """
    from langgraph.types import Command

    graph = get_orchestrator_graph()
    config = {"configurable": {"thread_id": thread_id}}

    logger.info(f"Resuming workflow for thread {thread_id} with resume_data: {resume_data}")

    try:
        final_state = None
        for event in graph.stream(Command(resume=resume_data), config):
            for node_name, node_state in event.items():
                if node_name == "__interrupt__":
                    if final_state is None:
                        final_state = dict(get_orchestrator_graph().get_state(config).values)
                    final_state["__interrupt__"] = list(node_state)
                    if on_node_progress:
                        on_node_progress("human_gate", dict(node_state=list(node_state)))
                else:
                    final_state = node_state
                    if on_node_progress:
                        on_node_progress(node_name, node_state)
        if final_state is None:
            final_state = dict(get_orchestrator_graph().get_state(config).values)
        logger.info(f"Workflow resumed for thread {thread_id} | status: {final_state.get('status')}")
        return final_state
    except Exception as e:
        logger.error(f"Resuming workflow failed for thread {thread_id}: {e}")
        raise


# =============================================================================
# SECTION 9: MAIN (for testing)
# =============================================================================

# Development-only demo. Not part of the shipped code path: enable explicitly
# with DEVGUARD_DEMO=1 to run the manual "test run" walkthrough.
if __name__ == "__main__":
    if os.getenv("DEVGUARD_DEMO") != "1":
        raise SystemExit(
            "This is a development demo. Set DEVGUARD_DEMO=1 to run it, e.g. "
            "DEVGUARD_DEMO=1 python -m src.agents.orchestrator.graph"
        )

    test_repo = "https://github.com/NadaBhm/devguard-ai"

    print("=" * 60)
    print("DevGuard AI - Orchestrator Test Run")
    print(f"Version: {GRAPH_VERSION} (adapters + retry + chat + report)")
    print("=" * 60)

    state = create_initial_state(test_repo)
    print(f"\n[TEST 1] Initial state created:")
    print(f"  Job ID: {state['job_id']}")
    print(f"  Repo: {state['repo_url']}")
    print(f"  Status: {state['status']}")
    print(f"  Graph Version: {state['orchestrator_metadata']['graph_version']}")

    print(f"\n[TEST 2] Building graph...")
    graph = build_orchestrator_graph()
    print("  Graph compiled successfully!")

    print(f"\n[TEST 3] Running workflow (will pause at human gates)...")
    print("  Note: Human gates use LangGraph interrupt() mechanism.")
    print("  To resume, use: graph.invoke(Command(resume={...}), config)")

    config = {"configurable": {"thread_id": state["job_id"]}}

    try:
        for event in graph.stream(state, config):
            for node_name, node_state in event.items():
                if node_name == "__interrupt__":
                    # langgraph >=1.x streams a real Interrupt object here
                    # (with a .value attribute), not a plain dict - so we
                    # need attribute access on the Interrupt itself, then
                    # dict-style access into its .value payload.
                    interrupt_payload = node_state[0].value
                    print(f"\n  \u23f8\ufe0f  INTERRUPT at gate: {interrupt_payload['gate']}")
                    print(f"     Message: {interrupt_payload['message']}")
                    print(f"     Actions: {interrupt_payload['actions']}")
                    print(f"\n  To resume, call:")
                    print(f"    from langgraph.types import Command")
                    print(f"    graph.invoke(Command(resume={{'approved': True, ...}}), config)")
                    break
                else:
                    status = node_state.get("status", "unknown")
                    print(f"  \u2705 Node '{node_name}' completed | status: {status}")
    except Exception as e:
        print(f"  \u274c Error: {e}")

    print("\n" + "=" * 60)
    print("Test complete.")
    print("=" * 60)
