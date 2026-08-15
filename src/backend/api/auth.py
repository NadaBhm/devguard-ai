from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from .. import auth, crud, models, schemas
from ..database import get_db
from ..config import settings

router = APIRouter(prefix="/auth", tags=["auth"])

@router.post("/register", response_model=schemas.User)
def register(user: schemas.UserCreate, db: Session = Depends(get_db)):
    db_user = crud.get_user_by_email(db, email=user.email)
    if db_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    if user.username:
        db_user = crud.get_user_by_username(db, username=user.username.strip())
        if db_user:
            raise HTTPException(status_code=400, detail="Username already taken")
    return crud.create_user(db=db, user=user)

@router.post("/login", response_model=schemas.Token)
def login(user_credentials: schemas.UserLogin, db: Session = Depends(get_db)):
    user = crud.get_user_by_email(db, email=user_credentials.email)
    if not user or not auth.verify_password(user_credentials.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = auth.create_access_token(
        data={"sub": user.email}, expires_delta=access_token_expires
    )
    refresh_token = auth.create_refresh_token(data={"sub": user.email})
    return {"access_token": access_token, "refresh_token": refresh_token, "token_type": "bearer"}

@router.post("/refresh", response_model=schemas.Token)
def refresh_token(refresh: schemas.RefreshRequest, db: Session = Depends(get_db)):
    payload = auth.decode_token(refresh.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    email = payload.get("sub")
    user = crud.get_user_by_email(db, email=email)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = auth.create_access_token(
        data={"sub": user.email}, expires_delta=access_token_expires
    )
    new_refresh_token = auth.create_refresh_token(data={"sub": user.email})
    return {"access_token": access_token, "refresh_token": new_refresh_token, "token_type": "bearer"}

@router.get("/me", response_model=schemas.User)
def read_users_me(current_user: schemas.User = Depends(auth.get_current_active_user)):
    return current_user

@router.get("/me/stats", response_model=schemas.UserStats)
def read_users_me_stats(
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user),
):
    from sqlalchemy import func

    total_projects = (
        db.query(func.count(models.Project.id))
        .filter(models.Project.user_id == current_user.id)
        .scalar()
        or 0
    )

    runs_q = db.query(models.AnalysisRun).filter(models.AnalysisRun.triggered_by == current_user.id)
    total_runs = runs_q.count()

    total_findings = (
        db.query(func.count(models.CodeSecFinding.id))
        .join(models.AnalysisRun, models.AnalysisRun.id == models.CodeSecFinding.run_id)
        .filter(models.AnalysisRun.triggered_by == current_user.id)
        .scalar()
        or 0
    )

    total_deployments = (
        db.query(func.count(models.Deployment.id))
        .join(models.AnalysisRun, models.AnalysisRun.id == models.Deployment.run_id)
        .filter(models.AnalysisRun.triggered_by == current_user.id)
        .scalar()
        or 0
    )

    est_monthly_cost = (
        db.query(func.coalesce(func.sum(models.InfracostEstimate.monthly_cost_usd), 0))
        .join(models.AnalysisRun, models.AnalysisRun.id == models.InfracostEstimate.run_id)
        .filter(models.AnalysisRun.triggered_by == current_user.id)
        .scalar()
        or 0
    )

    return {
        "total_projects": total_projects,
        "total_runs": total_runs,
        "total_findings": total_findings,
        "total_deployments": total_deployments,
        "est_monthly_cost": float(est_monthly_cost),
        "member_since": current_user.created_at,
    }

@router.put("/me", response_model=schemas.User)
def update_user_me(
    user_update: schemas.UserUpdate,
    db: Session = Depends(get_db),
    current_user: schemas.User = Depends(auth.get_current_active_user)
):
    updated_user = crud.update_user(db, current_user.id, user_update)
    return updated_user