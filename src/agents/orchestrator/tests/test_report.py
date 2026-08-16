"""
Tests for report.py (T-3.12 / US-2.2.6)

US-2.2.6 requires "PDF/HTML with all agent results, architecture diagram,
and cost breakdown", in under 10 seconds (NFR).

PDF assertions are conditional: WeasyPrint binds to system libraries
(cairo, pango) that are absent on several of the team's machines, so the
suite must pass with or without a working PDF backend.
"""

import pytest

from src.agents.orchestrator.nodes import (
    build_mock_codesec_result,
    build_mock_deployops_result,
    build_mock_infracost_result,
    generate_report_impl,
)
from src.agents.orchestrator.report import (
    build_architecture_svg,
    build_template_context,
    generate_report,
    render_html,
)
from src.agents.orchestrator.state import create_initial_state


@pytest.fixture
def completed_state():
    state = create_initial_state("https://github.com/test/repo")
    state["status"] = "completed"
    state["codesec_result"] = build_mock_codesec_result(state["job_id"], state["repo_url"])
    state["infracost_result"] = build_mock_infracost_result()
    state["deployops_result"] = build_mock_deployops_result(state["job_id"])
    state["human_gates"]["gate_1_pre_infracost"].update(
        {"approved": True, "approved_by": "nada@devguard.ai", "comment": "ok"}
    )
    state["human_gates"]["gate_2_pre_deployops"].update(
        {"approved": True, "approved_by": "hbib@devguard.ai", "comment": "deploy"}
    )
    return state


class TestArchitectureDiagram:
    def test_produces_svg_for_known_architecture(self):
        svg = build_architecture_svg("ecs_fargate")
        assert svg.startswith("<svg")
        assert "ECS Fargate" in svg
        assert "ALB" in svg

    def test_each_architecture_has_a_layout(self):
        for arch in ("ecs_fargate", "lambda", "ec2", "hybrid"):
            assert build_architecture_svg(arch).startswith("<svg")

    def test_unknown_architecture_yields_nothing(self):
        """An empty diagram is worse than no diagram."""
        assert build_architecture_svg(None) == ""
        assert build_architecture_svg("quantum_compute") == ""

    def test_region_is_labelled(self):
        assert "us-east-1" in build_architecture_svg("ecs_fargate", "us-east-1")

    def test_labels_are_escaped(self):
        """Region strings reach the SVG; they must not be able to inject markup."""
        svg = build_architecture_svg("ecs_fargate", "<script>alert(1)</script>")
        assert "<script>" not in svg
        assert "&lt;script&gt;" in svg


class TestTemplateContext:
    def test_aggregates_all_finding_types(self, completed_state):
        ctx = build_template_context(completed_state)
        assert ctx["total_findings"] == 18

    def test_carries_security_score(self, completed_state):
        ctx = build_template_context(completed_state)
        assert ctx["security_score"] == 68
        assert ctx["security_grade"] == "C"

    def test_carries_cost(self, completed_state):
        ctx = build_template_context(completed_state)
        assert ctx["monthly_cost"] == 145.32
        assert len(ctx["cost_breakdown"]) == 3

    def test_merges_security_and_finops_recommendations(self, completed_state):
        ctx = build_template_context(completed_state)
        joined = " ".join(ctx["recommendations"])
        assert "SQL injection" in joined
        assert "Graviton" in joined

    def test_records_approval_trail(self, completed_state):
        ctx = build_template_context(completed_state)
        decisions = {g["name"]: g["decision"] for g in ctx["approvals"]}
        assert decisions["gate_1_pre_infracost"] == "approved"
        assert decisions["gate_2_pre_deployops"] == "approved"

    def test_marks_gates_never_reached(self):
        state = create_initial_state("https://github.com/test/repo")
        ctx = build_template_context(state)
        assert all(g["decision"] == "not reached" for g in ctx["approvals"])

    def test_handles_an_empty_state(self):
        """A job that failed at CodeSec must still produce a report."""
        ctx = build_template_context(create_initial_state("https://github.com/x/y"))
        assert ctx["has_codesec"] is False
        assert ctx["has_infracost"] is False
        assert ctx["total_findings"] == 0


