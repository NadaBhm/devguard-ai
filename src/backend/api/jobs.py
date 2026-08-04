"""
Job endpoints.

The FastAPI process drives the LangGraph orchestrator in-process
(get_orchestrator_graph built once at startup), because the orchestrator
currently uses an in-memory MemorySaver checkpointer (graph.py). Each call to
run_workflow / resume_workflow returns at the next human gate; the API persists
the resulting state to Postgres when the run reaches a terminal state.
"""

"""
functions : 
- get/create system user
- get/create project
- create run
- publish progress to redis
- create job
- approve job
- get job
- list jobs
- current gate


"""


import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..persistence import persist_results, serialize_state, update_run_state
from ..redis_client import publish_gate, publish_progress, publish_results_ready

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])

# The orchestrator's repo_url is the same as the project's github_url.
SYSTEM_USER_EMAIL = "system@devguard.ai"


class JobCreate(BaseModel):
    repo_url: str = Field(..., description="Public GitHub repository URL")
    commit_sha: str = "HEAD"
    commit_message: str | None = None
    default_branch: str = "main"


class ApproveRequest(BaseModel):
    approved: bool
    comment: str = ""
    approved_by: str = ""


def _get_or_create_system_user(db: Session) -> models.User:
    """Minimal ownership fallback: a single system user for jobs created
    without an authenticated user. Swap for JWT auth when ready."""
    user = db.query(models.User).filter(models.User.email == SYSTEM_USER_EMAIL).first()
    if user:
        return user
    from ..auth import get_password_hash
    user = models.User(
        email=SYSTEM_USER_EMAIL,
        hashed_password=get_password_hash("system-user-not-for-login"),
        first_name="System",
        last_name="User",
        is_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _get_or_create_project(
    db: Session, user_id: str, repo_url: str, default_branch: str
) -> models.Project:
    project = db.query(models.Project).filter(models.Project.github_url == repo_url).first()
    if project:
        return project
    repo_name = repo_url.rstrip("/").rsplit("/", 1)[-1]
    project = models.Project(
        user_id=user_id,
        repo_name=repo_name,
        github_url=repo_url,
        default_branch=default_branch,
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _create_run(
    db: Session,
    project: models.Project,
    user_id: str,
    commit_sha: str,
    commit_message: str | None,
) -> models.AnalysisRun:
    run = models.AnalysisRun(
        project_id=project.id,
        commit_sha=commit_sha,
        commit_message=commit_message,
        status="queued",
        triggered_by=user_id,
        started_at=datetime.utcnow(),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _publish(state: dict, progress: int, message: str) -> None:
    job_id = state.get("job_id", "")
    phase = state.get("orchestrator_metadata", {}).get("current_node", "unknown")
    publish_progress(job_id, phase=phase, progress=progress, message=message)


@router.post("/", status_code=201)
def create_job(body: JobCreate, db: Session = Depends(get_db)):
    """Start a job: create Project + AnalysisRun, run the orchestrator up to
    the first human gate, and persist the resulting state."""
    user = _get_or_create_system_user(db)
    project = _get_or_create_project(db, user.id, body.repo_url, body.default_branch)
    run = _create_run(db, project, user.id, body.commit_sha, body.commit_message)

    logger.info(f"Starting orchestrator for run {run.id} | repo {body.repo_url}")
    publish_progress(str(run.id), phase="start", progress=5, message="Job started")

    try:
        from src.agents.orchestrator.graph import run_workflow
        state = run_workflow(repo_url=body.repo_url, thread_id=str(run.id))
        state["job_id"] = str(run.id)  # keep orchestrator job_id == DB run id
    except Exception as exc:
        logger.error(f"run_workflow failed for {run.id}: {exc}", exc_info=True)
        state = {"status": "failed", "error": str(exc), "job_id": str(run.id)}

    run = update_run_state(db, str(run.id), state)
    _publish(state, 30, f"Orchestrator status: {state.get('status')}")

    gate = _current_gate(state)
    if gate:
        publish_gate(str(run.id), gate, "awaiting_approval")

    return {
        "job_id": str(run.id),
        "status": run.status,
        "orchestrator_status": state.get("status"),
        "gate": gate,
        "state": serialize_state(state),
    }


@router.post("/{job_id}/approve")
def approve_job(job_id: str, body: ApproveRequest, db: Session = Depends(get_db)):
    """Resume a paused workflow at its human gate with an approval/rejection."""
    run = db.query(models.AnalysisRun).filter(models.AnalysisRun.id == job_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Job not found")

    try:
        from src.agents.orchestrator.graph import resume_workflow
        resume_data = {
            "approved": body.approved,
            "comment": body.comment,
            "approved_by": body.approved_by or SYSTEM_USER_EMAIL,
        }
        state = resume_workflow(thread_id=job_id, resume_data=resume_data)
    except Exception as exc:
        logger.error(f"resume_workflow failed for {job_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Resume failed: {exc}")

    run = update_run_state(db, job_id, state)

    if state.get("status") in ("completed", "rolled_back", "failed", "rejected"):
        # The orchestrator runs in-process, so write results synchronously to
        # the schema tables (persist_results is idempotent).
        persist_results(db, job_id, state)
        _publish(state, 100, f"Run finished: {state.get('status')}")
        publish_results_ready(job_id)
    else:
        _publish(state, 60, f"Orchestrator status: {state.get('status')}")
        gate = _current_gate(state)
        if gate:
            publish_gate(job_id, gate, "awaiting_approval")

    return {
        "job_id": job_id,
        "status": run.status,
        "orchestrator_status": state.get("status"),
        "gate": _current_gate(state),
        "state": serialize_state(state),
    }


@router.get("/{job_id}/results")
def get_job_results(job_id: str, db: Session = Depends(get_db)):
    """Return the normalized result tables for a run (findings, cost, IaC, deploy)."""
    run = db.query(models.AnalysisRun).filter(models.AnalysisRun.id == job_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Job not found")

    def _rows(query):
        return [dict((c.name, getattr(r, c.name)) for c in r.__table__.columns) for r in query]

    findings = _rows(
        db.query(models.CodeSecFinding)
        .filter(models.CodeSecFinding.run_id == job_id)
        .order_by(models.CodeSecFinding.severity.desc())
    )
    estimates = _rows(
        db.query(models.InfracostEstimate)
        .filter(models.InfracostEstimate.run_id == job_id)
        .order_by(models.InfracostEstimate.monthly_cost_usd.desc())
    )
    artifacts = _rows(
        db.query(models.TerraformArtifact).filter(models.TerraformArtifact.run_id == job_id)
    )
    deployments = _rows(
        db.query(models.Deployment).filter(models.Deployment.run_id == job_id)
    )
    agent_tasks = _rows(
        db.query(models.AgentTask).filter(models.AgentTask.run_id == job_id)
    )

    return {
        "job_id": job_id,
        "status": run.status,
        "agent_tasks": agent_tasks,
        "codesec_findings": findings,
        "infracost_estimates": estimates,
        "terraform_artifacts": artifacts,
        "deployments": deployments,
    }


@router.get("/{job_id}")
def get_job(job_id: str, db: Session = Depends(get_db)):
    """Return the persisted job state (analysis run + full orchestrator state)."""
    run = db.query(models.AnalysisRun).filter(models.AnalysisRun.id == job_id).first()
    if not run:
        raise HTTPException(status_code=404, detail="Job not found")

    state = run.run_metadata or {}
    return {
        "job_id": str(run.id),
        "status": run.status,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "duration_seconds": run.duration_seconds,
        "orchestrator_status": state.get("status"),
        "gate": _current_gate(state),
        "state": state,
    }


@router.get("/")
def list_jobs(db: Session = Depends(get_db)):
    """List all analysis runs with their coarse status."""
    runs = (
        db.query(models.AnalysisRun)
        .order_by(models.AnalysisRun.started_at.desc())
        .limit(100)
        .all()
    )
    return {
        "jobs": [
            {
                "job_id": str(run.id),
                "status": run.status,
                "repo_url": run.project.github_url if run.project else None,
                "started_at": run.started_at,
                "completed_at": run.completed_at,
                "duration_seconds": run.duration_seconds,
            }
            for run in runs
        ]
    }


def _current_gate(state: dict) -> str | None:
    """Return the name of the gate the run is currently paused at, if any.

    LangGraph signals paused human gates via the __interrupt__ key (a list of
    interrupt payloads). Fall back to the status string when that is absent.
    """
    interrupts = state.get("__interrupt__")
    if isinstance(interrupts, list) and interrupts:
        entry = interrupts[0]
        value = entry.get("value", {}) if isinstance(entry, dict) else getattr(entry, "value", {})
        if isinstance(value, dict):
            gate = value.get("gate")
            if gate:
                return gate
    status = state.get("status")
    if status and status.startswith("awaiting_approval_gate"):
        return status
    return None
