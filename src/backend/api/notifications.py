from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import auth, models
from ..database import get_db

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/")
def list_notifications(
    unread_only: bool = False,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    query = db.query(models.Notification).filter(
        models.Notification.user_id == current_user.id
    )
    if unread_only:
        query = query.filter(models.Notification.is_read.is_(False))
    notifications = query.order_by(models.Notification.created_at.desc()).limit(100).all()
    return {
        "notifications": [
            {
                "id": n.id,
                "user_id": n.user_id,
                "type": n.type,
                "severity": n.severity,
                "title": n.title,
                "body": n.body,
                "run_id": n.run_id,
                "related_finding_id": n.related_finding_id,
                "is_read": n.is_read,
                "created_at": n.created_at,
                "read_at": None,
                "dismissed_at": None,
            }
            for n in notifications
        ]
    }


@router.put("/{notification_id}/read")
def mark_notification_read(
    notification_id: str,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(auth.get_current_active_user),
):
    notification = (
        db.query(models.Notification)
        .filter(
            models.Notification.id == notification_id,
            models.Notification.user_id == current_user.id,
        )
        .first()
    )
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")
    notification.is_read = True  # type: ignore[reportAttributeAccessIssue]
    db.commit()
    db.refresh(notification)
    return {"id": notification.id, "is_read": notification.is_read}