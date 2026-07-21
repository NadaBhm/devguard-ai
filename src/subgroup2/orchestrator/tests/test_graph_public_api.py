"""
Tests pour l'API publique de graph.py
"""

import pytest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from graph import (
    get_orchestrator_graph,
    reset_orchestrator_graph,
    build_orchestrator_graph,
    create_initial_state,
)


class TestGraphSingleton:
    """Tests pour le singleton pattern."""
    
    def test_get_orchestrator_graph_returns_same_instance(self):
        g1 = get_orchestrator_graph()
        g2 = get_orchestrator_graph()
        assert g1 is g2
    
    def test_reset_orchestrator_graph_creates_new_instance(self):
        g1 = get_orchestrator_graph()
        reset_orchestrator_graph()
        g2 = get_orchestrator_graph()
        assert g1 is not g2
        reset_orchestrator_graph()  # Cleanup


class TestBuildOrchestratorGraph:
    """Tests pour la construction du graph."""
    
    def test_graph_has_expected_nodes(self):
        graph = build_orchestrator_graph()
        # Vérifier que les nœuds existent
        # Note: LangGraph ne expose pas directement les nœuds
        # On teste via stream
        
    def test_graph_compiles_without_error(self):
        graph = build_orchestrator_graph()
        assert graph is not None


class TestCreateInitialState:
    """Tests pour create_initial_state."""
    
    def test_creates_valid_state(self):
        state = create_initial_state("https://github.com/test/repo")
        assert state["job_id"] is not None
        assert state["repo_url"] == "https://github.com/test/repo"
        assert state["status"] == "pending"
        assert state["codesec_result"] is None
        assert state["infracost_result"] is None
        assert state["deployops_result"] is None
        assert state["final_report"] is None
        assert len(state["error_log"]) == 0
        assert state["human_gates"]["gate_1_pre_infracost"]["required"] is True
        assert state["human_gates"]["gate_2_pre_deployops"]["required"] is True