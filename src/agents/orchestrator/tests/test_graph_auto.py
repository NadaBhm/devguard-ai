"""
Test rapide du graphe - Auto-approve les gates pour tester le flow complet
Place dans: src/agents/orchestrator/tests/test_graph_auto.py

NOTE (v1.0.6): mis a jour suite au decoupage de graph.py. Les gates humaines
vivent maintenant dans human_gates.py (human_gate_1_impl / human_gate_2_impl,
sans underscore). graph.py les importe et les reference dans les lambdas de
build_orchestrator_graph() - on patche donc ces noms dans le namespace de
graph.py (src.agents.orchestrator.graph.human_gate_1_impl), pas directement
dans human_gates.py, pour que le graphe construit APRES le patch utilise
bien nos versions auto-approve.

Lancer avec: pytest depuis la racine du repo (pas depuis tests/).
"""

from unittest.mock import patch

from src.agents.orchestrator.state import create_initial_state
from src.agents.orchestrator import graph as graph_module
from src.agents.orchestrator.graph import build_orchestrator_graph


def test_full_pipeline_auto_approve():
    """Test le pipeline complet en auto-approvant les gates via mock."""

    # Patch les fonctions de gate pour auto-approve SANS interrupt
    def mock_human_gate_1(state):
        """Version mock du gate 1 - auto-approve sans interrupt."""
        from datetime import datetime, timezone
        import logging

        logger = logging.getLogger(__name__)
        logger.info(f"[{state['job_id']}] MOCK GATE 1: Auto-approved")

        state["status"] = "awaiting_approval_gate_1"
        state["orchestrator_metadata"]["current_node"] = "human_gate_1"
        state["updated_at"] = datetime.now(timezone.utc).isoformat()

        # Auto-approve sans interrupt
        state["human_gates"]["gate_1_pre_infracost"]["approved"] = True
        state["human_gates"]["gate_1_pre_infracost"]["comment"] = "Auto-approved for testing"
        state["human_gates"]["gate_1_pre_infracost"]["approved_at"] = datetime.now(timezone.utc).isoformat()
        state["human_gates"]["gate_1_pre_infracost"]["approved_by"] = "test@devguard.ai"

        return state

    def mock_human_gate_2(state):
        """Version mock du gate 2 - auto-approve sans interrupt."""
        from datetime import datetime, timezone
        import logging

        logger = logging.getLogger(__name__)
        logger.info(f"[{state['job_id']}] MOCK GATE 2: Auto-approved")

        state["status"] = "awaiting_approval_gate_2"
        state["orchestrator_metadata"]["current_node"] = "human_gate_2"
        state["updated_at"] = datetime.now(timezone.utc).isoformat()

        # Auto-approve sans interrupt
        state["human_gates"]["gate_2_pre_deployops"]["approved"] = True
        state["human_gates"]["gate_2_pre_deployops"]["comment"] = "Auto-approved for testing"
        state["human_gates"]["gate_2_pre_deployops"]["approved_at"] = datetime.now(timezone.utc).isoformat()
        state["human_gates"]["gate_2_pre_deployops"]["approved_by"] = "test@devguard.ai"

        return state

    # Creer l'etat initial
    state = create_initial_state("https://github.com/test/repo")
    config = {"configurable": {"thread_id": state["job_id"]}}

    print(f"\nJob ID: {state['job_id']}")
    print(f"Repo: {state['repo_url']}")
    print(f"Status initial: {state['status']}")
    print("-" * 60)

    node_state = state

    # Build le graph avec les gates mockees.
    # On patche dans le namespace de graph.py (pas human_gates.py), car
    # c'est de la que les lambdas de build_orchestrator_graph() resolvent
    # ces noms au moment de l'appel.
    with patch.object(graph_module, "human_gate_1_impl", mock_human_gate_1), \
         patch.object(graph_module, "human_gate_2_impl", mock_human_gate_2):

        graph = build_orchestrator_graph()
        print("Graph compile avec gates auto-approve")

        # Stream le graph complet
        for event in graph.stream(state, config):
            for node_name, node_state in event.items():
                print(f"Node '{node_name}' | status: {node_state.get('status', 'N/A')}")

        # Mettre a jour le state final
        state = node_state

    print("-" * 60)
    print(f"Status final: {state['status']}")
    print(f"Nodes executes: {state['orchestrator_metadata']['nodes_executed']}")
    print(f"Duree: {state['orchestrator_metadata']['elapsed_seconds']:.2f}s")

    # Verifications finales
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
