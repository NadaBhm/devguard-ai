from sqlalchemy.orm import Session
from . import models, schemas

# get_password_hash is imported lazily inside each function that needs it
# (not at module level) to avoid a circular import: auth.py imports crud
# for crud.get_user_by_email, so crud importing from auth at module load
# time creates a cycle -- same pattern already used in api/jobs.py's
# _get_or_create_system_user for the same reason.

def get_user(db: Session, user_id: str):  # FIX: was int, now str
    return db.query(models.User).filter(models.User.id == user_id).first()

def get_user_by_email(db: Session, email: str):
    return db.query(models.User).filter(models.User.email == email).first()

def get_users(db: Session, skip: int = 0, limit: int = 100):
    return db.query(models.User).offset(skip).limit(limit).all()

def create_user(db: Session, user: schemas.UserCreate):
    from .auth import get_password_hash
    hashed_password = get_password_hash(user.password)
    db_user = models.User(
        email=user.email,
        hashed_password=hashed_password,
        first_name=user.first_name,
        last_name=user.last_name,
        is_verified=True,
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

def update_user(db: Session, user_id: str, user_update: schemas.UserUpdate):  # FIX: was int, now str
    db_user = get_user(db, user_id)
    if not db_user:
        return None
    # FIX: Pydantic v2 uses model_dump, not dict
    update_data = user_update.model_dump(exclude_unset=True)
    if "password" in update_data:
        from .auth import get_password_hash
        update_data["hashed_password"] = get_password_hash(update_data.pop("password"))
    for field, value in update_data.items():
        setattr(db_user, field, value)
    db.commit()
    db.refresh(db_user)
    return db_user

def delete_user(db: Session, user_id: str):  # FIX: was int, now str
    db_user = get_user(db, user_id)
    if not db_user:
        return None
    db.delete(db_user)
    db.commit()
    return db_user