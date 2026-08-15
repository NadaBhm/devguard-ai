from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import auth, models
from ..database import get_db

router = APIRouter(prefix="/deployments", tags=["deployments"])


@router.get("/")
def list_deployments(
    environment: str | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    """List the current user's deployments with run/project context, most recent first."""
    query = (
        db.query(models.Deployment)
        .join(models.AnalysisRun, models.AnalysisRun.id == models.Deployment.run_id)
        .join(models.Project, models.Project.id == models.AnalysisRun.project_id)
        .filter(models.AnalysisRun.triggered_by == current_user.id)
    )
    if environment:
        query = query.filter(models.Deployment.environment == environment)
    deployments = query.order_by(models.Deployment.created_at.desc()).limit(100).all()
    return {
        "deployments": [
            {
                "id": d.id,
                "run_id": d.run_id,
                "project_id": d.run.project_id,
                "repo_name": d.run.project.repo_name if d.run.project else None,
                "repo_url": d.run.project.github_url if d.run.project else None,
                "environment": d.environment,
                "aws_region": d.aws_region,
                "terraform_version": d.terraform_version,
                "terraform_state_id": d.terraform_state_id,
                "status": d.status,
                "applied_at": d.applied_at,
                "created_at": d.created_at,
                "rollback_reason": d.rollback_reason,
                "cost_total_monthly": float(d.cost_total_monthly) if d.cost_total_monthly is not None else None,
            }
            for d in deployments
        ]
    }
