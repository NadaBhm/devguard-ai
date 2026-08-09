from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import auth, models
from ..database import get_db

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("/")
def list_alerts(
    resolved: bool | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    """List the current user's cost alerts, most recent first.

    Note: cost_alerts records an alert already triggered for a specific
    run (it always carries run_id/actual_cost_usd) -- there is no
    "create a budget rule in advance" concept in the current schema, so
    there is deliberately no POST endpoint here yet. See docs/ for the
    schema this would need if that's wanted later.
    """
    query = db.query(models.CostAlert).filter(models.CostAlert.user_id == current_user.id)
    if resolved is not None:
        query = query.filter(models.CostAlert.is_resolved == resolved)
    alerts = query.order_by(models.CostAlert.created_at.desc()).limit(100).all()
    return {
        "alerts": [
            {
                "id": a.id,
                "run_id": a.run_id,
                "project_id": a.project_id,
                "user_id": a.user_id,
                "alert_type": a.alert_type,
                "threshold_usd": float(a.threshold_usd),
                "actual_cost_usd": float(a.actual_cost_usd),
                "severity": a.severity,
                "is_resolved": a.is_resolved,
                "created_at": a.created_at,
                "resolved_at": a.resolved_at,
            }
            for a in alerts
        ]
    }


@router.put("/{alert_id}/resolve")
def resolve_alert(
    alert_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    """Mark a cost alert as resolved. Only the owning user may do this."""
    from datetime import datetime

    alert = (
        db.query(models.CostAlert)
        .filter(models.CostAlert.id == alert_id, models.CostAlert.user_id == current_user.id)
        .first()
    )
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.is_resolved = True
    alert.resolved_at = datetime.utcnow()
    db.commit()
    db.refresh(alert)
    return {"id": alert.id, "is_resolved": alert.is_resolved, "resolved_at": alert.resolved_at}
