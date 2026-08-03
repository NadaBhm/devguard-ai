import asyncio
import json
import logging
from fastapi import WebSocket, WebSocketDisconnect
from typing import Dict, Set

logger = logging.getLogger(__name__)

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}

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

    async def send_rag_answer(self, job_id: str, answer: str, sources):
        """Send RAG answer. sources can be str or list."""
        if job_id not in self.active_connections:
            return
        
        # Normalize sources to list[str]
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
    Long-running background task (started at app startup).

    Subscribes to the Redis progress channel pattern and forwards every
    message to the WebSocket ConnectionManager, so progress published by
    the API reaches connected clients.
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
                    await manager.send_progress(
                        payload["job_id"],
                        payload.get("phase", "unknown"),
                        payload.get("progress", 0),
                        payload.get("message", ""),
                    )
                except Exception:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - reconnect on failure
            logger.warning(f"Progress relay disconnected: {exc}, reconnecting in 3s")
            await asyncio.sleep(3)


async def websocket_endpoint(websocket: WebSocket, job_id: str):
    """WebSocket endpoint for real-time job progress + RAG chat."""
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
                
                # Handle both return types
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