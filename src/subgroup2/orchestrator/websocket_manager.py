"""
DevGuard AI - WebSocket Connection Manager
Manages active WebSocket connections per job_id for real-time progress streaming.

Owner: Hbib (Subgroup 2 - Execution & Control)
Sprint: 1 (Foundation & Mock Agents)
CDC Reference: US-1.3.3, T-1.10

CHANGELOG:
- v1.0.0: Initial implementation - connection registry, broadcast, per-job tracking
"""

import logging
from typing import Dict, List
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    Manages WebSocket connections grouped by job_id.

    Allows multiple clients to listen to the same job's progress
    (e.g., user has dashboard open in 2 tabs, or team member joins).
    """

    def __init__(self):
        # job_id -> list of WebSocket connections
        self._connections: Dict[str, List[WebSocket]] = {}
        logger.info("ConnectionManager initialized")

    async def connect(self, websocket: WebSocket, job_id: str) -> None:
        """Accept a new WebSocket connection and register it for a job."""
        await websocket.accept()

        if job_id not in self._connections:
            self._connections[job_id] = []
        self._connections[job_id].append(websocket)

        logger.info(f"WS client connected to job {job_id} | "
                    f"total listeners: {len(self._connections[job_id])}")

    def disconnect(self, websocket: WebSocket, job_id: str) -> None:
        """Remove a WebSocket connection from a job's listener list."""
        if job_id in self._connections:
            try:
                self._connections[job_id].remove(websocket)
                logger.info(f"WS client disconnected from job {job_id} | "
                            f"remaining listeners: {len(self._connections[job_id])}")

                # Clean up empty job entries
                if not self._connections[job_id]:
                    del self._connections[job_id]
                    logger.info(f"Cleaned up empty connection list for job {job_id}")
            except ValueError:
                # WebSocket not in list - already removed or never added
                pass

    async def send_to_job(self, job_id: str, message: dict) -> None:
        """
        Send a JSON message to all clients listening to a specific job.
        Silently skips disconnected clients.
        """
        if job_id not in self._connections:
            return

        dead_connections = []
        for ws in self._connections[job_id]:
            try:
                await ws.send_json(message)
            except Exception as e:
                # Client disconnected without clean close
                logger.warning(f"Failed to send to WS client for job {job_id}: {e}")
                dead_connections.append(ws)

        # Clean up dead connections
        for dead in dead_connections:
            self.disconnect(dead, job_id)

    async def broadcast(self, message: dict) -> None:
        """Send a message to ALL connected clients across all jobs."""
        for job_id in list(self._connections.keys()):
            await self.send_to_job(job_id, message)

    def get_listener_count(self, job_id: str) -> int:
        """Return number of active listeners for a job."""
        return len(self._connections.get(job_id, []))

    def get_all_jobs(self) -> List[str]:
        """Return list of job_ids with active connections."""
        return list(self._connections.keys())


# Global singleton instance - shared across all WS endpoints
manager = ConnectionManager()