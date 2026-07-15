"""
Test rapide de graph.py - Auto-approve les gates pour tester le flow complet
Place dans: src/subgroup2/orchestrator/tests/test_graph_auto.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from unittest.mock import patch
from graph import build_orchestrator_graph, create_initial_state


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
    
    # Créer l'état initial
    state = create_initial_state("https://github.com/test/repo")
    config = {"configurable": {"thread_id": state["job_id"]}}
    
    print(f"\n🚀 Job ID: {state['job_id']}")
    print(f"📦 Repo: {state['repo_url']}")
    print(f"⏳ Status initial: {state['status']}")
    print("-" * 60)
    
    # Build le graph avec les gates mockés
    with patch('graph._human_gate_1_impl', mock_human_gate_1), \
         patch('graph._human_gate_2_impl', mock_human_gate_2):
        
        graph = build_orchestrator_graph()
        print("✅ Graph compilé avec gates auto-approve")
        
        # Stream le graph complet
        for event in graph.stream(state, config):
            for node_name, node_state in event.items():
                print(f"✅ Node '{node_name}' | status: {node_state.get('status', 'N/A')}")
        
        # Mettre à jour le state final
        state = node_state
    
    print("-" * 60)
    print(f"🏁 Status final: {state['status']}")
    print(f"📊 Nodes exécutés: {state['orchestrator_metadata']['nodes_executed']}")
    print(f"⏱️  Durée: {state['orchestrator_metadata']['elapsed_seconds']:.2f}s")
    
    # Vérifications finales
    assert state["codesec_result"] is not None, "❌ CodeSec manquant"
    print(f"🔒 CodeSec: Score {state['codesec_result']['security_score']['score']}/100")
    
    assert state["infracost_result"] is not None, "❌ InfraCost manquant"
    print(f"💰 InfraCost: ${state['infracost_result']['cost_estimate']['monthly_cost_usd']}/mois")
    
    assert state["deployops_result"] is not None, "❌ DeployOps manquant"
    print(f"🚀 DeployOps: URL {state['deployops_result']['deployed_url']}")
    
    assert state["final_report"] is not None, "❌ Report manquant"
    print(f"📄 Report: {state['final_report']['format'].upper()} format")
    
    print(f"\n🎉 Pipeline complet réussi en {state['orchestrator_metadata']['elapsed_seconds']:.2f}s!")
    
    return state


if __name__ == "__main__":
    test_full_pipeline_auto_approve()