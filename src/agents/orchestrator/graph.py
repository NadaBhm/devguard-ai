"""
DevGuard AI - Orchestrator Agent
LangGraph-based workflow orchestrator for the DevSecOps pipeline.

Owner: Hbib (Subgroup 2 - Execution & Control)

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

IMPORTANT: codesec_result uses the EXACT payload format from Nada's codesec-mock-schema.json
Option 1: "Accept everything from Nada" - no transformation, direct passthrough.

CDC Reference: Section 4.2 (Epic 2.2: Orchestrator Agent)

CHANGELOG:
- v1.0.1: Fixed error handling (removed time.sleep), secured dict access, added route guards
- v1.0.1: Added safe_node_wrapper for automatic error catching in graph nodes
- v1.0.1: Added pipeline timeout tracking
- v1.0.1: Fixed error_log resolved flag logic
- v1.0.1: Added conditional routing with failure checks
- v1.0.2: Aligned DeployOpsResult with deployops-mock-schema.json (Oussema)
- v1.0.2: Added job_id to DeployOpsResult (required by schema)
- v1.0.2: Changed terraform_outputs from Optional[dict] to dict (required by schema)
- v1.0.2: Added HealthCheckResult TypedDict for strict typing
- v1.0.2: Added job_id to mock_deployops_agent_impl payload
- v1.0.3: Fixed GraphInterrupt being swallowed by the generic except in _safe_node_wrapper
- v1.0.3: Fixed run_workflow / docs to use the real Command(resume=...) API
- v1.0.4: Fixed _safe_node_wrapper signature (removed stray extra "str" parameter
          that shadowed the built-in str() and caused a TypeError on every node call)
- v1.0.4: Fixed _human_gate_2_impl to use status "rejected" instead of "failed"
          on human rejection, consistent with _human_gate_1_impl
- v1.3.1: InfraCost API on master reverted to run_pipeline_with_context() +
          core.orchestrator_adapter.to_orchestrator_result() (the API v1.3.0
          replaced no longer matches master - see agent_adapters.py's module
          docstring on the churn). This build ALSO generates real Dockerfile
          content (output_builder.resolve_docker_artifacts), unblocking the
          DeployOps translation that v1.3.0's build could not complete.
          normalize_infracost_result() now does real work again: fixes
          cost_estimate.amount -> .monthly_cost_usd, a real gap in Karim's
          own to_orchestrator_result() caught by test_schema_conformance.py.
- v1.3.0: agent_adapters.py rewritten for InfraCost's REAL API on master.
          run_pipeline_with_context()/core.orchestrator_adapter never existed
          on master - only run_pipeline() -> InfraCostOutput. Also surfaced a
          real integration gap: real InfraCost never provides Dockerfile
          CONTENT (only a source_code path, which is CodeSec's clone -
          already deleted by the time DeployOps needs it). The translator
          now fails loudly on this instead of silently sending an unusable
          payload. Needs a team decision (see agent_adapters.py docstring).
- v1.2.1: InfraCost output is normalized to the documented schema shape
          (agent_adapters.normalize_infracost_result). The real agent emits
          Money.amount / files{} / projected_monthly_savings, none of which
          match orchestrator-input-schema.json - unnormalized, the final
          report told stakeholders the deployment cost $0/month. Schema also
          gained "rejected" status and __interrupt__, both of which the
          orchestrator already produced but the schema rejected.
- v1.2.0: Final report generation (T-3.12 / US-2.2.6) in report.py.
          Jinja2 -> HTML always; WeasyPrint -> PDF best-effort (its native
          libs are absent on some dev machines). Inline SVG architecture
          diagram, no extra dependency. Secret VALUES are never rendered.
- v1.1.0: Chat with conversation memory (T-3.10 / T-3.11, US-2.2.5) in
          chat.py. Combines orchestrator job results with Nada's RAG repo
          retrieval; memory is orchestrator-side because lib/rag is stateless.
- v1.0.8: Retry with exponential backoff on agent nodes (T-2.18 / US-2.2.4).
          Transient failures retry up to 3x (1s/2s); deterministic ones
          (ValueError/TypeError/KeyError) escalate immediately; each attempt
          runs on an isolated state copy so partial mutations can't leak.
- v1.0.7: Agent nodes now go through agent_adapters.py (T-2.17 / T-3.16).
          The three mock_*_agent_impl functions are kept in nodes.py and are
          still what the adapters return in mock mode (the default), so
          behavior is unchanged until DEVGUARD_REAL_* is switched on.
- v1.0.6: Split into state.py / error_handlers.py / human_gates.py / nodes.py.
          graph.py now only wires the graph together and exposes the public API.
          No behavior change - pure refactor.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

# LangGraph imports
from langgraph.graph import StateGraph, END
import os
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

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)






# =============================================================================
# PERSISTENT CHECKPOINTER (replaces MemorySaver - T-4.3)
# =============================================================================
# MemorySaver kept every paused job's state in a plain Python object, living
# only inside the current process. Any backend restart (a crash, a redeploy,
# even `uvicorn --reload` picking up a code change) silently lost every job
# waiting at a human gate - verified concretely today: a fresh process has no
# way to resume a thread_id checkpointed by a process that no longer exists.
#
# SqliteSaver persists the same checkpoints to a file on disk instead, so a
# freshly-built graph in a brand new process can resume exactly where an
# earlier process left off. Verified with a two-process test: process A ran
# up to a human gate and exited; process B, sharing nothing but the sqlite
# file, resumed and got the correct state back.
#
# Chosen over PostgresSaver for now because it works immediately without a
# running Postgres instance - useful today, and matches devguard.db (the
# backend's own SQLite dev database). Both checkpointers implement the same
# BaseCheckpointSaver interface, so swapping to PostgresSaver later (T-5.12,
# once a real Postgres is deployed) is a one-line change here, not a rewrite.

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
    saver.setup()  # creates the checkpoint tables if this file is new
    return saver

# =============================================================================
# SECTION 7: GRAPH CONSTRUCTION
# =============================================================================


def build_orchestrator_graph() -> StateGraph:
    """
    Build and compile the LangGraph state graph.

    NOTE: Uses MemorySaver for Sprint 1.
    TODO Sprint 2: Replace with PostgresSaver for persistence across restarts.
    """
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
        {"deployops_agent": "deployops_agent", "end": END}
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
# BUGFIX v1.0.5: run_workflow() used to call build_orchestrator_graph() on
# every invocation, which creates a brand new MemorySaver each time. Since
# MemorySaver keeps checkpoints only inside the object it was constructed
# with, a fresh graph means a fresh (empty) checkpoint store - any job
# paused at an interrupt() became unresumable, because the checkpoint that
# graph.invoke(Command(resume=...), config) needs to look up simply isn't
# there anymore. The graph (and its checkpointer) must be built ONCE and
# reused for every call - typically once at FastAPI app startup.

_graph_singleton: Optional[StateGraph] = None


def get_orchestrator_graph() -> StateGraph:
    """
    Return the single, shared, compiled orchestrator graph.
    Builds it once (lazily) and reuses it afterwards so that the
    MemorySaver checkpoints survive across separate run_workflow /
    resume_workflow calls within the same process.

    NOTE: In FastAPI, prefer building this once in a startup/lifespan
    hook and reusing that instance, rather than relying on the
    lazy-singleton fallback here.
    """
    global _graph_singleton
    if _graph_singleton is None:
        _graph_singleton = build_orchestrator_graph()
    return _graph_singleton


def reset_orchestrator_graph() -> None:
    """
    Force the next get_orchestrator_graph() call to rebuild a fresh graph
    (and a fresh MemorySaver). Mostly useful for tests that need isolated
    checkpoint state between test cases.
    """
    global _graph_singleton
    _graph_singleton = None


# =============================================================================
# SECTION 8: PUBLIC API
# =============================================================================

def run_workflow(repo_url: str, thread_id: Optional[str] = None) -> OrchestratorState:
    """
    Run the complete orchestrator workflow for a repository.

    NOTE: For human gates, execution will pause at interrupt points.
    Resume with resume_workflow(thread_id, resume_data) below - do NOT
    call run_workflow() again for the same job, it would start a brand
    new job with a brand new job_id.

    BUGFIX v1.0.5: now uses get_orchestrator_graph() (a shared, cached
    graph instance) instead of build_orchestrator_graph() directly, so
    the MemorySaver checkpoint created here is still around when
    resume_workflow() is called later for the same thread_id.
    """
    graph = get_orchestrator_graph()
    state = create_initial_state(repo_url, job_id=thread_id)
    config = {"configurable": {"thread_id": thread_id or state["job_id"]}}

    logger.info(f"Starting workflow for job {state['job_id']} | repo: {repo_url}")

    try:
        final_state = graph.invoke(state, config)
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


def resume_workflow(thread_id: str, resume_data: dict) -> OrchestratorState:
    """
    Resume a workflow that is paused at a human gate (interrupt()).

    thread_id must match the one used in the original run_workflow() call
    (defaults to the job_id if none was passed explicitly).

    resume_data is handed back as the return value of interrupt(...) inside
    the paused node - e.g. for a human gate:
        {"approved": True, "comment": "OK", "approved_by": "user@email.com"}

    Example:
        result = resume_workflow(
            job_id,
            {"approved": True, "comment": "Looks good", "approved_by": "alice@company.com"},
        )
    """
    from langgraph.types import Command

    graph = get_orchestrator_graph()
    config = {"configurable": {"thread_id": thread_id}}

    logger.info(f"Resuming workflow for thread {thread_id} with resume_data: {resume_data}")

    try:
        final_state = graph.invoke(Command(resume=resume_data), config)
        logger.info(f"Workflow resumed for thread {thread_id} | status: {final_state.get('status')}")
        return final_state
    except Exception as e:
        logger.error(f"Resuming workflow failed for thread {thread_id}: {e}")
        raise


# ===============   ==============================================================
# SECTION 9: MAIN (for testing)
# =============================================================================

if __name__ == "__main__":
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
        # Stream to see progress step by step
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
