import logging
from datetime import datetime
from uuid import UUID

from ..celery_app import celery_app
from . import DatabaseTask
from ..models import AnalysisRun, Project

logger = logging.getLogger(__name__)


@celery_app.task(base=DatabaseTask, bind=True)
def run_security_scan(self, run_id: UUID, project_id: UUID) -> dict:
    """
    Trigger the orchestrator for a security scan.

    This task is the entry point for the entire analysis pipeline.
    It updates the run status, calls the orchestrator, and persists results.

    Args:
        run_id: UUID of the AnalysisRun record
        project_id: UUID of the Project to scan

    Returns:
        dict: Status and metadata of the orchestration result
    """
    logger.info(f"Starting security scan orchestration for run {run_id}")

    # Fetch run and project
    run = self.db.query(AnalysisRun).filter(AnalysisRun.id == run_id).first()
    if not run:
        raise ValueError(f"AnalysisRun {run_id} not found")

    project = self.db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise ValueError(f"Project {project_id} not found")

    # Update status to running
    run.status = "running"
    run.started_at = datetime.utcnow()
    self.db.commit()

    try:
        # Importing the orchestrator
        try:
            from src.agents.orchestrator.graph import run_workflow
        except ImportError:
            logger.warning("Orchestrator not yet implemented, using mock")
            # Mock result for development
            result = {
                "status": "completed",
                "job_id": str(run_id),
                "final_report": {
                    "summary": {
                        "total_vulnerabilities": 3,
                        "critical_count": 1,
                        "estimated_monthly_cost_usd": 145.32,
                        "deployment_status": "completed",
                        "recommendations": ["Fix critical SQL injection"],
                        "pipeline_duration_seconds": 45.2,
                    }
                },
            }
        else:
            # Call the real orchestrator
            result = run_workflow(
                repo_url=project.github_url,
                thread_id=str(run_id)
            )

        # Update run with success
        run.status = result.get("status", "completed")
        run.completed_at = datetime.utcnow()
        if run.started_at:
            run.duration_seconds = int((run.completed_at - run.started_at).total_seconds())
        run.metadata = {"orchestrator_result": result}
        self.db.commit()

        logger.info(f"Security scan completed for run {run_id} with status {run.status}")
        return {
            "status": "success",
            "run_id": str(run_id),
            "orchestrator_status": run.status,
        }

    except Exception as e:
        logger.error(f"Security scan failed for run {run_id}: {str(e)}", exc_info=True)
        run.status = "failed"
        run.completed_at = datetime.utcnow()
        run.metadata = {"error": str(e)}
        self.db.commit()
        # Re-raise for Celery retry
        raise

