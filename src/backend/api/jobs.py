"""
Job endpoints.

The FastAPI process drives the LangGraph orchestrator in-process (graph built
once at startup); run_workflow / resume_workflow return at the next human gate,
and terminal-state results are persisted to Postgres.
"""


import asyncio
import logging
import tempfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..artifact_validation import allowed_file_path, validate_artifact
from ..auth import get_current_user
from ..database import get_db
from ..persistence import (
    derive_results_from_state,
    persist_results,
    serialize_state,
    update_run_state,
    _artifact_specs,
)
from ..redis_client import publish_gate, publish_progress, publish_results_ready
from src.agents.orchestrator.agent_adapters import report_mode

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobCreate(BaseModel):
    repo_url: str | None = Field(default=None, description="Public GitHub/GitLab repository URL or local folder path")
    source_type: str = Field(default="github", description="github | gitlab | local_folder")
    commit_sha: str = "HEAD"
    commit_message: str | None = None
    default_branch: str = "main"


class ApproveRequest(BaseModel):
    approved: bool
    comment: str = ""
    approved_by: str = ""
    request_regeneration: bool = False


class ArtifactEditRequest(BaseModel):
    file_path: str
    content: str


class ArtifactsEditRequest(BaseModel):
    """Batch of artifact edits applied atomically to the paused run's state and rows."""

    files: list[ArtifactEditRequest]


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


def _store_uploaded_files(files: list[UploadFile], job_id: str) -> Path:
    base_dir = Path(tempfile.mkdtemp(prefix=f"devguard_upload_{job_id}_"))
    for uploaded in files:
        if not uploaded.filename:
            continue
        relative = PurePosixPath(uploaded.filename)
        if relative.is_absolute() or any(part in ("..", "") for part in relative.parts):
            continue
        target = base_dir.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = uploaded.file.read()
        target.write_bytes(content)
    return base_dir


def _get_owned_run(db: Session, job_id: str, user_id: str) -> models.AnalysisRun:
    """Fetch a run owned by ``user_id`` or 404, without revealing existence."""
    run = (
        db.query(models.AnalysisRun)
        .filter(
            models.AnalysisRun.id == job_id,
            models.AnalysisRun.triggered_by == user_id,
        )
        .first()
    )
    if not run:
        raise HTTPException(status_code=404, detail="Job not found")
    return run


def _publish(state: dict, progress: int, message: str) -> None:
    job_id = state.get("job_id", "")
    phase = state.get("orchestrator_metadata", {}).get("current_node", "unknown")
    publish_progress(job_id, phase=phase, progress=progress, message=message)


# Coarse per-node percentages so clients stream beyond the 5/30/60/100 milestones.
_NODE_PROGRESS = {
    "codesec_agent": 15,
    "human_gate_1": 25,
    "infracost_agent": 40,
    "human_gate_2": 55,
    "deployops_agent": 70,
    "health_check": 85,
    "generate_report": 95,
}


def _publish_node_progress(job_id: str):
    def handler(node_name: str, node_state: dict) -> None:
        progress = _NODE_PROGRESS.get(node_name)
        if progress is None:
            return
        publish_progress(
            job_id,
            phase=node_name,
            progress=progress,
            message=f"Node '{node_name}' completed",
        )

    return handler


def _create_job_for_repo(
    db: Session,
    current_user: models.User,
    repo_url: str,
    default_branch: str,
    commit_sha: str,
    commit_message: str | None,
):
    project = _get_or_create_project(db, str(current_user.id), repo_url, default_branch)
    run = _create_run(db, project, str(current_user.id), commit_sha, commit_message)

    logger.info(f"Starting orchestrator for run {run.id} | repo {repo_url}")
    publish_progress(str(run.id), phase="start", progress=5, message="Job started")

    try:
        from src.agents.orchestrator.graph import run_workflow
        state = cast(dict[str, Any], run_workflow(
        repo_url=repo_url,
        thread_id=str(run.id),
        on_node_progress=_publish_node_progress(str(run.id)),
        commit_sha=commit_sha or "HEAD",
        ))
        state["job_id"] = str(run.id)
    except Exception as exc:
        logger.error(f"run_workflow failed for {run.id}: {exc}", exc_info=True)
        state = {"status": "failed", "error": str(exc), "job_id": str(run.id)}

    state = cast(dict[Any, Any], state)

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
        "mode": report_mode(),
        "state": serialize_state(dict(state)),
    }


