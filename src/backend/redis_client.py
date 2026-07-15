# src/backend/redis_client.py
import redis
from typing import Optional
from .config import settings

class RedisClient:
    _instance: Optional[redis.Redis] = None
    
    @classmethod 
    def get_client(cls) -> redis.Redis:
        if cls._instance is None:
            cls._instance = redis.from_url(
                settings.redis_url,
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