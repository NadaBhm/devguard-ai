# src/backend/redis_client.py
import json
from typing import Optional

import redis

from .config import settings


class RedisClient:
    _instance: Optional[redis.Redis] = None
    
    @classmethod 
    def get_client(cls) -> redis.Redis:
        if cls._instance is None:
            cls._instance = redis.from_url(
                settings.REDIS_URL,  
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5
            )
        return cls._instance
    
    @classmethod
    def ping(cls) -> bool:
        try:
            cls.get_client().ping()
            return True
        except Exception:
            return False

# Convenience function
def get_redis():
    return RedisClient.get_client()

# Progress pub/sub: the API publishes job events on channel "progress:{job_id}";
# the WebSocket relay subscribes and forwards them to connected clients.

PROGRESS_CHANNEL = "progress:{job_id}"


def publish_event(job_id: str, event_type: str, **payload) -> bool:
    """Publish a typed live event for a job. Safe no-op if Redis is down.

    Event types: progress, phase, gate, results_ready, error, ...
    """
    try:
        message = json.dumps({
            "type": event_type,
            "job_id": job_id,
            **payload,
        })
        RedisClient.get_client().publish(PROGRESS_CHANNEL.format(job_id=job_id), message)
        return True
    except Exception:
        return False


def publish_progress(job_id: str, phase: str, progress: int, message: str = "") -> bool:
    """Publish a progress event for a job (backwards-compatible)."""
    return publish_event(job_id, "progress", phase=phase, progress=progress, message=message)


def publish_gate(job_id: str, gate: str, status: str) -> bool:
    """Publish a human-gate event: a run is waiting on approval (or was approved)."""
    return publish_event(job_id, "gate", gate=gate, status=status)


def publish_results_ready(job_id: str) -> bool:
    """Signal that a run's results were persisted and are available via /results."""
    return publish_event(job_id, "results_ready")