from datetime import datetime
from pydantic import BaseModel

class UserBase(BaseModel):
    email: str

class UserCreate(UserBase):
    password: str
    first_name: str | None = None
    last_name: str | None = None

class User(UserBase):
    id: int
    is_verified: bool
    first_name: str | None = None
    last_name: str | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        orm_mode = True