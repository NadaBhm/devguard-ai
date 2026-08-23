"""
DevGuard AI - Orchestrator State Definitions
TypedDict definitions and the factory for a fresh workflow state.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional, TypedDict

GRAPH_VERSION = "1.3.1"
MAX_INFRACOST_ITERATIONS = 3


class InfraCostResult(TypedDict):
    architecture_recommendation: Literal["ecs_fargate", "lambda", "ec2", "hybrid"]
    justification: str
    generated_terraform: dict
    cost_estimate: dict
    load_scenarios: list[dict]
    optimizations: list[dict]
    region_comparison: list[dict]


class HealthCheckResult(TypedDict):
    passed: bool
    response_time_ms: int
    status_code: int
    checked_at: Optional[str]


class DeployOpsArtifactsTerraform(TypedDict):
    files: dict[str, str]
    variables: dict[str, str]


class DeployOpsArtifacts(TypedDict):
    terraform: DeployOpsArtifactsTerraform
    dockerfile: str
    docker_image: dict[str, str]
    docker_images: list[dict[str, str]]
    source_code: Optional[str]


class DeployOpsAwsConfig(TypedDict):
    region: str
    ecs_cluster: str
    service_name: str
    task_cpu: str
    task_memory: str


class DeployOpsDeploymentConfig(TypedDict):
    strategy: Literal["rolling", "blue-green"]
    health_check_path: str
    health_check_port: int
    timeout_minutes: int
    min_healthy_percent: int
    max_percent: int


class DeployOpsApproval(TypedDict):
    deploy_approved: bool
    approved_by: str


class TerraformOutputs(TypedDict):
    ecs_cluster_name: str
    service_name: str
    alb_dns: str


class DeployOpsResult(TypedDict):
    job_id: str
    deployment_status: Literal["success", "failed", "rolled_back"]
    deployed_url: Optional[str]
    health_check: HealthCheckResult
    rollback_triggered: bool
    rollback_reason: Optional[str]
    error: Optional[str]
    terraform_outputs: TerraformOutputs
    artifacts: Optional[DeployOpsArtifacts]
    aws_config: Optional[DeployOpsAwsConfig]
    deployment_config: Optional[DeployOpsDeploymentConfig]
    approval: Optional[DeployOpsApproval]


class HumanGate(TypedDict):
    required: bool
    approved: Optional[bool]
    comment: Optional[str]
    approved_at: Optional[str]
    approved_by: Optional[str]
    requested_changes: Optional[str]


class HumanGates(TypedDict):
    gate_1_pre_infracost: HumanGate
    gate_2_pre_deployops: HumanGate


class InfracostIteration(TypedDict):
    iteration: int
    prompt: str
    result: dict
    requested_at: str


class ErrorEntry(TypedDict):
    node: str
    attempt: int
    max_attempts: int
    message: str
    timestamp: str
    stack_trace: Optional[str]
    resolved: bool


class OrchestratorMetadata(TypedDict):
    graph_version: str
    start_time: str
    elapsed_seconds: float
    current_node: str
    nodes_executed: list[str]
    chat_session_id: Optional[str]


class FinalReportSummary(TypedDict):
    total_vulnerabilities: int
    critical_count: int
    estimated_monthly_cost_usd: float
    deployment_status: str
    recommendations: list[str]
    pipeline_duration_seconds: float


class FinalReport(TypedDict):
    format: Literal["pdf", "html", "json"]
    generated_at: str
    download_url: Optional[str]
    summary: FinalReportSummary


class ExistingDeploymentInfo(TypedDict):
    """The live ECS service an "update deployment" run redeploys onto,
    resolved by the backend from the project's last live Deployment row
    before run_workflow() is called."""
    region: str
    ecs_cluster: str
    service_name: str


class OrchestratorState(TypedDict):
    job_id: str
    repo_url: str
    status: Literal[
        "pending", "cloning", "analyzing", "awaiting_approval_gate_1",
        "infra_generating", "awaiting_approval_gate_2", "deploying",
        "health_checking", "completed", "failed", "rolled_back",
        "rejected",
    ]
    created_at: str
    updated_at: str
    codesec_result: Optional[dict]
    infracost_result: Optional[InfraCostResult]
    deployops_result: Optional[DeployOpsResult]
    human_gates: HumanGates
    error_log: list[ErrorEntry]
    orchestrator_metadata: OrchestratorMetadata
    final_report: Optional[FinalReport]
    infracost_feedback: Optional[str]
    infracost_iterations: list[InfracostIteration]

    # "Update deployment" flow: set once, up front, by the backend (which has
    # DB access the orchestrator itself never does) and threaded through
    # unchanged so deployops_agent_impl can redeploy onto the existing ECS
    # service instead of provisioning fresh infra, and human_gate_2 can show
    # a cost delta instead of only the new total.
    is_update: bool
    existing_deployment: Optional[ExistingDeploymentInfo]
    previous_monthly_cost_usd: Optional[float]


def create_initial_state(
    repo_url: str,
    job_id: str | None = None,
    *,
    is_update: bool = False,
    existing_deployment: ExistingDeploymentInfo | None = None,
    previous_monthly_cost_usd: float | None = None,
) -> OrchestratorState:
    now = datetime.now(timezone.utc).isoformat()
    job_id = job_id or str(uuid.uuid4())

    return {
        "job_id": job_id,
        "repo_url": repo_url,
        "status": "pending",
        "created_at": now,
        "updated_at": now,
        "codesec_result": None,
        "infracost_result": None,
        "deployops_result": None,
        "human_gates": {
            "gate_1_pre_infracost": {
                "required": True,
                "approved": None,
                "comment": None,
                "approved_at": None,
                "approved_by": None,
                "requested_changes": None,
            },
            "gate_2_pre_deployops": {
                "required": True,
                "approved": None,
                "comment": None,
                "approved_at": None,
                "approved_by": None,
                "requested_changes": None,
            },
        },
        "error_log": [],
        "orchestrator_metadata": {
            "graph_version": GRAPH_VERSION,
            "start_time": now,
            "elapsed_seconds": 0.0,
            "current_node": "start",
            "nodes_executed": [],
            "chat_session_id": None,
        },
        "final_report": None,
        "infracost_feedback": None,
        "infracost_iterations": [],
        "is_update": is_update,
        "existing_deployment": existing_deployment,
        "previous_monthly_cost_usd": previous_monthly_cost_usd,
    }