@router.post("/", status_code=201)
def create_job(
    body: JobCreate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Create Project + AnalysisRun, run the orchestrator to the first human
    gate, and persist the resulting state."""
    if not body.repo_url:
        raise HTTPException(status_code=400, detail="A repository URL is required")
    return _create_job_for_repo(
        db,
        current_user,
        body.repo_url,
        body.default_branch,
        body.commit_sha,
        body.commit_message,
    )


@router.post("/upload", status_code=201)
def upload_job(
    files: list[UploadFile] = File(...),
    commit_sha: str = Form("HEAD"),
    commit_message: str | None = Form(default=None),
    default_branch: str = Form("main"),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    if not files:
        raise HTTPException(status_code=400, detail="No project files were uploaded")

    extracted_dir = _store_uploaded_files(files, "upload")
    repo_url = str(extracted_dir)
    return _create_job_for_repo(
        db,
        current_user,
        repo_url,
        default_branch,
        commit_sha,
        commit_message,
    )


@router.get("/{job_id}/check-update")
def check_update(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Compare the last analyzed commit against the default-branch tip, with
    no cloning or run started."""
    run = _get_owned_run(db, job_id, str(current_user.id))
    project = run.project

    from src.lib.git_remote import latest_remote_sha
    try:
        latest_sha = latest_remote_sha(project.github_url, project.default_branch)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"Could not check remote: {exc}") from exc

    last_run = (
        db.query(models.AnalysisRun)
        .filter(models.AnalysisRun.project_id == project.id)
        .order_by(models.AnalysisRun.started_at.desc())
        .first()
    )
    current_sha = last_run.commit_sha if last_run else None
    # CodeSec records commit_sha truncated to 12 chars (git rev-parse HEAD),
    # while git ls-remote returns the full 40-char SHA -- compare prefixes.
    has_update = current_sha is None or current_sha[:12] != latest_sha[:12]

    return {
        "job_id": job_id,
        "has_update": has_update,
        "latest_sha": latest_sha,
        "current_sha": current_sha,
    }


