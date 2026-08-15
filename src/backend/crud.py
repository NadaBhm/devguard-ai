from sqlalchemy.orm import Session

from . import models, schemas

# get_password_hash is imported lazily inside each function that needs it to
# avoid a circular import: auth.py imports crud, so crud importing from auth
# at module load time would cycle.


def get_user(db: Session, user_id: str):
    return db.query(models.User).filter(models.User.id == user_id).first()

def get_user_by_email(db: Session, email: str):
    return db.query(models.User).filter(models.User.email == email).first()

def get_user_by_username(db: Session, username: str):
    return db.query(models.User).filter(models.User.username == username).first()

def get_users(db: Session, skip: int = 0, limit: int = 100):
    return db.query(models.User).offset(skip).limit(limit).all()

def create_user(db: Session, user: schemas.UserCreate):
    from .auth import get_password_hash
    hashed_password = get_password_hash(user.password)
    db_user = models.User(
        email=user.email,
        username=user.username or None,
        hashed_password=hashed_password,
        first_name=user.first_name,
        last_name=user.last_name,
        is_verified=True,
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

def update_user(db: Session, user_id: str, user_update: schemas.UserUpdate):
    db_user = get_user(db, user_id)
    if not db_user:
        return None
    update_data = user_update.model_dump(exclude_unset=True)
    if "password" in update_data:
        from .auth import get_password_hash
        update_data["hashed_password"] = get_password_hash(update_data.pop("password"))
    if "username" in update_data:
        username = (update_data.get("username") or "").strip() or None
        update_data["username"] = username
        if username:
            existing = get_user_by_username(db, username)
            if existing and existing.id != user_id:
                from fastapi import HTTPException
                raise HTTPException(status_code=409, detail="Username already taken")
    for field, value in update_data.items():
        setattr(db_user, field, value)
    db.commit()
    db.refresh(db_user)
    return db_user

def delete_user(db: Session, user_id: str):
    db_user = get_user(db, user_id)
    if not db_user:
        return None
    db.delete(db_user)
    db.commit()
    return db_user
