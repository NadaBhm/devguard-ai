"""
Tests unitaires pour graph.py - Sprint 1
Place dans: src/subgroup2/orchestrator/tests/test_graph.py
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from graph import (
    create_initial_state,
    _mock_codesec_agent_impl,
    _mock_infracost_agent_impl,
    _mock_deployops_agent_impl,
    _health_check_impl,
    _generate_report_impl,
    route_after_codesec,
    route_after_gate_1,
    route_after_infracost,
    route_after_gate_2,
    route_after_deployops,
    route_after_health_check,
    _safe_node_wrapper,
)


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def initial_state():
    """État initial pour les tests."""
    return create_initial_state("https://github.com/test/repo")


# =============================================================================
# TESTS: STATE INITIAL
# =============================================================================

class TestInitialState:
    """Tests pour create_initial_state."""
    
    def test_job_id_generated(self, initial_state):
        assert len(initial_state["job_id"]) == 36  # UUID v4
    
    def test_status_pending(self, initial_state):
        assert initial_state["status"] == "pending"
    
    def test_repo_url_set(self, initial_state):
        assert initial_state["repo_url"] == "https://github.com/test/repo"
    
    def test_codesec_result_none(self, initial_state):
        assert initial_state["codesec_result"] is None
    
    def test_human_gates_initialized(self, initial_state):
        gate1 = initial_state["human_gates"]["gate_1_pre_infracost"]
        assert gate1["required"] is True
        assert gate1["approved"] is None


# =============================================================================
# TESTS: MOCK AGENTS (Nodes individuels)
# =============================================================================

class TestMockCodeSecAgent:
    """Tests pour _mock_codesec_agent_impl."""
    
    def test_returns_codesec_result(self, initial_state):
        result = _mock_codesec_agent_impl(initial_state)
        assert result["codesec_result"] is not None
        assert result["codesec_result"]["job_id"] == initial_state["job_id"]
    
    def test_status_changed_to_analyzing(self, initial_state):
        result = _mock_codesec_agent_impl(initial_state)
        assert result["status"] == "analyzing"
    
    def test_security_score_present(self, initial_state):
        result = _mock_codesec_agent_impl(initial_state)
        assert "security_score" in result["codesec_result"]
        assert result["codesec_result"]["security_score"]["score"] == 68
    
    def test_stack_detection_present(self, initial_state):
        result = _mock_codesec_agent_impl(initial_state)
        assert "stack_detection" in result["codesec_result"]
        assert result["codesec_result"]["stack_detection"]["primary_language"] == "python"
    
    def test_sast_findings_is_list(self, initial_state):
        result = _mock_codesec_agent_impl(initial_state)
        assert isinstance(result["codesec_result"]["sast_findings"], list)
        assert len(result["codesec_result"]["sast_findings"]) > 0
    
    def test_secrets_is_list(self, initial_state):
        result = _mock_codesec_agent_impl(initial_state)
        assert isinstance(result["codesec_result"]["secrets"], list)
    
    def test_phases_is_list(self, initial_state):
        result = _mock_codesec_agent_impl(initial_state)
        assert isinstance(result["codesec_result"]["phases"], list)
        assert len(result["codesec_result"]["phases"]) == 8  # 8 phases
    
    def test_metadata_updated(self, initial_state):
        result = _mock_codesec_agent_impl(initial_state)
        assert "codesec_agent" in result["orchestrator_metadata"]["nodes_executed"]


class TestMockInfraCostAgent:
    """Tests pour _mock_infracost_agent_impl."""
    
    def test_returns_infracost_result(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        result = _mock_infracost_agent_impl(initial_state)
        assert result["infracost_result"] is not None
    
    def test_status_changed_to_infra_generating(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        result = _mock_infracost_agent_impl(initial_state)
        assert result["status"] == "infra_generating"
    
    def test_architecture_recommendation(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        result = _mock_infracost_agent_impl(initial_state)
        assert result["infracost_result"]["architecture_recommendation"] == "ecs_fargate"
    
    def test_cost_estimate_present(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        result = _mock_infracost_agent_impl(initial_state)
        assert "monthly_cost_usd" in result["infracost_result"]["cost_estimate"]
        assert result["infracost_result"]["cost_estimate"]["monthly_cost_usd"] == 145.32
    
    def test_load_scenarios_present(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        result = _mock_infracost_agent_impl(initial_state)
        assert len(result["infracost_result"]["load_scenarios"]) == 3
    
    def test_optimizations_present(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        result = _mock_infracost_agent_impl(initial_state)
        assert len(result["infracost_result"]["optimizations"]) > 0
    
    def test_region_comparison_present(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        result = _mock_infracost_agent_impl(initial_state)
        assert len(result["infracost_result"]["region_comparison"]) == 3


class TestMockDeployOpsAgent:
    """Tests pour _mock_deployops_agent_impl - ALIGNED avec deployops-mock-schema.json."""
    
    def test_returns_deployops_result(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state = _mock_infracost_agent_impl(initial_state)
        result = _mock_deployops_agent_impl(initial_state)
        assert result["deployops_result"] is not None
    
    def test_job_id_matches(self, initial_state):
        """REQUIS par deployops-mock-schema.json: job_id doit matcher."""
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state = _mock_infracost_agent_impl(initial_state)
        result = _mock_deployops_agent_impl(initial_state)
        assert result["deployops_result"]["job_id"] == initial_state["job_id"]
    
    def test_deployment_status_enum(self, initial_state):
        """REQUIS: deployment_status doit être success/failed/rolled_back."""
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state = _mock_infracost_agent_impl(initial_state)
        result = _mock_deployops_agent_impl(initial_state)
        assert result["deployops_result"]["deployment_status"] in ["success", "failed", "rolled_back"]
    
    def test_deployed_url_present(self, initial_state):
        """REQUIS: deployed_url doit exister."""
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state = _mock_infracost_agent_impl(initial_state)
        result = _mock_deployops_agent_impl(initial_state)
        assert result["deployops_result"]["deployed_url"] is not None
        assert result["deployops_result"]["deployed_url"].startswith("https://")
    
    def test_health_check_required_fields(self, initial_state):
        """REQUIS: health_check avec passed, response_time_ms, status_code."""
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state = _mock_infracost_agent_impl(initial_state)
        result = _mock_deployops_agent_impl(initial_state)
        
        hc = result["deployops_result"]["health_check"]
        assert "passed" in hc
        assert "response_time_ms" in hc
        assert "status_code" in hc
        assert isinstance(hc["response_time_ms"], int)
        assert isinstance(hc["status_code"], int)
    
    def test_rollback_triggered_present(self, initial_state):
        """REQUIS: rollback_triggered booléen."""
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state = _mock_infracost_agent_impl(initial_state)
        result = _mock_deployops_agent_impl(initial_state)
        assert isinstance(result["deployops_result"]["rollback_triggered"], bool)
    
    def test_terraform_outputs_required(self, initial_state):
        """REQUIS: terraform_outputs avec ecs_cluster_name, service_name, alb_dns."""
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state = _mock_infracost_agent_impl(initial_state)
        result = _mock_deployops_agent_impl(initial_state)
        
        outputs = result["deployops_result"]["terraform_outputs"]
        assert "ecs_cluster_name" in outputs
        assert "service_name" in outputs
        assert "alb_dns" in outputs
    
    def test_artifacts_present(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state = _mock_infracost_agent_impl(initial_state)
        result = _mock_deployops_agent_impl(initial_state)
        assert "terraform" in result["deployops_result"]["artifacts"]
        assert "dockerfile" in result["deployops_result"]["artifacts"]
    
    def test_aws_config_present(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state = _mock_infracost_agent_impl(initial_state)
        result = _mock_deployops_agent_impl(initial_state)
        assert result["deployops_result"]["aws_config"]["region"] == "us-east-1"
    
    def test_deployment_config_present(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state = _mock_infracost_agent_impl(initial_state)
        result = _mock_deployops_agent_impl(initial_state)
        assert result["deployops_result"]["deployment_config"]["strategy"] in ["rolling", "blue-green"]
    
    def test_approval_present(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state = _mock_infracost_agent_impl(initial_state)
        result = _mock_deployops_agent_impl(initial_state)
        assert result["deployops_result"]["approval"]["deploy_approved"] is True
        assert "@" in result["deployops_result"]["approval"]["approved_by"]


class TestHealthCheck:
    """Tests pour _health_check_impl."""
    
    def test_passed_health_check(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state = _mock_infracost_agent_impl(initial_state)
        initial_state = _mock_deployops_agent_impl(initial_state)
        result = _health_check_impl(initial_state)
        assert result["status"] == "completed"
    
    def test_failed_health_check_triggers_rollback(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state = _mock_infracost_agent_impl(initial_state)
        initial_state = _mock_deployops_agent_impl(initial_state)
        # Simuler un health check failed
        initial_state["deployops_result"]["health_check"]["passed"] = False
        result = _health_check_impl(initial_state)
        assert result["status"] == "rolled_back"
        assert result["deployops_result"]["rollback_triggered"] is True
        assert result["deployops_result"]["deployment_status"] == "rolled_back"


class TestGenerateReport:
    """Tests pour _generate_report_impl."""
    
    def test_report_generated(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state = _mock_infracost_agent_impl(initial_state)
        initial_state = _mock_deployops_agent_impl(initial_state)
        initial_state = _health_check_impl(initial_state)
        result = _generate_report_impl(initial_state)
        assert result["final_report"] is not None
    
    def test_report_has_summary(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state = _mock_infracost_agent_impl(initial_state)
        initial_state = _mock_deployops_agent_impl(initial_state)
        initial_state = _health_check_impl(initial_state)
        result = _generate_report_impl(initial_state)
        assert "summary" in result["final_report"]
    
    def test_report_summary_fields(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state = _mock_infracost_agent_impl(initial_state)
        initial_state = _mock_deployops_agent_impl(initial_state)
        initial_state = _health_check_impl(initial_state)
        result = _generate_report_impl(initial_state)
        summary = result["final_report"]["summary"]
        assert "total_vulnerabilities" in summary
        assert "critical_count" in summary
        assert "estimated_monthly_cost_usd" in summary
        assert "deployment_status" in summary
        assert "recommendations" in summary
        assert "pipeline_duration_seconds" in summary
    
    def test_report_format_html(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state = _mock_infracost_agent_impl(initial_state)
        initial_state = _mock_deployops_agent_impl(initial_state)
        initial_state = _health_check_impl(initial_state)
        result = _generate_report_impl(initial_state)
        assert result["final_report"]["format"] == "html"


# =============================================================================
# TESTS: ROUTING FUNCTIONS
# =============================================================================

class TestRouting:
    """Tests pour les fonctions de routing conditionnel."""
    
    def test_route_after_codesec_success(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        assert route_after_codesec(initial_state) == "human_gate_1"
    
    def test_route_after_codesec_failed(self, initial_state):
        initial_state["status"] = "failed"
        assert route_after_codesec(initial_state) == "end"
    
    def test_route_after_codesec_no_result(self, initial_state):
        initial_state["codesec_result"] = None
        assert route_after_codesec(initial_state) == "end"
    
    def test_route_after_gate_1_approved(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state["human_gates"]["gate_1_pre_infracost"]["approved"] = True
        assert route_after_gate_1(initial_state) == "infracost_agent"
    
    def test_route_after_gate_1_rejected(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state["human_gates"]["gate_1_pre_infracost"]["approved"] = False
        assert route_after_gate_1(initial_state) == "end"
    
    def test_route_after_infracost_success(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state = _mock_infracost_agent_impl(initial_state)
        assert route_after_infracost(initial_state) == "human_gate_2"
    
    def test_route_after_gate_2_approved(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state = _mock_infracost_agent_impl(initial_state)
        initial_state["human_gates"]["gate_2_pre_deployops"]["approved"] = True
        assert route_after_gate_2(initial_state) == "deployops_agent"
    
    def test_route_after_deployops_success(self, initial_state):
        initial_state = _mock_codesec_agent_impl(initial_state)
        initial_state = _mock_infracost_agent_impl(initial_state)
        initial_state = _mock_deployops_agent_impl(initial_state)
        assert route_after_deployops(initial_state) == "health_check"
    
    def test_route_after_health_check_completed(self, initial_state):
        initial_state["status"] = "completed"
        assert route_after_health_check(initial_state) == "generate_report"
    
    def test_route_after_health_check_failed(self, initial_state):
        initial_state["status"] = "rolled_back"
        assert route_after_health_check(initial_state) == "end"


# =============================================================================
# TESTS: SAFE NODE WRAPPER
# =============================================================================

class TestSafeNodeWrapper:
    """Tests pour _safe_node_wrapper."""
    
    def test_catches_exception(self, initial_state):
        def failing_node(state):
            raise ValueError("Test error")
        
        result = _safe_node_wrapper(failing_node, "test_node", initial_state)
        assert result["status"] == "failed"
        assert len(result["error_log"]) == 1
        assert result["error_log"][0]["node"] == "test_node"
        assert result["error_log"][0]["message"] == "Test error"
        assert result["error_log"][0]["resolved"] is False
    
    def test_skips_if_already_failed(self, initial_state):
        initial_state["status"] = "failed"
        
        def should_not_run(state):
            raise ValueError("Should not be called")
        
        result = _safe_node_wrapper(should_not_run, "skipped", initial_state)
        assert result["status"] == "failed"
        assert len(result["error_log"]) == 0