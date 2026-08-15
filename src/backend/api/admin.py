from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import auth, crud, models, schemas
from ..database import get_db

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin(
    current_user: models.User = Depends(auth.get_current_active_user),
) -> models.User:
    if current_user.role != schemas.UserRole.ADMIN.value:
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return current_user


@router.get("/users", response_model=list[schemas.User])
def list_users(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    _admin: models.User = Depends(require_admin),
):
    return crud.get_users(db, skip=skip, limit=limit)


@router.put("/users/{user_id}/role", response_model=schemas.User)
def update_user_role(
    user_id: str,
    role: schemas.UserRole,
    db: Session = Depends(get_db),
    _admin: models.User = Depends(require_admin),
):
    user = crud.get_user(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.role = role.value
    db.commit()
    db.refresh(user)
    return user
