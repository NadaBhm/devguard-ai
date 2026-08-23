"""
DevGuard AI - Orchestrator Agent: LangGraph workflow wiring nodes.py, human_gates.py, state.py,
and error_handlers.py; exposes the backend API run_workflow()/resume_workflow().
Pipeline: CodeSec -> InfraCost -> DeployOps with human approval gates, a chat LLM, and a
retrying error handler.
IMPORTANT: codesec_result passes through Nada's codesec-mock-schema.json payload untransformed.
CDC Reference: Section 4.2 (Epic 2.2). Owner: Hbib (Subgroup 2 - Execution & Control).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Optional, cast

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


# === PERSISTENT CHECKPOINTER (replaces MemorySaver - T-4.3) ===
# MemorySaver held paused jobs only in-process: a backend restart lost every gate-waiting job.
# SqliteSaver persists checkpoints to disk so fresh processes resume; needs no Postgres (T-5.12).

CHECKPOINT_DB_PATH = os.getenv("DEVGUARD_CHECKPOINT_DB", "orchestrator_checkpoints.sqlite")

def _build_checkpointer() -> SqliteSaver:
    """
    check_same_thread=False: FastAPI sync endpoints run in a threadpool, so different requests
    reach this connection from different threads; WAL mode reduces "database is locked" contention
    under that (still SQLite-grade concurrency, not for heavy production load).
    """
    conn = sqlite3.connect(CHECKPOINT_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    saver = SqliteSaver(conn)
    saver.setup()
    return saver

# === GRAPH CONSTRUCTION ===


def _wrap_node(node_func, node_name: str):
    def _node(state: OrchestratorState, config: Any = None) -> OrchestratorState:
        return safe_node_wrapper(node_func, node_name, state)
    return _node


def build_orchestrator_graph() -> Any:
    builder = StateGraph(OrchestratorState)

    builder.add_node("codesec_agent", _wrap_node(codesec_agent_impl, "codesec_agent"))
    builder.add_node("human_gate_1", _wrap_node(human_gate_1_impl, "human_gate_1"))
    builder.add_node("infracost_agent", _wrap_node(infracost_agent_impl, "infracost_agent"))
    builder.add_node("human_gate_2", _wrap_node(human_gate_2_impl, "human_gate_2"))
    builder.add_node("deployops_agent", _wrap_node(deployops_agent_impl, "deployops_agent"))
    builder.add_node("health_check", _wrap_node(health_check_impl, "health_check"))
    builder.add_node("generate_report", _wrap_node(generate_report_impl, "generate_report"))

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


# === GRAPH SINGLETON ===
# Build the graph/checkpointer ONCE and reuse it: rebuilding per call creates a fresh, empty
# checkpoint store, making jobs paused at interrupt() unresumable. Built lazily on first use.

_graph_singleton: Optional[Any] = None


def get_orchestrator_graph() -> Any:
    """
    Return the single shared, compiled graph, building it once (lazily) so checkpoints survive
    across run_workflow / resume_workflow calls. In FastAPI, prefer building once in a
    startup/lifespan hook rather than relying on this lazy fallback.
    """
    global _graph_singleton
    if _graph_singleton is None:
        _graph_singleton = build_orchestrator_graph()
    return _graph_singleton


def reset_orchestrator_graph() -> None:
    """
    Force the next get_orchestrator_graph() call to rebuild; useful for tests needing
    isolated checkpoint state.
    """
    global _graph_singleton
    _graph_singleton = None


# === PUBLIC API ===

def _get_current_state(graph, config, fallback_state: Any) -> Any:
    """Safely fetch the latest checkpointed state from the graph."""
    try:
        snapshot = graph.get_state(config)
        return dict(snapshot.values) if snapshot else fallback_state
    except Exception:
        return fallback_state


def run_workflow(
    repo_url: str,
    thread_id: Optional[str] = None,
    on_node_progress: Optional[Callable[[str, dict], None]] = None,
    *,
    is_update: bool = False,
    existing_deployment: Optional[dict] = None,
    previous_monthly_cost_usd: Optional[float] = None,
) -> OrchestratorState:
    """
    Run the complete orchestrator workflow for a repository; pauses at human gates -- resume via
    resume_workflow(thread_id, resume_data), never by re-calling run_workflow() (new job_id).
    on_node_progress(node, state) streams per-node progress; is_update/existing_deployment/
    previous_monthly_cost_usd arrive pre-resolved from the DB for update runs (state.py).
    """
    graph = get_orchestrator_graph()
    state = create_initial_state(
        repo_url,
        job_id=thread_id,
        is_update=is_update,
        existing_deployment=existing_deployment,
        previous_monthly_cost_usd=previous_monthly_cost_usd,
    )
    config = {"configurable": {"thread_id": thread_id or state["job_id"]}}

    logger.info(f"Starting workflow for job {state['job_id']} | repo: {repo_url}")

    try:
        final_state = None
        for event in graph.stream(state, config):
            for node_name, node_state in event.items():
                if node_name == "__interrupt__":
                    # CRITICAL FIX: fetch the REAL checkpointed state, not the initial input,
                    # or all pre-gate progress is lost and resumption breaks.
                    final_state = _get_current_state(graph, config, state)

                    # Normalize LangGraph >=1.x Interrupt objects to plain dicts
                    interrupt_values = [
                        i.value if hasattr(i, "value") else i for i in node_state
                    ]
                    final_state["__interrupt__"] = interrupt_values

                    if on_node_progress:
                        on_node_progress("human_gate", dict(node_state=interrupt_values))
                else:
                    final_state = node_state
                    if on_node_progress:
                        on_node_progress(node_name, node_state)

        if final_state is None:
            final_state = dict(state)

        logger.info(f"Workflow completed for job {state['job_id']} | status: {final_state['status']}")
        return cast(OrchestratorState, final_state)

    except Exception as e:
        logger.error(f"Workflow failed for job {state['job_id']}: {e}")
        # FIX: recover latest checkpointed state so we don't wipe node results
        error_state = _get_current_state(graph, config, state)
        error_state["status"] = "failed"
        error_state["error_log"].append({
            "node": "orchestrator",
            "attempt": 1,
            "max_attempts": 1,
            "message": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stack_trace": str(e.__traceback__) if e.__traceback__ else None,
            "resolved": False,
        })
        return cast(OrchestratorState, error_state)


def resume_workflow(
    thread_id: str,
    resume_data: dict,
    on_node_progress: Optional[Callable[[str, dict], None]] = None,
) -> OrchestratorState:
    """
    Resume a workflow paused at a human gate (interrupt()). thread_id must match the original
    run_workflow() call; resume_data is handed back as interrupt(...)'s return value, e.g.
    {"approved": True, "comment": "OK", "approved_by": "user@email.com"}.
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
                    final_state = _get_current_state(graph, config, {})

                    interrupt_values = [
                        i.value if hasattr(i, "value") else i for i in node_state
                    ]
                    final_state["__interrupt__"] = interrupt_values

                    if on_node_progress:
                        on_node_progress("human_gate", dict(node_state=interrupt_values))
                else:
                    final_state = node_state
                    if on_node_progress:
                        on_node_progress(node_name, node_state)

        if final_state is None:
            final_state = _get_current_state(graph, config, {})

        logger.info(f"Workflow resumed for thread {thread_id} | status: {final_state.get('status')}")
        return cast(OrchestratorState, final_state)

    except Exception as e:
        logger.error(f"Resuming workflow failed for thread {thread_id}: {e}")
        raise


# === MAIN (for testing) ===

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
                    # langgraph >=1.x streams a real Interrupt object (with a .value attribute),
                    # not a plain dict -- access .value, then dict-style into its payload.
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