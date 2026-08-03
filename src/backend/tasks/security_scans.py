import logging
from datetime import datetime, timedelta
from uuid import UUID

from ..celery_app import celery_app
from . import DatabaseTask
from ..models import AnalysisRun, Project
from ..persistence import update_run_state, persist_results
from ..redis_client import publish_progress

logger = logging.getLogger(__name__)


@celery_app.task(base=DatabaseTask, bind=True)
def run_security_scan(self, run_id: UUID, project_id: UUID) -> dict:
    """
    Thin kick-off task for a job.

    It no longer runs the orchestrator (the API drives run_workflow /
    resume_workflow in-process so the MemorySaver checkpoints survive).
    This task only flips the run to "running" and publishes a progress event.
    """
    logger.info(f"Kicking off security scan orchestration for run {run_id}")

    run = self.db.query(AnalysisRun).filter(AnalysisRun.id == str(run_id)).first()
    if not run:
        raise ValueError(f"AnalysisRun {run_id} not found")

    run.status = "running"
    run.started_at = datetime.utcnow()
    self.db.commit()

    publish_progress(
        str(run_id),
        phase="start",
        progress=5,
        message="Job started. Running CodeSec analysis.",
    )

    return {"status": "success", "run_id": str(run_id), "orchestrator_status": run.status}


@celery_app.task(base=DatabaseTask, bind=True)
def persist_run_state(self, run_id: UUID, state: dict) -> dict:
    """Persist coarse status + full orchestrator state JSON (light write)."""
    run = update_run_state(self.db, str(run_id), state)
    return {"status": "success", "run_id": str(run_id), "run_status": run.status}


@celery_app.task(base=DatabaseTask, bind=True)
def persist_run_results(self, run_id: UUID, state: dict) -> dict:
    """Materialize a completed run into the child result tables (heavy write)."""
    written = persist_results(self.db, str(run_id), state)
    return {"status": "success", "run_id": str(run_id), "rows_written": written}


@celery_app.task(base=DatabaseTask, bind=True)
def cleanup_old_results(self, days: int = 30) -> dict:
    """Beat task: purge completed/failed runs older than `days`."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    old = self.db.query(AnalysisRun).filter(
        AnalysisRun.completed_at.isnot(None),
        AnalysisRun.completed_at < cutoff,
    ).all()
    count = len(old)
    for run in old:
        self.db.delete(run)
    self.db.commit()
    logger.info(f"Cleanup removed {count} old runs")
    return {"status": "success", "removed": count}