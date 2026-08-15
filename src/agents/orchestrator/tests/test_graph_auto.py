"""
Full pipeline integration test with auto-approved human gates.

Patches human_gate_1_impl / human_gate_2_impl in graph.py's namespace
so the lambdas in build_orchestrator_graph() resolve to our mocks.
"""

from unittest.mock import patch

from src.agents.orchestrator.state import create_initial_state
from src.agents.orchestrator import graph as graph_module
from src.agents.orchestrator.graph import build_orchestrator_graph


def test_full_pipeline_auto_approve():
    def mock_human_gate_1(state):
        from datetime import datetime, timezone
        import logging

        logger = logging.getLogger(__name__)
        logger.info(f"[{state['job_id']}] MOCK GATE 1: Auto-approved")

        state["status"] = "awaiting_approval_gate_1"
        state["orchestrator_metadata"]["current_node"] = "human_gate_1"
        state["updated_at"] = datetime.now(timezone.utc).isoformat()

        state["human_gates"]["gate_1_pre_infracost"]["approved"] = True
        state["human_gates"]["gate_1_pre_infracost"]["comment"] = "Auto-approved for testing"
        state["human_gates"]["gate_1_pre_infracost"]["approved_at"] = datetime.now(timezone.utc).isoformat()
        state["human_gates"]["gate_1_pre_infracost"]["approved_by"] = "test@devguard.ai"

        return state

    def mock_human_gate_2(state):
        from datetime import datetime, timezone
        import logging

        logger = logging.getLogger(__name__)
        logger.info(f"[{state['job_id']}] MOCK GATE 2: Auto-approved")

        state["status"] = "awaiting_approval_gate_2"
        state["orchestrator_metadata"]["current_node"] = "human_gate_2"
        state["updated_at"] = datetime.now(timezone.utc).isoformat()

        state["human_gates"]["gate_2_pre_deployops"]["approved"] = True
        state["human_gates"]["gate_2_pre_deployops"]["comment"] = "Auto-approved for testing"
        state["human_gates"]["gate_2_pre_deployops"]["approved_at"] = datetime.now(timezone.utc).isoformat()
        state["human_gates"]["gate_2_pre_deployops"]["approved_by"] = "test@devguard.ai"

        return state

    state = create_initial_state("https://github.com/test/repo")
    config = {"configurable": {"thread_id": state["job_id"]}}

    print(f"\nJob ID: {state['job_id']}")
    print(f"Repo: {state['repo_url']}")
    print(f"Status initial: {state['status']}")
    print("-" * 60)

    node_state = state

    # Patch in graph.py's namespace so lambdas in build_orchestrator_graph()
    # resolve to our mocks.
    with patch.object(graph_module, "human_gate_1_impl", mock_human_gate_1), \
         patch.object(graph_module, "human_gate_2_impl", mock_human_gate_2):

        graph = build_orchestrator_graph()
        print("Graph compile avec gates auto-approve")

        for event in graph.stream(state, config):
            for node_name, node_state in event.items():
                print(f"Node '{node_name}' | status: {node_state.get('status', 'N/A')}")

        state = node_state

    print("-" * 60)
    print(f"Status final: {state['status']}")
    print(f"Nodes executes: {state['orchestrator_metadata']['nodes_executed']}")
    print(f"Duree: {state['orchestrator_metadata']['elapsed_seconds']:.2f}s")

    assert state["codesec_result"] is not None, "CodeSec manquant"
    print(f"CodeSec: Score {state['codesec_result']['security_score']['score']}/100")

    assert state["infracost_result"] is not None, "InfraCost manquant"
    print(f"InfraCost: ${state['infracost_result']['cost_estimate']['monthly_cost_usd']}/mois")

    assert state["deployops_result"] is not None, "DeployOps manquant"
    print(f"DeployOps: URL {state['deployops_result']['deployed_url']}")

    assert state["final_report"] is not None, "Report manquant"
    print(f"Report: {state['final_report']['format'].upper()} format")

    print(f"\nPipeline complet reussi en {state['orchestrator_metadata']['elapsed_seconds']:.2f}s!")

    return state


if __name__ == "__main__":
    test_full_pipeline_auto_approve()
