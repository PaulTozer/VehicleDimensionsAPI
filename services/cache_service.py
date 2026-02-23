"""Redis caching service for vehicle lookups"""

import json
import logging
import hashlib
from typing import Optional, Any
from datetime import datetime

logger = logging.getLogger(__name__)

try:
    import redis.asyncio as redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logger.warning("Redis not installed. Caching will be disabled.")


class CacheService:
    """
    Redis-based caching service for vehicle lookup results.
    
    Caches:
    - Full vehicle lookup results (7 day TTL by default — vehicle specs rarely change)
    - Bing search results (24 hour TTL)
    """
    
    VEHICLE_LOOKUP_TTL = 7 * 24 * 60 * 60  # 7 days
    BING_SEARCH_TTL = 24 * 60 * 60  # 24 hours
    
    def __init__(self, redis_url: str = "redis://localhost:6379"):
        self.redis_url = redis_url
        self._client: Optional[redis.Redis] = None
        self._connected = False
    
    async def connect(self) -> bool:
        if not REDIS_AVAILABLE:
            logger.info("Redis library not available, caching disabled")
            return False
        
        try:
            self._client = redis.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True
            )
            await self._client.ping()
            self._connected = True
            logger.info(f"Connected to Redis at {self.redis_url}")
            return True
        except Exception as e:
            logger.warning(f"Failed to connect to Redis: {e}. Caching disabled.")
            self._connected = False
            return False
    
    async def disconnect(self):
        if self._client:
            await self._client.close()
            self._connected = False
            logger.info("Disconnected from Redis")
    
    @property
    def is_connected(self) -> bool:
        return self._connected and self._client is not None
    
    def _generate_cache_key(self, prefix: str, make: str, model: str, year: Optional[int] = None) -> str:
        key_parts = [make.lower().strip(), model.lower().strip()]
        if year:
            key_parts.append(str(year))
        raw_key = ":".join(key_parts)
        key_hash = hashlib.md5(raw_key.encode()).hexdigest()[:12]
        return f"vehicle:{prefix}:{key_hash}"
    
    async def get_vehicle_lookup(self, make: str, model: str, year: Optional[int] = None) -> Optional[dict]:
        if not self.is_connected:
            return None
        try:
            key = self._generate_cache_key("lookup", make, model, year)
            data = await self._client.get(key)
            if data:
                logger.debug(f"Cache HIT: {make} {model}")
                return json.loads(data)
            logger.debug(f"Cache MISS: {make} {model}")
            return None
        except Exception as e:
            logger.warning(f"Cache read error: {e}")
            return None
    
    async def set_vehicle_lookup(self, make: str, model: str, year: Optional[int], data: dict) -> bool:
        if not self.is_connected:
            return False
        try:
            key = self._generate_cache_key("lookup", make, model, year)
            data["_cached_at"] = datetime.utcnow().isoformat()
            await self._client.setex(key, self.VEHICLE_LOOKUP_TTL, json.dumps(data))
            logger.debug(f"Cache SET: {make} {model}")
            return True
        except Exception as e:
            logger.warning(f"Cache write error: {e}")
            return False
    
    async def get_bing_search(self, make: str, model: str, year: Optional[int] = None) -> Optional[dict]:
        if not self.is_connected:
            return None
        try:
            key = self._generate_cache_key("bing", make, model, year)
            data = await self._client.get(key)
            if data:
                return json.loads(data)
            return None
        except Exception as e:
            logger.warning(f"Cache read error: {e}")
            return None
    
    async def set_bing_search(self, make: str, model: str, year: Optional[int], data: dict) -> bool:
        if not self.is_connected:
            return False
        try:
            key = self._generate_cache_key("bing", make, model, year)
            data["_cached_at"] = datetime.utcnow().isoformat()
            await self._client.setex(key, self.BING_SEARCH_TTL, json.dumps(data))
            return True
        except Exception as e:
            logger.warning(f"Cache write error: {e}")
            return False
    
    async def delete_vehicle_lookup(self, make: str, model: str, year: Optional[int] = None) -> bool:
        if not self.is_connected:
            return False
        try:
            key = self._generate_cache_key("lookup", make, model, year)
            deleted = await self._client.delete(key)
            return deleted > 0
        except Exception as e:
            logger.warning(f"Cache delete error: {e}")
            return False
    
    async def clear_all(self) -> int:
        if not self.is_connected:
            return 0
        try:
            keys = []
            async for key in self._client.scan_iter(match="vehicle:*"):
                keys.append(key)
            if keys:
                deleted = await self._client.delete(*keys)
                return deleted
            return 0
        except Exception as e:
            logger.warning(f"Cache clear error: {e}")
            return 0
    
    async def get_stats(self) -> dict:
        if not self.is_connected:
            return {"connected": False}
        try:
            info = await self._client.info()
            keys = 0
            async for _ in self._client.scan_iter(match="vehicle:*"):
                keys += 1
            return {
                "connected": True,
                "redis_version": info.get("redis_version"),
                "used_memory_human": info.get("used_memory_human"),
                "total_vehicle_keys": keys,
            }
        except Exception as e:
            return {"connected": True, "error": str(e)}
