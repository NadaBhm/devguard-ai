import asyncio
import logging
from typing import Optional

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from .api import admin, alerts, auth, chat, deployments, jobs, notifications
from .database import init_db
from .websocket import redis_progress_relay, websocket_endpoint

logger = logging.getLogger(__name__)

app = FastAPI(title="DevGuard AI", version="0.1.0")

_relay_task: Optional[asyncio.Task] = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    # Create tables for local dev (Postgres migrations run via alembic in prod).
    init_db()

    # Build the shared orchestrator graph ONCE so the MemorySaver checkpoints
    # survive across run_workflow / resume_workflow calls in this process.
    try:
        from src.agents.orchestrator.graph import get_orchestrator_graph
        get_orchestrator_graph()
        logger.info("Orchestrator graph built at startup")
    except Exception as exc:
        logger.warning(f"Orchestrator graph unavailable at startup: {exc}")

    # Redis progress relay -> WebSocket clients.
    global _relay_task
    try:
        _relay_task = asyncio.create_task(redis_progress_relay())
    except RuntimeError:
        logger.warning("No running event loop; progress relay started lazily")


@app.on_event("shutdown")
def on_shutdown():
    global _relay_task
    if _relay_task is not None:
        _relay_task.cancel()
        _relay_task = None
    # FIX: the SQLAlchemy engine was never disposed on shutdown. On Linux
    # this went unnoticed (the OS releases the file handle once the process
    # exits regardless), but on Windows the sqlite file stays locked as long
    # as the engine's connection pool is alive - test_jobs.py's teardown
    # fixture (TEST_DB.unlink() right after the TestClient context manager
    # closes, which triggers this very shutdown event) failed with
    # PermissionError [WinError 32] because of exactly this. Disposing the
    # engine here releases the file handle for real, on every platform.
    from .database import engine
    engine.dispose()


# REST API routes
app.include_router(auth.router, prefix="/api")
app.include_router(chat.router, prefix="/api")

#main functionalities are found here
app.include_router(jobs.router, prefix="/api")

app.include_router(notifications.router, prefix="/api")
app.include_router(alerts.router, prefix="/api")
app.include_router(deployments.router, prefix="/api")
app.include_router(admin.router, prefix="/api")

# WebSocket endpoint for real-time progress + RAG chat
@app.websocket("/ws/jobs/{job_id}")
async def ws_jobs(websocket: WebSocket, job_id: str):
    await websocket_endpoint(websocket, job_id)

@app.get("/")
def root():
    return {"message": "DevGuard AI API"}

@app.get("/health")
def health():
    return {"status": "healthy"}
