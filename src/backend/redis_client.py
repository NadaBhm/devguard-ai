# src/backend/redis_client.py
import json
import redis
from typing import Optional
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

# Progress pub/sub: the API publishes job progress on channel "progress:{job_id}"; the WebSocket relay subscribes and forwards to connected clients.

PROGRESS_CHANNEL = "progress:{job_id}"


def publish_progress(job_id: str, phase: str, progress: int, message: str = "") -> bool:
    """Publish a progress event for a job. Safe no-op if Redis is down."""
    try:
        payload = json.dumps({
            "type": "progress",
            "job_id": job_id,
            "phase": phase,
            "progress": progress,
            "message": message,
        })
        RedisClient.get_client().publish(PROGRESS_CHANNEL.format(job_id=job_id), payload)
        return True
    except Exception:
        return False