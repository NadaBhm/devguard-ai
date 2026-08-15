import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from . import auth, crud, models
from .database import SessionLocal

logger = logging.getLogger(__name__)

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, set[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, job_id: str):
        await websocket.accept()
        if job_id not in self.active_connections:
            self.active_connections[job_id] = set()
        self.active_connections[job_id].add(websocket)

    def disconnect(self, websocket: WebSocket, job_id: str):
        if job_id in self.active_connections:
            self.active_connections[job_id].discard(websocket)
            if not self.active_connections[job_id]:
                del self.active_connections[job_id]

    async def send_progress(self, job_id: str, phase: str, progress: int, message: str = ""):
        if job_id not in self.active_connections:
            return
        payload = {
            "type": "progress",
            "job_id": job_id,
            "phase": phase,
            "progress": progress,
            "message": message,
        }
        disconnected = set()
        for ws in self.active_connections[job_id]:
            try:
                await ws.send_text(json.dumps(payload))
            except Exception:
                disconnected.add(ws)
        for ws in disconnected:
            self.disconnect(ws, job_id)

    async def send_event(self, job_id: str, event_type: str, data: dict):
        if job_id not in self.active_connections:
            return
        payload = {
            "type": event_type,
            "job_id": job_id,
            **data,
        }
        disconnected = set()
        for ws in self.active_connections[job_id]:
            try:
                await ws.send_text(json.dumps(payload))
            except Exception:
                disconnected.add(ws)
        for ws in disconnected:
            self.disconnect(ws, job_id)

    async def send_rag_answer(self, job_id: str, answer: str, sources):
        """Send a RAG answer; sources may be a str or list."""
        if job_id not in self.active_connections:
            return

        if isinstance(sources, str):
            sources = [sources]
        elif not isinstance(sources, list):
            sources = []

        payload = {
            "type": "rag_answer",
            "job_id": job_id,
            "answer": answer,
            "sources": sources,
        }
        disconnected = set()
        for ws in self.active_connections[job_id]:
            try:
                await ws.send_text(json.dumps(payload))
            except Exception:
                disconnected.add(ws)
        for ws in disconnected:
            self.disconnect(ws, job_id)


manager = ConnectionManager()


async def redis_progress_relay():
    """
    Background task (started at app startup) that subscribes to the Redis
    progress channel pattern and forwards every message to the WebSocket
    ConnectionManager, so progress published by the API reaches clients.
    """
    from .config import settings

    try:
        import redis.asyncio as aioredis
    except Exception as exc:  # pragma: no cover - redis missing
        logger.warning(f"Redis async client unavailable, progress relay disabled: {exc}")
        return

    client = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    while True:
        try:
            pubsub = client.pubsub()
            await pubsub.psubscribe("progress:*")
            async for message in pubsub.listen():
                if message.get("type") != "pmessage":
                    continue
                try:
                    payload = json.loads(message["data"])
                    event_type = payload.pop("type", "progress")
                    job_id = payload.pop("job_id", "")
                    if event_type == "progress":
                        await manager.send_progress(
                            job_id,
                            payload.get("phase", "unknown"),
                            payload.get("progress", 0),
                            payload.get("message", ""),
                        )
                    else:
                        await manager.send_event(job_id, event_type, payload)
                except Exception:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - reconnect on failure
            logger.warning(f"Progress relay disconnected: {exc}, reconnecting in 3s")
            await asyncio.sleep(3)


def _authenticate(token: str | None):
    if not token:
        return None
    payload = auth.decode_token(token)
    if not payload or payload.get("type") == "refresh":
        return None
    email = payload.get("sub")
    if not email:
        return None
    db = SessionLocal()
    try:
        return crud.get_user_by_email(db, email=email)
    finally:
        db.close()


def _owns_job(user, job_id: str) -> bool:
    db = SessionLocal()
    try:
        run = (
            db.query(models.AnalysisRun)
            .filter(
                models.AnalysisRun.id == job_id,
                models.AnalysisRun.triggered_by == user.id,
            )
            .first()
        )
        return run is not None
    finally:
        db.close()


async def websocket_endpoint(websocket: WebSocket, job_id: str):
    """Real-time job progress + RAG chat. Requires ``?token=<JWT>`` in the
    connection URL."""
    user = _authenticate(websocket.query_params.get("token"))
    if user is None:
        await websocket.close(code=4401, reason="Unauthorized")
        return
    if not _owns_job(user, job_id):
        await websocket.close(code=4403, reason="Forbidden")
        return
    await manager.connect(websocket, job_id)
    try:
        while True:
            data = await websocket.receive_text()
            message = json.loads(data)

            action = message.get("action")

            if action == "rag_query":
                query = message.get("query", "")
                from src.lib.rag.retrieval import ask_repo
                result = ask_repo(query, job_id)

                if isinstance(result, tuple):
                    answer, sources = result
                else:
                    answer = result
                    sources = []

                await manager.send_rag_answer(job_id, answer, sources)

            elif action == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))

            else:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": f"Unknown action: {action}"
                }))

    except WebSocketDisconnect:
        manager.disconnect(websocket, job_id)
