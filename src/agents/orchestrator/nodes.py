"""
DevGuard AI - Orchestrator Nodes
==================================
Agent node implementations for the LangGraph workflow, plus the conditional
routing functions that decide which node runs next.

Sprint 1: all three agent nodes below are MOCKS returning realistic static
payloads (matching Nada's codesec-mock-schema.json / Karim's InfraCost
schema / Oussema's deployops-mock-schema.json), so the graph could be built
and tested end-to-end before the real agents existed.

Sprint 2 (T-2.17): these mock implementations are what needs to be replaced
by real calls to CodeSecAgent.analyze(), run_pipeline() (InfraCost), and
DeployOpsAgent.deploy() - see the TODO markers below.

Split out of graph.py (originally Sections 3, 5, and 6).

Owner: Hbib (Subgroup 2 - Execution & Control)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .state import OrchestratorState

logger = logging.getLogger(__name__)


# =============================================================================
# SECTION 3: MOCK AGENT FUNCTIONS (Sprint 1)
# =============================================================================
# TODO (T-2.17): replace each of these three functions with a real call to
# the corresponding agent. See docs/api-contracts/ for the exact contracts:
#   - CodeSec (Nada):    await CodeSecAgent().analyze(repo_url, job_id)  [async class method]
#   - InfraCost (Karim): run_pipeline(payload, region=..., environment=...) [sync plain function]
#   - DeployOps (Oussema): await DeployOpsAgent().deploy(payload)        [async class method]
# NOTE: the three contracts are NOT uniform (async class / async class / sync
# function) - InfraCost's call will need to go through
# `await loop.run_in_executor(None, run_pipeline, payload)` to stay
# consistent with the rest of this (async-friendly) graph.

def mock_codesec_agent_impl(state: OrchestratorState) -> OrchestratorState:
    """MOCK: CodeSec Agent (Nada). CDC: US-1.1.1 to US-1.1.6"""
    logger.info(f"[{state['job_id']}] Running MOCK CodeSec Agent for: {state['repo_url']}")

    state["status"] = "analyzing"
    state["orchestrator_metadata"]["current_node"] = "codesec_agent"
    state["orchestrator_metadata"]["nodes_executed"].append("codesec_agent")
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    state["codesec_result"] = {
        "job_id": state["job_id"],
        "status": "completed",
        "error": None,
        "repo_url": state["repo_url"],
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
                "terraform": 600
            }
        },
        "phases": [
            {"name": "clone", "status": "completed", "started_at": "2026-07-08T10:00:00Z", "completed_at": "2026-07-08T10:00:05Z"},
            {"name": "stack_detection", "status": "completed", "started_at": "2026-07-08T10:00:06Z", "completed_at": "2026-07-08T10:00:10Z"},
            {"name": "sast", "status": "completed", "started_at": "2026-07-08T10:00:11Z", "completed_at": "2026-07-08T10:00:20Z"},
            {"name": "secrets", "status": "completed", "started_at": "2026-07-08T10:00:21Z", "completed_at": "2026-07-08T10:00:25Z"},
            {"name": "dependencies", "status": "completed", "started_at": "2026-07-08T10:00:26Z", "completed_at": "2026-07-08T10:00:30Z"},
            {"name": "dockerfile_scan", "status": "completed", "started_at": "2026-07-08T10:00:31Z", "completed_at": "2026-07-08T10:00:35Z"},
            {"name": "sbom", "status": "completed", "started_at": "2026-07-08T10:00:36Z", "completed_at": "2026-07-08T10:00:40Z"},
            {"name": "scoring", "status": "completed", "started_at": "2026-07-08T10:00:41Z", "completed_at": "2026-07-08T10:00:45Z"}
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
            "total_info": 0
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
                "compose_detected": True
            },
            "confidence": 0.92,
            "detected_files": ["requirements.txt", "Dockerfile", "main.py", "docker-compose.yml"]
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
                "remediation": "Use parameterized queries with SQLAlchemy ORM"
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
                "remediation": "Use DOMPurify or React's built-in escaping"
            }
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
                "remediation": "Rotate the key and use AWS Secrets Manager"
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
                "remediation": "Use environment variables or a secret manager"
            }
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
                    "description": "Unintended leak of Proxy-Authorization header"
                }
            ]
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
                "remediation": "Add 'USER appuser' after creating non-root user"
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
                "remediation": "Pin to specific version: FROM python:3.12-slim"
            }
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
                    "source_file": "requirements.txt"
                }
            ],
            "download_url": f"/api/jobs/{state['job_id']}/sbom/download"
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
                "stack_detection": 7
            },
            "severity_counts": {
                "critical": 1,
                "high": 4,
                "medium": 5,
                "low": 2,
                "info": 0
            },
            "recommendations": [
                "Fix 1 critical SQL injection in app/db.py:24",
                "Update requests to >= 2.31.0 (CVE-2023-32681)",
                "Remove 2 hardcoded secrets from .env and scripts/deploy.sh",
                "Run Dockerfile as non-root user (Dockerfile:5)"
            ]
        }
    }

    score = state["codesec_result"]["security_score"]["score"]
    grade = state["codesec_result"]["security_score"]["grade"]
    logger.info(f"[{state['job_id']}] CodeSec complete. Score: {score}/100 (Grade {grade})")
    return state


def mock_infracost_agent_impl(state: OrchestratorState) -> OrchestratorState:
    """MOCK: InfraCost Agent (Karim). Sprint 1 mock data."""
    logger.info(f"[{state['job_id']}] Running MOCK InfraCost Agent")

    state["status"] = "infra_generating"
    state["orchestrator_metadata"]["current_node"] = "infracost_agent"
    state["orchestrator_metadata"]["nodes_executed"].append("infracost_agent")
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    state["infracost_result"] = {
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

    logger.info(f"[{state['job_id']}] InfraCost complete. Est. cost: ${state['infracost_result']['cost_estimate']['monthly_cost_usd']}/mo")
    return state


def mock_deployops_agent_impl(state: OrchestratorState) -> OrchestratorState:
    """
    MOCK: DeployOps Agent (Oussema). Sprint 1 mock data.
    ALIGNED with deployops-mock-schema.json v1.0
    """
    logger.info(f"[{state['job_id']}] Running MOCK DeployOps Agent")

    state["status"] = "deploying"
    state["orchestrator_metadata"]["current_node"] = "deployops_agent"
    state["orchestrator_metadata"]["nodes_executed"].append("deployops_agent")
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    # ALIGNED with deployops-mock-schema.json - all required fields present
    state["deployops_result"] = {
        # ===== CHAMPS REQUIS par deployops-mock-schema.json =====
        "job_id": state["job_id"],  # <- REQUIS: doit matcher l'orchestrateur
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

        # ===== CHAMPS OPTIONNELS du schema =====
        "artifacts": {
            "terraform": {
                "files": {
                    "main.tf": 'resource "aws_instance" "web" {\n  ami = "ami-12345678"\n  instance_type = "t3.micro"\n}',
                    "variables.tf": 'variable "region" {\n  default = "us-east-1"\n}',
                    "outputs.tf": 'output "url" {\n  value = aws_instance.web.public_dns\n}'
                },
                "variables": {
                    "region": "us-east-1",
                    "environment": "dev"
                }
            },
            "dockerfile": "FROM python:3.12\nCOPY . /app\nCMD [\"python\", \"app.py\"]",
            "docker_image": {
                "name": "devguard-app",
                "tag": "latest"
            },
            "source_code": "/tmp/repo_abc123"
        },
        "aws_config": {
            "region": "us-east-1",
            "ecs_cluster": "devguard-cluster",
            "service_name": "app-service",
            "task_cpu": "256",
            "task_memory": "512"
        },
        "deployment_config": {
            "strategy": "rolling",
            "health_check_path": "/health",
            "health_check_port": 8080,
            "timeout_minutes": 5,
            "min_healthy_percent": 50,
            "max_percent": 200
        },
        "approval": {
            "deploy_approved": True,
            "approved_by": "user@email.com"
        }
    }

    logger.info(f"[{state['job_id']}] DeployOps complete. URL: {state['deployops_result']['deployed_url']}")
    return state


# =============================================================================
# SECTION 5: HEALTH CHECK & REPORT
# =============================================================================

def health_check_impl(state: OrchestratorState) -> OrchestratorState:
    """Health check verification after deployment. CDC: US-1.2.2"""
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
            deployops["rollback_reason"] = "Health check failed after deployment"
            deployops["deployment_status"] = "rolled_back"

    return state


def generate_report_impl(state: OrchestratorState) -> OrchestratorState:
    """Generate final report with all agent results. CDC: US-2.2.6"""
    logger.info(f"[{state['job_id']}] Generating final report...")

    state["orchestrator_metadata"]["current_node"] = "generate_report"
    state["orchestrator_metadata"]["nodes_executed"].append("generate_report")
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    codesec = state.get("codesec_result") or {}
    summary = codesec.get("summary", {})
    vuln_count = summary.get("sast_findings_count", 0)
    critical_count = summary.get("total_critical", 0)

    infracost = state.get("infracost_result") or {}
    cost_estimate = infracost.get("cost_estimate", {})
    monthly_cost = cost_estimate.get("monthly_cost_usd", 0.0)

    deployops = state.get("deployops_result")
    deploy_status = deployops.get("deployment_status", "not_deployed") if deployops else "not_deployed"

    recommendations = []
    security_score = codesec.get("security_score", {})
    if security_score and security_score.get("recommendations"):
        recommendations.extend(security_score["recommendations"])

    optimizations = infracost.get("optimizations", [])
    if optimizations:
        recommendations.extend([opt.get("description", "") for opt in optimizations])

    start_time = datetime.fromisoformat(state["orchestrator_metadata"]["start_time"])
    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    state["orchestrator_metadata"]["elapsed_seconds"] = elapsed

    state["final_report"] = {
        "format": "html",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "download_url": None,
        "summary": {
            "total_vulnerabilities": vuln_count,
            "critical_count": critical_count,
            "estimated_monthly_cost_usd": monthly_cost,
            "deployment_status": deploy_status,
            "recommendations": recommendations,
            "pipeline_duration_seconds": elapsed,
        },
    }

    logger.info(f"[{state['job_id']}] Report generated in {elapsed:.2f}s.")
    return state


# =============================================================================
# SECTION 6: CONDITIONAL ROUTING
# =============================================================================

def route_after_codesec(state: OrchestratorState) -> str:
    # BUGFIX v1.0.5: also route to "end" on "rejected" (e.g. a future real
    # CodeSec agent rejecting an unsupported repo), not just "failed".
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
    if state["human_gates"]["gate_2_pre_deployops"]["approved"]:
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
