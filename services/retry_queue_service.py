"""
Retry queue service for failed vehicle lookups.

Automatically queues failed/not_found lookups and supports manual or automatic retries
with configurable attempts, backoff, and concurrency.

Uses Redis when available (persistent across restarts), falls back to in-memory storage.
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from enum import Enum

logger = logging.getLogger(__name__)


class RetryStatus(str, Enum):
    PENDING = "pending"
    RETRYING = "retrying"
    SUCCEEDED = "succeeded"
    EXHAUSTED = "exhausted"


class RetryItem:
    """A single item in the retry queue"""

    def __init__(
        self,
        vehicle_make: str,
        vehicle_model: str,
        year: Optional[int] = None,
        original_errors: Optional[List[str]] = None,
        original_status: str = "not_found",
        source_batch_id: Optional[str] = None,
    ):
        self.id = str(uuid.uuid4())[:8]
        self.vehicle_make = vehicle_make
        self.vehicle_model = vehicle_model
        self.year = year
        self.original_errors = original_errors or []
        self.original_status = original_status
        self.source_batch_id = source_batch_id
        self.status = RetryStatus.PENDING
        self.attempt_count = 0
        self.max_attempts = 3
        self.created_at = datetime.utcnow().isoformat()
        self.last_attempt_at: Optional[str] = None
        self.next_retry_at: Optional[str] = None
        self.last_errors: List[str] = []
        self.result: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "vehicle_make": self.vehicle_make,
            "vehicle_model": self.vehicle_model,
            "year": self.year,
            "original_errors": self.original_errors,
            "original_status": self.original_status,
            "source_batch_id": self.source_batch_id,
            "status": self.status.value,
            "attempt_count": self.attempt_count,
            "max_attempts": self.max_attempts,
            "created_at": self.created_at,
            "last_attempt_at": self.last_attempt_at,
            "next_retry_at": self.next_retry_at,
            "last_errors": self.last_errors,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RetryItem":
        item = cls(
            vehicle_make=data["vehicle_make"],
            vehicle_model=data["vehicle_model"],
            year=data.get("year"),
            original_errors=data.get("original_errors", []),
            original_status=data.get("original_status", "not_found"),
            source_batch_id=data.get("source_batch_id"),
        )
        item.id = data["id"]
        item.status = RetryStatus(data.get("status", "pending"))
        item.attempt_count = data.get("attempt_count", 0)
        item.max_attempts = data.get("max_attempts", 3)
        item.created_at = data.get("created_at", datetime.utcnow().isoformat())
        item.last_attempt_at = data.get("last_attempt_at")
        item.next_retry_at = data.get("next_retry_at")
        item.last_errors = data.get("last_errors", [])
        item.result = data.get("result")
        return item


class RetryQueueService:
    """
    Manages a retry queue for failed vehicle lookups.
    """

    REDIS_KEY = "vehicle:retry_queue"
    REDIS_HISTORY_KEY = "vehicle:retry_history"
    HISTORY_MAX = 200

    def __init__(self, redis_client=None, max_attempts: int = 3, backoff_base: float = 30.0):
        self._redis = redis_client
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._queue: Dict[str, RetryItem] = {}
        self._history: List[dict] = []
        self._processing = False

    @property
    def uses_redis(self) -> bool:
        return self._redis is not None

    async def enqueue(
        self,
        vehicle_make: str,
        vehicle_model: str,
        year: Optional[int] = None,
        original_errors: Optional[List[str]] = None,
        original_status: str = "not_found",
        source_batch_id: Optional[str] = None,
    ) -> RetryItem:
        item = RetryItem(
            vehicle_make=vehicle_make,
            vehicle_model=vehicle_model,
            year=year,
            original_errors=original_errors,
            original_status=original_status,
            source_batch_id=source_batch_id,
        )
        item.max_attempts = self._max_attempts
        item.next_retry_at = self._calculate_next_retry(0)
        await self._save_item(item)
        logger.info(f"Retry queue: enqueued '{vehicle_make} {vehicle_model}' (id={item.id})")
        return item

    async def enqueue_batch_failures(
        self, results: list, requests: list, batch_id: Optional[str] = None
    ) -> List[RetryItem]:
        from models import StatusEnum

        enqueued = []
        for req, res in zip(requests, results):
            if res.status in (StatusEnum.NOT_FOUND, StatusEnum.ERROR):
                item = await self.enqueue(
                    vehicle_make=req.make,
                    vehicle_model=req.model,
                    year=req.year,
                    original_errors=res.errors,
                    original_status=res.status.value,
                    source_batch_id=batch_id,
                )
                enqueued.append(item)

        if enqueued:
            logger.info(f"Retry queue: enqueued {len(enqueued)} failures from batch {batch_id or 'unknown'}")
        return enqueued

    async def get_item(self, item_id: str) -> Optional[RetryItem]:
        if self.uses_redis:
            data = await self._redis.hget(self.REDIS_KEY, item_id)
            if data:
                return RetryItem.from_dict(json.loads(data))
            return None
        return self._queue.get(item_id)

    async def get_pending(self) -> List[RetryItem]:
        items = await self._get_all_items()
        return [i for i in items if i.status == RetryStatus.PENDING]

    async def get_all(self) -> List[RetryItem]:
        return await self._get_all_items()

    async def get_history(self) -> List[dict]:
        if self.uses_redis:
            raw = await self._redis.lrange(self.REDIS_HISTORY_KEY, 0, -1)
            return [json.loads(r) for r in raw]
        return list(self._history)

    async def remove_item(self, item_id: str) -> bool:
        if self.uses_redis:
            removed = await self._redis.hdel(self.REDIS_KEY, item_id)
            return removed > 0
        if item_id in self._queue:
            del self._queue[item_id]
            return True
        return False

    async def clear_queue(self) -> int:
        items = await self.get_pending()
        count = 0
        for item in items:
            if await self.remove_item(item.id):
                count += 1
        return count

    async def clear_history(self) -> int:
        if self.uses_redis:
            count = await self._redis.llen(self.REDIS_HISTORY_KEY)
            await self._redis.delete(self.REDIS_HISTORY_KEY)
            return count
        count = len(self._history)
        self._history.clear()
        return count

    async def get_stats(self) -> dict:
        items = await self._get_all_items()
        history = await self.get_history()
        pending = sum(1 for i in items if i.status == RetryStatus.PENDING)
        retrying = sum(1 for i in items if i.status == RetryStatus.RETRYING)
        succeeded = sum(1 for h in history if h.get("status") == RetryStatus.SUCCEEDED.value)
        exhausted = sum(1 for h in history if h.get("status") == RetryStatus.EXHAUSTED.value)
        return {
            "queue_size": len(items),
            "pending": pending,
            "retrying": retrying,
            "history_size": len(history),
            "total_succeeded": succeeded,
            "total_exhausted": exhausted,
            "storage": "redis" if self.uses_redis else "in-memory",
            "max_attempts": self._max_attempts,
            "backoff_base_seconds": self._backoff_base,
            "is_processing": self._processing,
        }

    async def retry_one(self, item_id: str, lookup_service) -> Optional[dict]:
        from models import VehicleSearchRequest, StatusEnum

        item = await self.get_item(item_id)
        if not item:
            return None

        item.status = RetryStatus.RETRYING
        item.attempt_count += 1
        item.last_attempt_at = datetime.utcnow().isoformat()
        await self._save_item(item)

        logger.info(
            f"Retry queue: retrying '{item.vehicle_make} {item.vehicle_model}' "
            f"(attempt {item.attempt_count}/{item.max_attempts}, id={item.id})"
        )

        try:
            request = VehicleSearchRequest(
                make=item.vehicle_make,
                model=item.vehicle_model,
                year=item.year,
            )
            result = await lookup_service.lookup_vehicle(request, use_cache=False)

            if result.status in (StatusEnum.SUCCESS, StatusEnum.PARTIAL):
                item.status = RetryStatus.SUCCEEDED
                item.result = {
                    "status": result.status.value,
                    "length_mm": result.length_mm,
                    "width_mm": result.width_mm,
                    "height_mm": result.height_mm,
                    "kerb_weight_kg": result.kerb_weight_kg,
                    "confidence_score": result.confidence_score,
                }
                item.last_errors = []
                await self._move_to_history(item)
                logger.info(f"Retry queue: '{item.vehicle_make} {item.vehicle_model}' SUCCEEDED")
                return item.to_dict()
            else:
                item.last_errors = result.errors
                if item.attempt_count >= item.max_attempts:
                    item.status = RetryStatus.EXHAUSTED
                    await self._move_to_history(item)
                    logger.warning(f"Retry queue: '{item.vehicle_make} {item.vehicle_model}' EXHAUSTED")
                else:
                    item.status = RetryStatus.PENDING
                    item.next_retry_at = self._calculate_next_retry(item.attempt_count)
                    await self._save_item(item)
                return item.to_dict()

        except Exception as e:
            logger.error(f"Retry queue: error retrying '{item.vehicle_make} {item.vehicle_model}': {e}")
            item.last_errors = [str(e)]
            if item.attempt_count >= item.max_attempts:
                item.status = RetryStatus.EXHAUSTED
                await self._move_to_history(item)
            else:
                item.status = RetryStatus.PENDING
                item.next_retry_at = self._calculate_next_retry(item.attempt_count)
                await self._save_item(item)
            return item.to_dict()

    async def retry_all_pending(self, lookup_service, max_concurrent: int = 5) -> dict:
        if self._processing:
            return {"error": "Retry processing already in progress"}

        self._processing = True
        pending = await self.get_pending()

        if not pending:
            self._processing = False
            return {"processed": 0, "succeeded": 0, "still_failed": 0, "exhausted": 0}

        semaphore = asyncio.Semaphore(max_concurrent)
        results = {"processed": 0, "succeeded": 0, "still_failed": 0, "exhausted": 0}

        async def _process(item: RetryItem):
            async with semaphore:
                result = await self.retry_one(item.id, lookup_service)
                if result:
                    results["processed"] += 1
                    status = result.get("status")
                    if status == RetryStatus.SUCCEEDED.value:
                        results["succeeded"] += 1
                    elif status == RetryStatus.EXHAUSTED.value:
                        results["exhausted"] += 1
                    else:
                        results["still_failed"] += 1

        tasks = [_process(item) for item in pending]
        await asyncio.gather(*tasks, return_exceptions=True)

        self._processing = False
        return results

    def _calculate_next_retry(self, attempt_count: int) -> str:
        delay_seconds = self._backoff_base * (2 ** attempt_count)
        next_time = datetime.utcnow() + timedelta(seconds=delay_seconds)
        return next_time.isoformat()

    async def _save_item(self, item: RetryItem):
        if self.uses_redis:
            await self._redis.hset(self.REDIS_KEY, item.id, json.dumps(item.to_dict()))
        else:
            self._queue[item.id] = item

    async def _get_all_items(self) -> List[RetryItem]:
        if self.uses_redis:
            raw = await self._redis.hgetall(self.REDIS_KEY)
            return [RetryItem.from_dict(json.loads(v)) for v in raw.values()]
        return list(self._queue.values())

    async def _move_to_history(self, item: RetryItem):
        await self.remove_item(item.id)
        history_entry = item.to_dict()
        history_entry["completed_at"] = datetime.utcnow().isoformat()
        if self.uses_redis:
            await self._redis.lpush(self.REDIS_HISTORY_KEY, json.dumps(history_entry))
            await self._redis.ltrim(self.REDIS_HISTORY_KEY, 0, self.HISTORY_MAX - 1)
        else:
            self._history.insert(0, history_entry)
            self._history = self._history[: self.HISTORY_MAX]
