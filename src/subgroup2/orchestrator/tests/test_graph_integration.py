"""
Test d'intégration du pipeline complet — version pytest
"""

import pytest
from unittest.mock import patch
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from graph import build_orchestrator_graph, create_initial_state


@pytest.fixture
def auto_approve_graph():
    """Graph avec gates auto-approvés pour test intégration."""
    
    def mock_gate_1(state):
        from datetime import datetime, timezone
        state["status"] = "awaiting_approval_gate_1"
        state["human_gates"]["gate_1_pre_infracost"]["approved"] = True
        state["human_gates"]["gate_1_pre_infracost"]["approved_at"] = datetime.now(timezone.utc).isoformat()
        return state
    
    def mock_gate_2(state):
        from datetime import datetime, timezone
        state["status"] = "awaiting_approval_gate_2"
        state["human_gates"]["gate_2_pre_deployops"]["approved"] = True
        state["human_gates"]["gate_2_pre_deployops"]["approved_at"] = datetime.now(timezone.utc).isoformat()
        return state
    
    with patch('graph._human_gate_1_impl', mock_gate_1), \
         patch('graph._human_gate_2_impl', mock_gate_2):
        yield build_orchestrator_graph()


class TestFullPipeline:
    """Test du pipeline complet."""
    
    def test_pipeline_completes_successfully(self, auto_approve_graph):
        state = create_initial_state("https://github.com/test/repo")
        config = {"configurable": {"thread_id": state["job_id"]}}
        
        final_state = auto_approve_graph.invoke(state, config)
        
        assert final_state["status"] == "completed"
        assert final_state["codesec_result"] is not None
        assert final_state["infracost_result"] is not None
        assert final_state["deployops_result"] is not None
        assert final_state["final_report"] is not None
    
    def test_pipeline_generates_report(self, auto_approve_graph):
        state = create_initial_state("https://github.com/test/repo")
        config = {"configurable": {"thread_id": state["job_id"]}}
        
        final_state = auto_approve_graph.invoke(state, config)
        
        report = final_state["final_report"]
        assert report["format"] == "html"
        assert "summary" in report
        assert report["summary"]["total_vulnerabilities"] >= 0
        assert report["summary"]["estimated_monthly_cost_usd"] > 0
    
    def test_pipeline_tracks_nodes_executed(self, auto_approve_graph):
        state = create_initial_state("https://github.com/test/repo")
        config = {"configurable": {"thread_id": state["job_id"]}}
        
        final_state = auto_approve_graph.invoke(state, config)
        
        nodes = final_state["orchestrator_metadata"]["nodes_executed"]
        assert "codesec_agent" in nodes
        assert "infracost_agent" in nodes
        assert "deployops_agent" in nodes
        assert "health_check" in nodes
        assert "generate_report" in nodes
    
    def test_pipeline_duration_is_tracked(self, auto_approve_graph):
        state = create_initial_state("https://github.com/test/repo")
        config = {"configurable": {"thread_id": state["job_id"]}}
        
        final_state = auto_approve_graph.invoke(state, config)
        
        elapsed = final_state["orchestrator_metadata"]["elapsed_seconds"]
        assert elapsed >= 0
        assert elapsed < 30  # Doit être < 30s (Definition of Done Sprint 1)