class TestHtmlRendering:
    def test_renders_a_full_document(self, completed_state):
        out = render_html(completed_state)
        assert out.startswith("<!DOCTYPE html>")
        assert "</html>" in out

    def test_contains_every_required_section(self, completed_state):
        out = render_html(completed_state)
        assert "Security analysis" in out
        assert "Cost estimate" in out
        assert "Deployment" in out
        assert "<svg" in out
        assert "ECS Fargate" in out
        assert "145.32" in out

    def test_never_prints_secret_values(self, completed_state):
        """
        This report gets emailed and archived. Printing the credentials it
        just found would make the security report a second leak.
        """
        out = render_html(completed_state)
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        # ...while staying actionable:
        assert "aws_access_key_id" in out
        assert ".env" in out

    def test_escapes_scanned_content(self):
        """Findings come from arbitrary repos; they must not inject markup."""
        state = create_initial_state("https://github.com/test/repo")
        state["codesec_result"] = build_mock_codesec_result(state["job_id"], state["repo_url"])
        state["codesec_result"]["sast_findings"][0]["message"] = "<script>alert('xss')</script>"
        out = render_html(state)
        assert "<script>alert" not in out
        assert "&lt;script&gt;" in out

    def test_renders_a_failed_deployment(self, completed_state):
        completed_state["deployops_result"].update({
            "deployment_status": "rolled_back",
            "rollback_triggered": True,
            "rollback_reason": "health check failed",
        })
        out = render_html(completed_state)
        assert "rolled_back" in out
        assert "health check failed" in out

    def test_reports_recovered_incidents(self, completed_state):
        """"We hit a 503 and recovered" is information, not noise to hide."""
        completed_state["error_log"].append({
            "node": "deployops_agent", "attempt": 1, "max_attempts": 3,
            "message": "AWS API 503", "timestamp": "2026-08-06T10:00:00Z",
            "stack_trace": None, "resolved": True,
        })
        out = render_html(completed_state)
        assert "AWS API 503" in out
        assert "Incidents" in out


class TestGenerateReport:
    def test_writes_an_html_file(self, completed_state, tmp_path):
        result = generate_report(completed_state, output_dir=tmp_path)
        assert "html" in result["formats_available"]
        assert (tmp_path / f"report-{completed_state['job_id']}.html").exists()

    def test_download_url_points_at_the_job(self, completed_state, tmp_path):
        result = generate_report(completed_state, output_dir=tmp_path)
        assert result["download_url"] == f"/api/jobs/{completed_state['job_id']}/report/download"

    def test_summary_matches_the_findings(self, completed_state, tmp_path):
        result = generate_report(completed_state, output_dir=tmp_path)
        summary = result["summary"]
        assert summary["total_vulnerabilities"] == 18
        assert summary["critical_count"] == 1
        assert summary["estimated_monthly_cost_usd"] == 145.32
        assert summary["deployment_status"] == "success"

    def test_respects_the_ten_second_budget(self, completed_state, tmp_path):
        """NFR: report generation < 10 seconds."""
        result = generate_report(completed_state, output_dir=tmp_path)
        assert result["render_seconds"] < 10

    def test_html_still_produced_without_pdf(self, completed_state, tmp_path):
        """PDF is a "Should"; its absence must not cost us the HTML."""
        result = generate_report(completed_state, output_dir=tmp_path, want_pdf=False)
        assert result["formats_available"] == ["html"]
        assert result["format"] == "html"
        assert result["pdf_path"] is None

    def test_pdf_when_the_backend_supports_it(self, completed_state, tmp_path):
        # WeasyPrint may raise ImportError or OSError if native libs (cairo,
        # pango) are missing on the host. Treat either as a skip so CI can
        # run on machines without the full PDF toolchain.
        try:
            pytest.importorskip("weasyprint")
        except OSError as exc:  # missing native shared libs
            pytest.skip(f"weasyprint not available: {exc}")
        result = generate_report(completed_state, output_dir=tmp_path)
        if "pdf" in result["formats_available"]:
            assert (tmp_path / f"report-{completed_state['job_id']}.pdf").exists()
            assert result["format"] == "pdf"


class TestReportNode:
    def test_node_populates_final_report(self, completed_state, monkeypatch, tmp_path):
        monkeypatch.setenv("DEVGUARD_REPORT_DIR", str(tmp_path))
        import src.agents.orchestrator.report as report_module
        monkeypatch.setattr(report_module, "DEFAULT_REPORT_DIR", tmp_path)

        result = generate_report_impl(completed_state)
        assert result["final_report"] is not None
        assert "summary" in result["final_report"]
        assert "generate_report" in result["orchestrator_metadata"]["nodes_executed"]

    def test_rendering_failure_is_not_fatal(self, completed_state, monkeypatch):
        """
        The pipeline has already deployed by this point. Losing the results
        because a template failed to render would be absurd.
        """
        import src.agents.orchestrator.report as report_module

        def boom(*args, **kwargs):
            raise RuntimeError("template exploded")

        monkeypatch.setattr(report_module, "generate_report", boom)

        result = generate_report_impl(completed_state)
        assert result["status"] != "failed"
        assert result["final_report"]["format"] == "json"
        assert "template exploded" in result["final_report"]["error"]
        assert result["final_report"]["summary"]["critical_count"] == 1
