"""
DevGuard AI - Orchestrator Nodes
Agent node implementations and conditional routing functions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import cast

from .state import (
    MAX_INFRACOST_ITERATIONS,
    DeployOpsResult,
    FinalReport,
    InfraCostResult,
    OrchestratorState,
)

logger = logging.getLogger(__name__)


def mock_codesec_agent_impl(state: OrchestratorState) -> OrchestratorState:
    logger.info(f"[{state['job_id']}] Running MOCK CodeSec Agent for: {state['repo_url']}")

    state["status"] = "analyzing"
    state["orchestrator_metadata"]["current_node"] = "codesec_agent"
    state["orchestrator_metadata"]["nodes_executed"].append("codesec_agent")
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    state["codesec_result"] = build_mock_codesec_result(state["job_id"], state["repo_url"])

    score = state["codesec_result"]["security_score"]["score"]
    grade = state["codesec_result"]["security_score"]["grade"]
    logger.info(f"[{state['job_id']}] CodeSec complete. Score: {score}/100 (Grade {grade})")
    return state


def build_mock_codesec_result(job_id: str, repo_url: str) -> dict:
    return {
        "job_id": job_id,
        "status": "completed",
        "error": None,
        "repo_url": repo_url,
        "repo_metadata": {
            "name": "repo-name",
            "branch": "main",
            "commit_sha": "a1b2c3d4e5f6",
            "total_files": 150,
            "loc": 8500,
            "language_breakdown": {
                "python": 6200,
                "javascript": 1500,
                "dockerfile": 200,
                "terraform": 600,
            },
        },
        "phases": [
            {"name": "clone", "status": "completed", "started_at": "2026-07-08T10:00:00Z", "completed_at": "2026-07-08T10:00:05Z"},
            {"name": "stack_detection", "status": "completed", "started_at": "2026-07-08T10:00:06Z", "completed_at": "2026-07-08T10:00:10Z"},
            {"name": "sast", "status": "completed", "started_at": "2026-07-08T10:00:11Z", "completed_at": "2026-07-08T10:00:20Z"},
            {"name": "secrets", "status": "completed", "started_at": "2026-07-08T10:00:21Z", "completed_at": "2026-07-08T10:00:25Z"},
            {"name": "dependencies", "status": "completed", "started_at": "2026-07-08T10:00:26Z", "completed_at": "2026-07-08T10:00:30Z"},
            {"name": "dockerfile_scan", "status": "completed", "started_at": "2026-07-08T10:00:31Z", "completed_at": "2026-07-08T10:00:35Z"},
            {"name": "sbom", "status": "completed", "started_at": "2026-07-08T10:00:36Z", "completed_at": "2026-07-08T10:00:40Z"},
            {"name": "scoring", "status": "completed", "started_at": "2026-07-08T10:00:41Z", "completed_at": "2026-07-08T10:00:45Z"},
        ],
        "summary": {
            "files_scanned": 150,
            "sast_findings_count": 12,
            "secrets_found_count": 2,
            "vulnerable_dependencies_count": 1,
            "dockerfile_issues_count": 3,
            "total_critical": 1,
            "total_high": 4,
            "total_medium": 5,
            "total_low": 2,
            "total_info": 0,
        },
        "stack_detection": {
            "primary_language": "python",
            "frameworks": ["fastapi", "sqlalchemy"],
            "database": "postgresql",
            "build_tool": "pip",
            "container": {
                "detected": True,
                "base_image": "python:3.12-slim",
                "dockerfile_path": "Dockerfile",
                "compose_detected": True,
            },
            "confidence": 0.92,
            "detected_files": ["requirements.txt", "Dockerfile", "main.py", "docker-compose.yml"],
        },
        "sast_findings": [
            {
                "rule_id": "python.sql-injection",
                "tool": "semgrep",
                "severity": "critical",
                "category": "owasp-top10",
                "owasp_category": "A03:2021 - Injection",
                "cwe_id": "CWE-89",
                "file": "app/db.py",
                "line": 24,
                "column": 10,
                "message": "Possible SQL injection via string concatenation",
                "snippet": 'query = f"SELECT * FROM users WHERE id = {user_id}"',
                "remediation": "Use parameterized queries with SQLAlchemy ORM",
            },
            {
                "rule_id": "python.xss.generic",
                "tool": "semgrep",
                "severity": "medium",
                "category": "owasp-top10",
                "owasp_category": "A03:2021 - Injection",
                "cwe_id": "CWE-79",
                "file": "src/frontend/components/ChatPanel.tsx",
                "line": 15,
                "column": 5,
                "message": "Unsanitized user input rendered in DOM",
                "snippet": "<div dangerouslySetInnerHTML={{__html: userInput}} />",
                "remediation": "Use DOMPurify or React's built-in escaping",
            },
        ],
        "secrets": [
            {
                "type": "aws_access_key_id",
                "tool": "gitleaks",
                "file": ".env",
                "line": 3,
                "column": 1,
                "value_preview": "AKIAIOSFODNN7EXAMPLE",
                "commit_sha": "a1b2c3d4e5f6",
                "confidence": 0.95,
                "remediation": "Rotate the key and use AWS Secrets Manager",
            },
            {
                "type": "api_key",
                "tool": "gitleaks",
                "file": "scripts/deploy.sh",
                "line": 8,
                "column": 15,
                "value_preview": "sk-****abcd1234",
                "commit_sha": "a1b2c3d4e5f6",
                "confidence": 0.88,
                "remediation": "Use environment variables or a secret manager",
            },
        ],
        "dependencies": {
            "total_packages": 42,
            "direct": 12,
            "transitive": 30,
            "vulnerable_packages": [
                {
                    "package": "requests",
                    "installed_version": "2.25.0",
                    "fixed_version": "2.31.0",
                    "cve_id": "CVE-2023-32681",
                    "severity": "high",
                    "cvss_score": 7.5,
                    "description": "Unintended leak of Proxy-Authorization header",
                }
            ],
        },
        "dockerfile_findings": [
            {
                "rule_id": "DS001",
                "tool": "trivy",
                "severity": "high",
                "category": "dockerfile",
                "file": "Dockerfile",
                "line": 5,
                "message": "Running as root user",
                "snippet": "USER root",
                "remediation": "Add 'USER appuser' after creating non-root user",
            },
            {
                "rule_id": "DS002",
                "tool": "trivy",
                "severity": "medium",
                "category": "dockerfile",
                "file": "Dockerfile",
                "line": 1,
                "message": "Using 'latest' tag for base image",
                "snippet": "FROM python:latest",
                "remediation": "Pin to specific version: FROM python:3.12-slim",
            },
        ],
        "sbom": {
            "format": "CycloneDX",
            "specVersion": "1.5",
            "serialNumber": "urn:uuid:550e8400-e29b-41d4-a716-446655440001",
            "version": 1,
            "components_count": 42,
            "components": [
                {
                    "type": "library",
                    "name": "fastapi",
                    "version": "0.110.0",
                    "purl": "pkg:pypi/fastapi@0.110.0",
                    "licenses": [{"id": "MIT"}],
                    "source_file": "requirements.txt",
                }
            ],
            "download_url": f"/api/jobs/{job_id}/sbom/download",
        },
        "security_score": {
            "score": 68,
            "grade": "C",
            "max_score": 100,
            "breakdown": {
                "sast": 20,
                "secrets": 15,
                "dependencies": 20,
                "dockerfile": 8,
                "sbom": 10,
                "stack_detection": 7,
            },
            "severity_counts": {
                "critical": 1,
                "high": 4,
                "medium": 5,
                "low": 2,
                "info": 0,
            },
            "recommendations": [
                "Fix 1 critical SQL injection in app/db.py:24",
                "Update requests to >= 2.31.0 (CVE-2023-32681)",
                "Remove 2 hardcoded secrets from .env and scripts/deploy.sh",
                "Run Dockerfile as non-root user (Dockerfile:5)",
            ],
        },
    }


def mock_infracost_agent_impl(state: OrchestratorState) -> OrchestratorState:
    logger.info(f"[{state['job_id']}] Running MOCK InfraCost Agent")

    state["status"] = "infra_generating"
    state["orchestrator_metadata"]["current_node"] = "infracost_agent"
    state["orchestrator_metadata"]["nodes_executed"].append("infracost_agent")
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    state["infracost_result"] = cast(InfraCostResult, build_mock_infracost_result())

    logger.info(f"[{state['job_id']}] InfraCost complete. Est. cost: ${state['infracost_result']['cost_estimate']['monthly_cost_usd']}/mo")
    return state


def build_mock_infracost_result() -> dict:
    return {
        "architecture_recommendation": "ecs_fargate",
        "justification": "FastAPI app with moderate traffic -> ECS Fargate for serverless containers without EC2 management",
        "generated_terraform": {
            "main_tf": 'resource "aws_ecs_cluster" "app" {\n  name = "devguard-app"\n}',
            "variables_tf": 'variable "aws_region" {\n  default = "us-east-1"\n}',
            "outputs_tf": 'output "alb_dns" {\n  value = aws_lb.app.dns_name\n}',
            "plan_passed": True,
        },
        "cost_estimate": {
            "monthly_cost_usd": 145.32,
            "currency": "USD",
            "breakdown": [
                {"service": "ECS Fargate", "monthly_cost_usd": 89.50},
                {"service": "ALB", "monthly_cost_usd": 22.80},
                {"service": "RDS PostgreSQL", "monthly_cost_usd": 33.02},
            ],
        },
        "load_scenarios": [
            {"users": 1000, "estimated_monthly_cost_usd": 145.32, "scaling_assumptions": "1 vCPU, 2GB RAM, single AZ"},
            {"users": 10000, "estimated_monthly_cost_usd": 420.15, "scaling_assumptions": "2 vCPU, 4GB RAM, multi-AZ"},
            {"users": 100000, "estimated_monthly_cost_usd": 1850.00, "scaling_assumptions": "Auto-scaling 2-10 tasks, multi-AZ, read replicas"},
        ],
        "optimizations": [
            {"strategy": "graviton", "projected_savings_usd": 25.40, "description": "Switch to Graviton2/3 processors for 20% price reduction"},
            {"strategy": "reserved_instances", "projected_savings_usd": 35.00, "description": "1-year reserved capacity for baseline load"},
        ],
        "region_comparison": [
            {"region": "us-east-1", "monthly_cost_usd": 145.32},
            {"region": "eu-west-1", "monthly_cost_usd": 162.80},
            {"region": "ap-southeast-1", "monthly_cost_usd": 158.45},
        ],
    }


def mock_deployops_agent_impl(state: OrchestratorState) -> OrchestratorState:
    logger.info(f"[{state['job_id']}] Running MOCK DeployOps Agent")

    state["status"] = "deploying"
    state["orchestrator_metadata"]["current_node"] = "deployops_agent"
    state["orchestrator_metadata"]["nodes_executed"].append("deployops_agent")
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    state["deployops_result"] = cast(DeployOpsResult, build_mock_deployops_result(state["job_id"]))
    logger.info(f"[{state['job_id']}] DeployOps complete. URL: {state['deployops_result']['deployed_url']}")
    return state


def build_mock_deployops_result(job_id: str) -> dict:
    return {
        "job_id": job_id,
        "deployment_status": "success",
        "deployed_url": "https://devguard-app-123456.us-east-1.elb.amazonaws.com",
        "health_check": {
            "passed": True,
            "response_time_ms": 245,
            "status_code": 200,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        },
        "rollback_triggered": False,
        "rollback_reason": None,
        "error": None,
        "terraform_outputs": {
            "ecs_cluster_name": "devguard-app",
            "service_name": "devguard-api",
            "alb_dns": "devguard-app-123456.us-east-1.elb.amazonaws.com",
        },
        "artifacts": {
            "terraform": {
                "files": {
                    "main.tf": 'resource "aws_instance" "web" {\n  ami = "ami-12345678"\n  instance_type = "t3.micro"\n}',
                    "variables.tf": 'variable "region" {\n  default = "us-east-1"\n}',
                    "outputs.tf": 'output "url" {\n  value = aws_instance.web.public_dns\n}',
                },
                "variables": {
                    "region": "us-east-1",
                    "environment": "dev",
                },
            },
            "dockerfile": "FROM python:3.12\nCOPY . /app\nCMD [\"python\", \"app.py\"]",
            "docker_image": {
                "name": "devguard-app",
                "tag": "latest",
            },
            "source_code": "/tmp/repo_abc123",
        },
        "aws_config": {
            "region": "us-east-1",
            "ecs_cluster": "devguard-cluster",
            "service_name": "app-service",
            "task_cpu": "256",
            "task_memory": "512",
        },
        "deployment_config": {
            "strategy": "rolling",
            "health_check_path": "/health",
            "health_check_port": 8080,
            "timeout_minutes": 5,
            "min_healthy_percent": 50,
            "max_percent": 200,
        },
        "approval": {
            "deploy_approved": True,
            "approved_by": "user@email.com",
        },
    }


def health_check_impl(state: OrchestratorState) -> OrchestratorState:
    logger.info(f"[{state['job_id']}] Running health check...")

    state["status"] = "health_checking"
    state["orchestrator_metadata"]["current_node"] = "health_check"
    state["orchestrator_metadata"]["nodes_executed"].append("health_check")
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    deployops = state.get("deployops_result")
    if deployops and deployops.get("health_check", {}).get("passed"):
        logger.info(f"[{state['job_id']}] Health check PASSED")
        state["status"] = "completed"
    else:
        logger.error(f"[{state['job_id']}] Health check FAILED. Triggering rollback...")
        state["status"] = "rolled_back"
        if deployops:
            deployops["rollback_triggered"] = True
            if deployops.get("error") and not deployops.get("rollback_reason"):
                deployops["rollback_reason"] = deployops["error"]
            elif not deployops.get("error"):
                deployops["rollback_reason"] = "Health check failed after deployment"
            deployops["deployment_status"] = "rolled_back"

    return state


def generate_report_impl(state: OrchestratorState) -> OrchestratorState:
    from .report import generate_report

    job_id = state["job_id"]
    logger.info(f"[{job_id}] Generating final report...")

    state["orchestrator_metadata"]["current_node"] = "generate_report"
    state["orchestrator_metadata"]["nodes_executed"].append("generate_report")
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    start_time = datetime.fromisoformat(state["orchestrator_metadata"]["start_time"])
    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    state["orchestrator_metadata"]["elapsed_seconds"] = elapsed

    try:
        state["final_report"] = cast(FinalReport, generate_report(state))
        logger.info(
            f"[{job_id}] Report ready: "
            f"{state['final_report'].get('formats_available')} in {elapsed:.2f}s"
        )
    except Exception as exc:
        logger.error(f"[{job_id}] Report generation failed: {exc}")
        state["final_report"] = cast(FinalReport, {
            "format": "json",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "download_url": None,
            "error": str(exc),
            "summary": _summary_fallback(state, elapsed),
        })

    return state


def _summary_fallback(state: OrchestratorState, elapsed: float) -> dict:
    codesec = state.get("codesec_result") or {}
    summary = codesec.get("summary", {})
    infracost = state.get("infracost_result") or {}
    deployops = state.get("deployops_result")

    return {
        "total_vulnerabilities": summary.get("sast_findings_count", 0),
        "critical_count": summary.get("total_critical", 0),
        "estimated_monthly_cost_usd": (infracost.get("cost_estimate") or {}).get("monthly_cost_usd", 0),
        "deployment_status": deployops.get("deployment_status", "not_deployed") if deployops else "not_deployed",
        "recommendations": (codesec.get("security_score") or {}).get("recommendations", []),
        "pipeline_duration_seconds": elapsed,
    }


def route_after_codesec(state: OrchestratorState) -> str:
    if state.get("status") in ("failed", "rejected"):
        return "end"
    if state.get("codesec_result") is None:
        return "end"
    return "human_gate_1"


def route_after_gate_1(state: OrchestratorState) -> str:
    if state.get("status") in ("failed", "rejected"):
        return "end"
    if state["human_gates"]["gate_1_pre_infracost"]["approved"]:
        return "infracost_agent"
    return "end"


def route_after_infracost(state: OrchestratorState) -> str:
    if state.get("status") in ("failed", "rejected"):
        return "end"
    if state.get("infracost_result") is None:
        return "end"
    return "human_gate_2"


def route_after_gate_2(state: OrchestratorState) -> str:
    if state.get("status") in ("failed", "rejected"):
        return "end"
    gate = state["human_gates"]["gate_2_pre_deployops"]
    if gate.get("requested_changes") or state.get("infracost_feedback"):
        if len(state.get("infracost_iterations", [])) < MAX_INFRACOST_ITERATIONS:
            return "infracost_agent"
        logger.warning(
            "[%s] Gate 2 regeneration cap reached (%d); halting.",
            state["job_id"], MAX_INFRACOST_ITERATIONS,
        )
        return "end"
    if gate["approved"]:
        return "deployops_agent"
    return "end"


def route_after_deployops(state: OrchestratorState) -> str:
    if state.get("status") in ("failed", "rejected"):
        return "end"
    if state.get("deployops_result") is None:
        return "end"
    return "health_check"


def route_after_health_check(state: OrchestratorState) -> str:
    if state.get("status") == "completed":
        return "generate_report"
    return "end"


def codesec_agent_impl(state: OrchestratorState) -> OrchestratorState:
    from .agent_adapters import call_codesec, run_sync

    job_id = state["job_id"]
    logger.info(f"[{job_id}] CodeSec node starting for: {state['repo_url']}")

    state["status"] = "analyzing"
    state["orchestrator_metadata"]["current_node"] = "codesec_agent"
    state["orchestrator_metadata"]["nodes_executed"].append("codesec_agent")
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    state["codesec_result"] = run_sync(call_codesec(state["repo_url"], job_id))

    score = state["codesec_result"].get("security_score", {}).get("score", 0)
    grade = state["codesec_result"].get("security_score", {}).get("grade", "N/A")
    logger.info(f"[{job_id}] CodeSec complete. Score: {score}/100 (Grade {grade})")
    return state


def infracost_agent_impl(state: OrchestratorState) -> OrchestratorState:
    from .agent_adapters import call_infracost, run_sync

    job_id = state["job_id"]
    logger.info(f"[{job_id}] InfraCost node starting")

    state["status"] = "infra_generating"
    state["orchestrator_metadata"]["current_node"] = "infracost_agent"
    state["orchestrator_metadata"]["nodes_executed"].append("infracost_agent")
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    feedback = state.get("infracost_feedback")
    previous_result = state.get("infracost_result")
    iteration_number = len(state.get("infracost_iterations") or []) + 1

    state["infracost_result"] = cast(InfraCostResult, run_sync(
        call_infracost(
            state["codesec_result"] or {},
            job_id,
            feedback=feedback,
            previous_result=previous_result,
            iteration_number=iteration_number,
        )
    ))

    if feedback:
        iterations = list(state.get("infracost_iterations") or [])
        iterations.append({
            "iteration": len(iterations) + 1,
            "prompt": feedback,
            "result": cast(dict, state["infracost_result"]),
            "requested_at": datetime.now(timezone.utc).isoformat(),
        })
        state["infracost_iterations"] = iterations
        state["infracost_feedback"] = None

    cost = state["infracost_result"].get("cost_estimate", {}).get("monthly_cost_usd", 0)
    logger.info(f"[{job_id}] InfraCost complete. Est. cost: ${cost}/mo")
    return state


def deployops_agent_impl(state: OrchestratorState) -> OrchestratorState:
    from .agent_adapters import (
        call_deployops,
        run_sync,
        translate_infracost_to_deploy_payload,
        use_real_deployops,
    )

    job_id = state["job_id"]
    logger.info(f"[{job_id}] DeployOps node starting")

    state["status"] = "deploying"
    state["orchestrator_metadata"]["current_node"] = "deployops_agent"
    state["orchestrator_metadata"]["nodes_executed"].append("deployops_agent")
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    gate_2 = state["human_gates"]["gate_2_pre_deployops"]
    approved_by = gate_2.get("approved_by") or "unknown"

    deploy_payload: dict = {}
    if use_real_deployops():
        raw_output = (state.get("infracost_result") or {}).get("_deploy_inputs")
        if not raw_output:
            raise ValueError(
                "DeployOps is in REAL mode but InfraCost produced no "
                "_deploy_inputs (InfraCost is probably still mocked). Set "
                "DEVGUARD_REAL_INFRACOST=1 as well, or turn DeployOps back to mock."
            )
        deploy_payload = translate_infracost_to_deploy_payload(
            job_id,
            raw_output,
            approved_by=approved_by,
            repo_url=state.get("repo_url"),
            is_update=state.get("is_update", False),
            existing_deployment=state.get("existing_deployment"),
        )

    state["deployops_result"] = cast(DeployOpsResult, run_sync(call_deployops(deploy_payload, job_id)))

    logger.info(f"[{job_id}] DeployOps complete. URL: {state['deployops_result'].get('deployed_url')}")
    return state