@router.post("/{job_id}/update", status_code=201)
def trigger_update(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Start a new run for the same project as an in-place update: full
    CodeSec + InfraCost re-analysis of the latest commit, then a lightweight
    redeploy onto the currently live ECS service, not fresh infrastructure."""
    run = _get_owned_run(db, job_id, str(current_user.id))
    project = run.project

    # "rolled_back" still counts as live: rollback swaps ECS to a previous
    # task-def revision without teardown; only "failed"/"destroyed" mean nothing to update.
    # Prefer the NEWEST deployment whose record actually carries usable ECS
    # targeting info — a failed later run must not block updating an older
    # healthy service.
    candidates = (
        db.query(models.Deployment)
        .join(models.AnalysisRun, models.Deployment.run_id == models.AnalysisRun.id)
        .filter(
            models.AnalysisRun.project_id == project.id,
            models.Deployment.status.in_(["succeeded", "rolled_back"]),
        )
        .order_by(models.Deployment.created_at.desc())
        .all()
    )
    deployment = None
    existing = {}
    for cand in candidates:
        cand_cfg = _extract_aws_config(cand)
        if cand_cfg.get("ecs_cluster") and cand_cfg.get("service_name"):
            deployment = cand
            existing = cand_cfg
            break
    if not deployment:
        raise HTTPException(status_code=400, detail="No live deployment to update")
    if not existing.get("ecs_cluster") or not existing.get("service_name"):
        raise HTTPException(
            status_code=400,
            detail="Deployment has no ECS cluster/service info to update",
        )
    if existing.get("cluster_was_guessed"):
        raise HTTPException(
            status_code=400,
            detail=(
                "Could not reliably determine the live ECS cluster for this "
                "deployment (older record, name was inferred rather than "
                "stored). Refusing to update automatically -- destroy and "
                "redeploy this project instead."
            ),
        )

    previous_run_id = deployment.run_id
    previous_costs = (
        db.query(models.InfracostEstimate.monthly_cost_usd)
        .filter(models.InfracostEstimate.run_id == previous_run_id)
        .all()
    )
    previous_monthly_cost_usd = (
        float(sum(row[0] for row in previous_costs)) if previous_costs else None
    )

    # Store resolved tip SHA (not literal "HEAD") for correct check-update.
    from src.lib.git_remote import latest_remote_sha
    try:
        update_target_sha = latest_remote_sha(project.github_url, project.default_branch)
    except RuntimeError:
        update_target_sha = "HEAD"

    new_run = _create_run(
    db, project, str(current_user.id), commit_sha=update_target_sha, commit_message="Update deployment"
    )

    logger.info(
        f"Starting update run {new_run.id} for project {project.id} "
        f"(reusing deployment from run {previous_run_id})"
    )
    publish_progress(str(new_run.id), phase="start", progress=5, message="Update started")

    try:
        from src.agents.orchestrator.graph import run_workflow
        state = cast(dict[str, Any], run_workflow(
        repo_url=project.github_url,
        thread_id=str(new_run.id),
        on_node_progress=_publish_node_progress(str(new_run.id)),
        is_update=True,
        existing_deployment={
            "region": existing["region"],
            "ecs_cluster": existing["ecs_cluster"],
            "service_name": existing["service_name"],
        },
        previous_monthly_cost_usd=previous_monthly_cost_usd,
        commit_sha="HEAD",
        ))
        state["job_id"] = str(new_run.id)
    except Exception as exc:
        logger.error(f"run_workflow failed for update {new_run.id}: {exc}", exc_info=True)
        state = {"status": "failed", "error": str(exc), "job_id": str(new_run.id)}

    new_run = update_run_state(db, str(new_run.id), state)
    _publish(state, 30, f"Orchestrator status: {state.get('status')}")

    gate = _current_gate(state)
    if gate:
        publish_gate(str(new_run.id), gate, "awaiting_approval")

    return {
        "job_id": str(new_run.id),
        "status": new_run.status,
        "orchestrator_status": state.get("status"),
        "gate": gate,
        "mode": report_mode(),
        "state": serialize_state(state),
    }


@router.post("/{job_id}/approve")
def approve_job(
    job_id: str,
    body: ApproveRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    run = _get_owned_run(db, job_id, str(current_user.id))

    try:
        from src.agents.orchestrator.graph import resume_workflow
        resume_data = {
            "approved": body.approved,
            "comment": body.comment,
            "approved_by": body.approved_by or current_user.email,
            "request_regeneration": body.request_regeneration,
        }
        state = resume_workflow(
            thread_id=job_id,
            resume_data=resume_data,
            on_node_progress=_publish_node_progress(job_id),
        )
        # Keep the orchestrator job_id aligned with the DB run id (create_job
        # does the same) so report/SBOM files key to the run the API exposes.
        state["job_id"] = job_id
        final_report = state.get("final_report")
        if isinstance(final_report, dict):
            final_report["download_url"] = f"/api/jobs/{job_id}/report/download"
    except Exception as exc:
        logger.error(f"resume_workflow failed for {job_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Resume failed: {exc}") from exc

    run = update_run_state(db, job_id, dict(state))

    if state.get("status") in ("completed", "rolled_back", "failed", "rejected"):
        # In-process orchestrator: write results synchronously to schema tables (idempotent).
        persist_results(db, job_id, dict(state))
        _publish(dict(state), 100, f"Run finished: {state.get('status')}")
        publish_results_ready(job_id)
    else:
        _publish(dict(state), 60, f"Orchestrator status: {state.get('status')}")
    gate = _current_gate(dict(state))
    if gate:
        publish_gate(job_id, gate, "awaiting_approval")

    return {
        "job_id": job_id,
        "status": run.status,
        "orchestrator_status": state.get("status"),
        "gate": _current_gate(dict(state)),
        "mode": report_mode(),
        "state": serialize_state(dict(state)),
    }


@router.put("/{job_id}/artifacts")
def edit_artifacts(
    job_id: str,
    body: ArtifactsEditRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Apply manual artifact edits to a run paused at Gate 2: writes into BOTH
    ``_deploy_inputs`` (what DeployOps consumes on resume) and the
    ``generated_terraform`` shape the tabs read, rewriting materialized rows with
    an audit trail. Gate 2 only: before it no artifacts exist; after, in flight."""
    if not body.files:
        raise HTTPException(status_code=422, detail="No artifact edits supplied")

    run = _get_owned_run(db, job_id, str(current_user.id))

    state = dict(run.run_metadata or {})  # type: ignore
    gate = _current_gate(state)
    if gate != "gate_2_pre_deployops":
        raise HTTPException(
            status_code=409,
            detail=(
                "Artifacts can only be edited while the run is paused at Gate 2 "
                f"(current gate: {gate or 'not paused'})."
            ),
        )

    # Validate every edit up front so a batch fails atomically -- never partial
    # writes of broken files.
    for edit in body.files:
        if not allowed_file_path(edit.file_path):
            raise HTTPException(
                status_code=422, detail=f"Unsupported artifact: {edit.file_path}"
            )
        error = validate_artifact(edit.file_path, edit.content)
        if error:
            raise HTTPException(status_code=422, detail=f"{edit.file_path}: {error}")

    edited_by = current_user.email
    edited_at = datetime.utcnow()

    _apply_artifact_edits(dict(state), body.files)
    update_run_state(db, job_id, dict(state))

    # Rewrite materialized artifact rows with an audit trail (persist_results
    # deletes/recreates rows at terminal status, so it's visible only while paused).
    db.query(models.TerraformArtifact).filter(models.TerraformArtifact.run_id == job_id).delete()
    edited_paths = {edit.file_path for edit in body.files}
    written = 0
    for file_path, artifact_type, content in _artifact_specs(state):
        db.add(models.TerraformArtifact(
            run_id=job_id,
            artifact_type=artifact_type,
            file_path=file_path,
            content=content,
            edited_by=edited_by if file_path in edited_paths else None,
            edited_at=edited_at if file_path in edited_paths else None,
        ))
        written += 1
    db.commit()

    from ..persistence import derive_results_from_state as _derive

    return {
        "edited": [
            {"file_path": edit.file_path, "edited_by": edited_by, "edited_at": edited_at.isoformat()}
            for edit in body.files
        ],
        "written": written,
        "terraform_artifacts": _derive(state)["terraform_artifacts"],
    }


def _apply_artifact_edits(state: dict, edits: list[ArtifactEditRequest]) -> None:
    """Apply file edits to orchestrator state in place, into both the
    ``_deploy_inputs`` block DeployOps reads and the ``generated_terraform``
    block the tabs read (nested ``files`` shape and mock's flat keys)."""
    infracost = state.setdefault("infracost_result", {})
    deploy_inputs = infracost.setdefault("_deploy_inputs", {})
    artifacts = deploy_inputs.setdefault("artifacts", {})
    terraform = artifacts.setdefault("terraform", {})
    tf_files = terraform.setdefault("files", {})

    generated = infracost.setdefault("generated_terraform", {})
    generated_files = generated["files"] if isinstance(generated.get("files"), dict) else generated

    plural_images = artifacts.get("docker_images")
    if not isinstance(plural_images, list):
        plural_images = None

    for edit in edits:
        file_path = edit.file_path
        content = edit.content
        if file_path in ("main.tf", "variables.tf", "outputs.tf"):
            tf_files[file_path] = content
            generated_files[file_path] = content
            generated_files[file_path.replace(".", "_")] = content
        elif file_path == "Dockerfile":
            artifacts["dockerfile"] = content
            if plural_images is not None:
                _apply_dockerfile_edit(plural_images, ".", content)
        elif file_path.endswith("/Dockerfile") and plural_images is not None:
            context = file_path[:-len("/Dockerfile")]
            _apply_dockerfile_edit(plural_images, context, content)
        elif file_path == "docker-image.json":
            import json as _json

            artifacts["docker_image"] = _json.loads(content)

    # Keep the mock/legacy deployops_result.artifacts copy in sync too.
    deployops = state.get("deployops_result")
    if isinstance(deployops, dict):
        dop_artifacts = deployops.setdefault("artifacts", {})
        dop_tf = dop_artifacts.setdefault("terraform", {}).setdefault("files", {})
        for edit in edits:
            if edit.file_path in ("main.tf", "variables.tf", "outputs.tf"):
                dop_tf[edit.file_path] = edit.content
            elif edit.file_path == "Dockerfile":
                dop_artifacts["dockerfile"] = edit.content
                dop_images = dop_artifacts.get("docker_images")
                if isinstance(dop_images, list):
                    _apply_dockerfile_edit(dop_images, ".", edit.content)
            elif edit.file_path.endswith("/Dockerfile"):
                dop_images = dop_artifacts.get("docker_images")
                if isinstance(dop_images, list):
                    _apply_dockerfile_edit(
                        dop_images, edit.file_path[:-len("/Dockerfile")], edit.content
                    )


def _apply_dockerfile_edit(images: list[dict], context: str, content: str) -> None:
    """Route a Dockerfile edit to the plural image whose build context matches
    ("." for root-level Dockerfiles); falls back to the primary (first) image."""
    for image in images:
        img_context = (image.get("context") or ".").rstrip("/")
        if img_context == context:
            image["dockerfile"] = content
            return
    if images:
        images[0]["dockerfile"] = content


class RollbackRequest(BaseModel):
    reason: str = "Manual rollback requested by user"
    target_revision: int | None = None


@router.post("/{job_id}/rollback")
def rollback_job(
    job_id: str,
    body: RollbackRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Roll back a run's most recent deployment to a previous ECS
    task-definition revision via DeployOpsAgent, using the recorded AWS config."""
    run = _get_owned_run(db, job_id, str(current_user.id))
    project = run.project

    # Targeting comes from the newest project deployment whose record actually
    # carries usable ECS info — a failed update run's empty record must not
    # block rolling back the live service (same rule as trigger_update).
    candidates = (
        db.query(models.Deployment)
        .join(models.AnalysisRun, models.Deployment.run_id == models.AnalysisRun.id)
        .filter(
            models.AnalysisRun.project_id == project.id,
            models.Deployment.status.in_(["succeeded", "rolled_back"]),
        )
        .order_by(models.Deployment.created_at.desc())
        .all()
    )
    deployment = None
    resolved: dict = {}
    for cand in candidates:
        cand_cfg = _extract_aws_config(cand)
        if cand_cfg.get("ecs_cluster") and cand_cfg.get("service_name"):
            deployment = cand
            resolved = cand_cfg
            break
    if not deployment:
        raise HTTPException(status_code=404, detail="No deployment found for this job")

    region = resolved.get("region") or "us-east-1"
    ecs_cluster = resolved.get("ecs_cluster")
    service_name = resolved.get("service_name")

    if not ecs_cluster or not service_name:
        raise HTTPException(
            status_code=400,
            detail="Deployment has no ECS cluster/service info to roll back",
        )

    try:
        from src.agents.orchestrator.agent_adapters import use_real_deployops

        if not use_real_deployops():
            logger.info(f"[{job_id}] Rollback: MOCK mode (DEVGUARD_REAL_DEPLOYOPS=1 for real)")
            result = {
                "status": "success",
                "job_id": job_id,
                "message": "Mock rollback: would have rolled back to previous ECS task definition",
                "task_definition": f"{ecs_cluster}-{service_name}:previous",
            }
        else:
            from src.agents.deployops.agent import DeployOpsAgent

            agent = DeployOpsAgent()
            result = asyncio.run(
                agent.rollback_deployment(
                    ecs_cluster=ecs_cluster,
                    service_name=service_name,
                    target_revision=body.target_revision,
                    region=region,
                    reason=body.reason,
                )
            )
    except Exception as exc:
        logger.error(f"Rollback failed for {job_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Rollback failed: {exc}") from exc

    if result.get("status") == "success":
        deployment.status = "rolled_back"  # type: ignore[assignment]
        deployment.rollback_reason = body.reason  # type: ignore[assignment]
        db.commit()
        if run.status not in ("completed", "failed", "rejected"):
            run.status = "rolled_back"  # type: ignore[assignment]
            run.completed_at = datetime.utcnow()  # type: ignore[assignment]
            db.commit()
        publish_results_ready(job_id)
    else:
        raise HTTPException(status_code=500, detail=result.get("error", "Rollback failed"))

    return {
        "job_id": job_id,
        "status": "rolled_back",
        "result": result,
    }


class DestroyRequest(BaseModel):
    confirm_service_name: str


def _extract_aws_config(deployment: models.Deployment) -> dict:
    """Resolve ECS cluster/service/region from deployment record."""
    infra = deployment.infrastructure_json or {}
    aws_config = infra.get("aws_config") or {}
    region = aws_config.get("region") or deployment.aws_region or "us-east-1"
    ecs_cluster = aws_config.get("ecs_cluster")
    service_name = aws_config.get("service_name")
    cluster_was_guessed = False

    if not ecs_cluster or not service_name:
        tf_outputs = infra.get("terraform_outputs") or {}
        service_name = service_name or tf_outputs.get("service_name")
        ecs_cluster = ecs_cluster or tf_outputs.get("ecs_cluster_name")
        if not ecs_cluster and service_name and "-" in service_name:
            suffix = service_name.rsplit("-", 1)[-1]
            ecs_cluster = f"devguard-cluster-{suffix}"
            cluster_was_guessed = True

    return {
        "region": region,
        "ecs_cluster": ecs_cluster,
        "service_name": service_name,
        # True only when ecs_cluster came from the suffix-derivation workaround,
        # not stored data; mutators (update) should refuse, read-only callers may ignore.
        "cluster_was_guessed": cluster_was_guessed,
    }


@router.post("/{job_id}/destroy")
def destroy_job(
    job_id: str,
    body: DestroyRequest,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Destroy a job's deployed infrastructure. Requires typing the exact
    service_name as confirmation (feature/destroy-deployment decision #2) --
    enforced server-side, not just a disabled UI button."""
    run = _get_owned_run(db, job_id, str(current_user.id))

    deployment = (
        db.query(models.Deployment)
        .filter(models.Deployment.run_id == job_id)
        .order_by(models.Deployment.created_at.desc())
        .first()
    )
    if not deployment:
        raise HTTPException(status_code=404, detail="No deployment found for this job")

    aws_config = _extract_aws_config(deployment)
    service_name = aws_config.get("service_name")

    if not service_name:
        raise HTTPException(
            status_code=400,
            detail="Deployment has no service_name on record; cannot confirm destroy target.",
        )

    if body.confirm_service_name != service_name:
        raise HTTPException(
            status_code=400,
            detail=f"Confirmation text does not match. Expected the service name '{service_name}'.",
        )

    try:
        from src.agents.deployops.agent import DeployOpsAgent

        agent = DeployOpsAgent()
        result = asyncio.run(agent.destroy_deployment(job_id=job_id, aws_config=aws_config))
    except Exception as exc:
        logger.error(f"Destroy failed for {job_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Destroy failed: {exc}") from exc

    status = result.get("status")
    if status == "success":
        deployment.status = "destroyed"  # type: ignore[assignment]
        db.commit()
        if run.status not in ("completed", "failed", "rejected"):
            run.status = "destroyed"  # type: ignore[assignment]
            run.completed_at = datetime.utcnow()  # type: ignore[assignment]
            db.commit()
        publish_results_ready(job_id)
        return {"job_id": job_id, "status": "destroyed", "result": result}

    # no_state/failed/partial_failure: don't mutate deployment.status -- resources
    # may still be live; remaining_resources shows UI what remains (destroy-decision #3).
    return {"job_id": job_id, "status": status, "result": result}


@router.delete("/{job_id}", status_code=200)
def delete_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Permanently remove a run and all its database history (deployments,
    findings, cost estimates, artifacts, reports). This is the UX complement
    to ``POST /{job_id}/destroy``: destroy tears down infrastructure but
    keeps the record; delete removes the record itself.

    Guard: runs whose latest deployment still tracks live infrastructure
    (succeeded / applying / rolled_back) are refused — destroy first, or AWS
    resources would outlive their only record."""
    run = _get_owned_run(db, job_id, str(current_user.id))

    deployment = (
        db.query(models.Deployment)
        .filter(models.Deployment.run_id == job_id)
        .order_by(models.Deployment.created_at.desc())
        .first()
    )
    if deployment and deployment.status in ("succeeded", "applying", "rolled_back"):
        raise HTTPException(
            status_code=409,
            detail=(
                "This job has a live deployment on record — destroy the "
                "infrastructure first, then delete the job."
            ),
        )

    db.delete(run)  # children cascade (ondelete=CASCADE + sqlite pragma)

    # Best-effort: drop the LangGraph checkpoint thread so paused/resumed
    # workflows don't leave orphan state in the checkpoints store.
    try:
        import sqlite3 as _sqlite3

        ckpt_path = Path("orchestrator_checkpoints.sqlite")
        if ckpt_path.exists():
            con = _sqlite3.connect(ckpt_path)
            con.execute("DELETE FROM checkpoints WHERE thread_id = ?", (job_id,))
            con.execute("DELETE FROM writes WHERE thread_id = ?", (job_id,))
            con.commit()
            con.close()
    except Exception as exc:  # noqa: BLE001 - checkpoint cleanup is best-effort
        logger.warning(f"Checkpoint cleanup for {job_id} failed: {exc}")

    db.commit()
    logger.info(f"Deleted job {job_id} and its history")
    return {"job_id": job_id, "deleted": True}


@router.get("/{job_id}/deployments/revisions")
def list_deployment_revisions(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    _get_owned_run(db, job_id, str(current_user.id))

    deployment = (
        db.query(models.Deployment)
        .filter(models.Deployment.run_id == job_id)
        .order_by(models.Deployment.created_at.desc())
        .first()
    )
    if not deployment:
        raise HTTPException(status_code=404, detail="No deployment found for this job")

    resolved = _extract_aws_config(deployment)
    region = resolved.get("region") or "us-east-1"
    ecs_cluster = resolved.get("ecs_cluster")
    service_name = resolved.get("service_name")

    if not ecs_cluster or not service_name:
        raise HTTPException(
            status_code=400,
            detail="Deployment has no ECS cluster/service info to list versions for",
        )

    try:
        from src.agents.orchestrator.agent_adapters import use_real_deployops

        if not use_real_deployops():
            logger.info(f"[{job_id}] List revisions: MOCK mode (DEVGUARD_REAL_DEPLOYOPS=1 for real)")
            result = {
                "status": "success",
                "service": f"{ecs_cluster}-dev-{service_name}",
                "versions": [
                    {
                        "task_definition_arn": f"arn:aws:ecs:us-east-1:000000000000:task-definition/{ecs_cluster}-{service_name}:3",
                        "family": f"{ecs_cluster}-{service_name}",
                        "revision": 3,
                        "is_current": True,
                    },
                    {
                        "task_definition_arn": f"arn:aws:ecs:us-east-1:000000000000:task-definition/{ecs_cluster}-{service_name}:2",
                        "family": f"{ecs_cluster}-{service_name}",
                        "revision": 2,
                        "is_current": False,
                    },
                    {
                        "task_definition_arn": f"arn:aws:ecs:us-east-1:000000000000:task-definition/{ecs_cluster}-{service_name}:1",
                        "family": f"{ecs_cluster}-{service_name}",
                        "revision": 1,
                        "is_current": False,
                    },
                ],
            }
        else:
            from src.agents.deployops.agent import DeployOpsAgent

            agent = DeployOpsAgent()
            result = asyncio.run(
                agent.list_revisions(
                    app_name=ecs_cluster,
                    environment="dev",
                    service_name=service_name,
                    region=region,
                )
            )
    except Exception as exc:
        logger.error(f"List revisions failed for {job_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to list revisions: {exc}") from exc

    if result.get("status") != "success":
        raise HTTPException(status_code=500, detail=result.get("error", "Failed to list revisions"))

    return {
        "job_id": job_id,
        "service": result.get("service"),
        "versions": result.get("versions", []),
    }


@router.get("/{job_id}/results")
def get_job_results(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    run = _get_owned_run(db, job_id, str(current_user.id))

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

    # In-flight/gate-paused runs have no materialized rows yet; derive live views.
    live = derive_results_from_state(dict(run.run_metadata or {}))  # type: ignore
    if not findings and live["codesec_findings"]:
        findings = live["codesec_findings"]
    if not estimates and live["infracost_estimates"]:
        estimates = live["infracost_estimates"]
    if not artifacts and live["terraform_artifacts"]:
        artifacts = live["terraform_artifacts"]

    return {
        "job_id": job_id,
        "status": run.status,
        "agent_tasks": agent_tasks,
        "codesec_findings": findings,
        "infracost_estimates": estimates,
        "terraform_artifacts": artifacts,
        "deployments": deployments,
    }


@router.get("/{job_id}/sbom/download")
def download_sbom(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    run = _get_owned_run(db, job_id, str(current_user.id))

    sbom = (run.run_metadata or {}).get("codesec_result", {}).get("sbom")
    if not sbom:
        raise HTTPException(status_code=404, detail="No SBOM generated for this job")

    payload = {k: v for k, v in sbom.items() if k != "download_url"}
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="sbom-{job_id}.json"'},
    )


@router.get("/{job_id}/report/download")
def download_report(
    job_id: str,
    file_format: str = "html",
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    """Serve the final HTML/PDF report for a job, regenerating it on demand."""
    run = _get_owned_run(db, job_id, str(current_user.id))

    from src.agents.orchestrator.report import DEFAULT_REPORT_DIR, generate_report

    report_dir = Path(DEFAULT_REPORT_DIR)
    html_path = report_dir / f"report-{job_id}.html"
    pdf_path = report_dir / f"report-{job_id}.pdf"

    if file_format == "pdf":
        if not pdf_path.is_file():
            raise HTTPException(status_code=404, detail="PDF not available for this report")
        return FileResponse(pdf_path, media_type="application/pdf", filename=f"report-{job_id}.pdf")

    if not html_path.is_file():
        # Reports key on job_id (normalized to the DB run id), so regenerate if missing.
        try:
            generate_report(dict(run.run_metadata or {}), output_dir=report_dir, want_pdf=False)  # type: ignore
        except Exception as exc:
            logger.error(f"Report regeneration failed for {job_id}: {exc}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Report generation failed: {exc}") from exc

    if not html_path.is_file():
        raise HTTPException(status_code=404, detail="Report file not found")

    return FileResponse(html_path, media_type="text/html", filename=f"report-{job_id}.html")


@router.get("/{job_id}")
def get_job(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    run = _get_owned_run(db, job_id, str(current_user.id))

    state: dict[str, Any] = dict(run.run_metadata or {})  # type: ignore[call-overload,arg-type,assignment]
    return {
        "job_id": str(run.id),
        "status": run.status,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
        "duration_seconds": run.duration_seconds,
        "orchestrator_status": state.get("status"),
        "gate": _current_gate(dict(state)),
        "mode": report_mode(),
        "state": state,
    }


@router.get("/")
@router.get("")
def list_jobs(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
):
    runs = (
        db.query(models.AnalysisRun)
        .filter(models.AnalysisRun.triggered_by == str(current_user.id))
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
    """Return the gate the run is paused at, if any: ``__interrupt__`` payload
    first (LangGraph's paused-gate signal), then the status string, then human_gates."""
    interrupts = state.get("__interrupt__")
    if isinstance(interrupts, list) and interrupts:
        entry = interrupts[0]
        # graph.py normalizes Interrupts to unwrapped {"gate": ..., "context": ...} dicts;
        # mirror RunDetailPage.tsx interruptInfo(), which handles both shapes for old runs.
        if isinstance(entry, dict) and "value" in entry:
            value = entry.get("value", {})
        elif isinstance(entry, dict):
            value = entry
        else:
            value = getattr(entry, "value", {})
        if isinstance(value, dict):
            gate = value.get("gate")
            if gate:
                return gate
    status = state.get("status")
    if status and status.startswith("awaiting_approval_gate"):
        return status
    # Fallback: human_gates, so artifact-edit validation agrees with the frontend.
    for gate_name in ("gate_1_pre_infracost", "gate_2_pre_deployops"):
        gate = (state.get("human_gates") or {}).get(gate_name)
        if isinstance(gate, dict) and gate.get("required") and gate.get("approved") is None:
            return gate_name
    return None
