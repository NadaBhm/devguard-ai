from celery import Task
from sqlalchemy.orm import Session
from ..database import SessionLocal

class DatabaseTask(Task):
    """Base task class with automatic database session management(close session after task execution)"""
    _db: Session = None
    
    @property
    def db(self) -> Session:
        if self._db is None:
            self._db = SessionLocal()
        return self._db
    
    def after_return(self, status, retval, task_id, args, kwargs, einfo):
        if self._db is not None:
            self._db.close()
            self._db = None