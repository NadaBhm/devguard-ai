"""
Database persistence helpers for orchestrator state.

The light writes (status + run_metadata on analysis_runs) and the heavier
materialization into the child tables both happen in the request path, since
the orchestrator runs in-process inside the API.
"""
import json
import logging
from datetime import datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from .models import (
    AgentTask,
    AnalysisRun,
    CodeSecFinding,
    Deployment,
    InfracostEstimate,
    TerraformArtifact,
)

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = ("completed", "rolled_back", "failed", "rejected")


def coarse_status(orch_status: str) -> str:
    """Map the rich orchestrator status onto the simple AnalysisRun enum."""
    if orch_status == "completed":
        return "completed"
    if orch_status in TERMINAL_STATUSES:
        return "failed"
    return "running"


def serialize_state(state: dict) -> dict:
    """Make an orchestrator state dict JSON-safe (drop __interrupt__, coerce values)."""
    clean = {k: v for k, v in state.items() if not k.startswith("__")}
    return json.loads(json.dumps(clean, default=str))


def update_run_state(db: Session, run_id: str, state: dict) -> AnalysisRun:
    """
    Persist coarse status + full orchestrator state onto the analysis run row.
    Cheap enough to call in the request path after every start/resume.
    """
    run = db.query(AnalysisRun).filter(AnalysisRun.id == run_id).first()
    if not run:
        raise ValueError(f"AnalysisRun {run_id} not found")

    run.status = coarse_status(state.get("status", "running"))
    run.run_metadata = serialize_state(state)
    if state.get("status") in TERMINAL_STATUSES:
        run.completed_at = datetime.utcnow()
        if run.started_at:
            run.duration_seconds = int((run.completed_at - run.started_at).total_seconds())
    db.commit()
    db.refresh(run)
    return run


def persist_results(db: Session, run_id: str, state: dict) -> int:
    """
    Materialize a completed orchestrator state into the child result tables.
    Idempotent: existing child rows for the run are replaced first.

    Returns the number of rows written.
    """
    # Idempotency: remove previously persisted children for this run.
    db.query(Deployment).filter(Deployment.run_id == run_id).delete()
    db.query(TerraformArtifact).filter(TerraformArtifact.run_id == run_id).delete()
    db.query(InfracostEstimate).filter(InfracostEstimate.run_id == run_id).delete()
    db.query(CodeSecFinding).filter(CodeSecFinding.run_id == run_id).delete()
    db.query(AgentTask).filter(AgentTask.run_id == run_id).delete()

    written = 0

    # ---- agent_tasks ----------------------------------------------------
    for agent_name, result in {
        "codesec": state.get("codesec_result"),
        "infracost": state.get("infracost_result"),
        "deployops": state.get("deployops_result"),
    }.items():
        if not result:
            continue
        db.add(AgentTask(
            run_id=run_id,
            agent_name=agent_name,
            celery_task_id=str(uuid4()),
            status="success" if result.get("status", "success") != "failed" else "failure",
            started_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
            retry_count=0,
            raw_result=result,
        ))
        written += 1

    # ---- codesec findings ------------------------------------------------
    codesec = state.get("codesec_result") or {}
    for finding in codesec.get("sast_findings", []):
        db.add(CodeSecFinding(
            run_id=run_id,
            scanner=finding.get("tool", "semgrep"),
            severity=finding.get("severity", "info"),
            file_path=finding.get("file", ""),
            line_number=finding.get("line"),
            rule_id=finding.get("rule_id", "unknown"),
            rule_title=finding.get("message", "No title"),
            description=finding.get("message", ""),
            remediation_hint=finding.get("remediation"),
            raw_json=finding,
        ))
        written += 1

    # ---- infracost estimates ----------------------------------------------
    infracost = state.get("infracost_result") or {}
    for item in (infracost.get("cost_estimate", {}).get("breakdown", []) or []):
        monthly = item.get("monthly_cost_usd", 0)
        db.add(InfracostEstimate(
            run_id=run_id,
            resource_type=item.get("service", "unknown"),
            resource_name=item.get("service", "unknown"),
            monthly_cost_usd=monthly,
            annual_cost_usd=round(monthly * 12, 2),
            usage_assumptions=infracost.get("load_scenarios"),
            cost_drivers=infracost.get("optimizations"),
            confidence_level="medium",
        ))
        written += 1

    # ---- terraform artifacts ------------------------------------------------
    terraform = infracost.get("generated_terraform") or {}
    for file_path, content in (
        ("main.tf", terraform.get("main_tf")),
        ("variables.tf", terraform.get("variables_tf")),
        ("outputs.tf", terraform.get("outputs_tf")),
    ):
        if content:
            db.add(TerraformArtifact(
                run_id=run_id,
                artifact_type="terraform",
                file_path=file_path,
                content=content,
            ))
            written += 1

    # ---- deployments ---------------------------------------------------------
    deployops = state.get("deployops_result") or {}
    status_map = {"success": "succeeded", "failed": "failed", "rolled_back": "rolled_back"}
    deploy_status = status_map.get(deployops.get("deployment_status", "pending"), "failed")
    db.add(Deployment(
        run_id=run_id,
        environment="dev",
        aws_region=(deployops.get("aws_config") or {}).get("region", "us-east-1"),
        status=deploy_status,
        applied_at=datetime.utcnow() if deploy_status == "succeeded" else None,
        rollback_reason=deployops.get("rollback_reason"),
        infrastructure_json=deployops,
        cost_total_monthly=None,
    ))
    written += 1

    db.commit()
    logger.info(f"Persisted {written} result rows for run {run_id}")
    return written
