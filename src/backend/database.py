from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base
from .config import settings

engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {}
)

# sqlite ignores FK constraints (and their ON DELETE CASCADE clauses) unless
# this pragma is set per connection — without it db.delete(run) strands child
# rows (deployments, findings, estimates...) forever.
if "sqlite" in settings.DATABASE_URL:
    @event.listens_for(engine, "connect")
    def _sqlite_fk_pragma(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    # models register their tables on models.Base (a separate declarative_base
    # instance from the one above); create those tables for local SQLite dev.
    from . import models
    models.Base.metadata.create_all(bind=engine)