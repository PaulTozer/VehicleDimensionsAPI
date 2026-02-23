"""
Tests for the RetryQueueService (in-memory mode only, no Redis).
"""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock

from models import VehicleSearchRequest, VehicleInfoResponse, StatusEnum
from services.retry_queue_service import RetryQueueService, RetryItem, RetryStatus


@pytest.fixture
def retry_queue():
    return RetryQueueService(redis_client=None, max_attempts=3, backoff_base=1.0)


@pytest.fixture
def mock_lookup_service_success():
    """A lookup service that always returns SUCCESS."""
    svc = AsyncMock()
    svc.lookup_vehicle = AsyncMock(return_value=VehicleInfoResponse(
        search_make="Ford",
        search_model="Focus",
        search_year=2020,
        length_mm=4378,
        width_mm=1825,
        height_mm=1471,
        kerb_weight_kg=1319,
        status=StatusEnum.SUCCESS,
    ))
    return svc


@pytest.fixture
def mock_lookup_service_fail():
    """A lookup service that always returns NOT_FOUND."""
    svc = AsyncMock()
    svc.lookup_vehicle = AsyncMock(return_value=VehicleInfoResponse(
        search_make="Ford",
        search_model="Focus",
        status=StatusEnum.NOT_FOUND,
        errors=["No data found"],
    ))
    return svc


# ── Basic queue operations ──────────────────────────────────

class TestRetryQueueBasics:

    @pytest.mark.asyncio
    async def test_uses_in_memory_storage(self, retry_queue):
        assert retry_queue.uses_redis is False

    @pytest.mark.asyncio
    async def test_enqueue_item(self, retry_queue):
        item = await retry_queue.enqueue("Ford", "Focus", year=2020)
        assert item.vehicle_make == "Ford"
        assert item.vehicle_model == "Focus"
        assert item.status == RetryStatus.PENDING

    @pytest.mark.asyncio
    async def test_get_pending(self, retry_queue):
        await retry_queue.enqueue("Ford", "Focus")
        await retry_queue.enqueue("BMW", "X5")
        pending = await retry_queue.get_pending()
        assert len(pending) == 2

    @pytest.mark.asyncio
    async def test_get_item(self, retry_queue):
        item = await retry_queue.enqueue("Toyota", "Corolla")
        fetched = await retry_queue.get_item(item.id)
        assert fetched is not None
        assert fetched.vehicle_make == "Toyota"

    @pytest.mark.asyncio
    async def test_get_item_not_found(self, retry_queue):
        fetched = await retry_queue.get_item("nonexistent")
        assert fetched is None

    @pytest.mark.asyncio
    async def test_remove_item(self, retry_queue):
        item = await retry_queue.enqueue("Audi", "A4")
        removed = await retry_queue.remove_item(item.id)
        assert removed is True
        pending = await retry_queue.get_pending()
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_remove_nonexistent(self, retry_queue):
        removed = await retry_queue.remove_item("nope")
        assert removed is False

    @pytest.mark.asyncio
    async def test_clear_queue(self, retry_queue):
        await retry_queue.enqueue("A", "B")
        await retry_queue.enqueue("C", "D")
        cleared = await retry_queue.clear_queue()
        assert cleared == 2
        pending = await retry_queue.get_pending()
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_stats(self, retry_queue):
        await retry_queue.enqueue("X", "Y")
        stats = await retry_queue.get_stats()
        assert stats["queue_size"] == 1
        assert stats["pending"] == 1
        assert stats["storage"] == "in-memory"
        assert stats["max_attempts"] == 3


# ── Retry execution ────────────────────────────────────────

class TestRetryExecution:

    @pytest.mark.asyncio
    async def test_retry_one_success(self, retry_queue, mock_lookup_service_success):
        item = await retry_queue.enqueue("Ford", "Focus", year=2020)
        result = await retry_queue.retry_one(item.id, mock_lookup_service_success)
        assert result is not None
        assert result["status"] == "succeeded"

    @pytest.mark.asyncio
    async def test_retry_one_moves_to_history_on_success(self, retry_queue, mock_lookup_service_success):
        item = await retry_queue.enqueue("Ford", "Focus")
        await retry_queue.retry_one(item.id, mock_lookup_service_success)
        pending = await retry_queue.get_pending()
        assert len(pending) == 0
        history = await retry_queue.get_history()
        assert len(history) == 1

    @pytest.mark.asyncio
    async def test_retry_one_fail_stays_pending(self, retry_queue, mock_lookup_service_fail):
        item = await retry_queue.enqueue("Ford", "Focus")
        result = await retry_queue.retry_one(item.id, mock_lookup_service_fail)
        assert result["status"] == "pending"
        pending = await retry_queue.get_pending()
        assert len(pending) == 1

    @pytest.mark.asyncio
    async def test_retry_exhausts_after_max_attempts(self, retry_queue, mock_lookup_service_fail):
        item = await retry_queue.enqueue("Ford", "Focus")
        # Retry 3 times (max_attempts=3)
        for _ in range(3):
            result = await retry_queue.retry_one(item.id, mock_lookup_service_fail)
            if result and result["status"] == "exhausted":
                break
            # Re-fetch item for next iteration if still pending
            item = await retry_queue.get_item(item.id)
            if item is None:
                break

        history = await retry_queue.get_history()
        assert any(h["status"] == "exhausted" for h in history)

    @pytest.mark.asyncio
    async def test_retry_nonexistent_returns_none(self, retry_queue, mock_lookup_service_success):
        result = await retry_queue.retry_one("nonexistent", mock_lookup_service_success)
        assert result is None

    @pytest.mark.asyncio
    async def test_retry_all_pending(self, retry_queue, mock_lookup_service_success):
        await retry_queue.enqueue("Ford", "Focus")
        await retry_queue.enqueue("BMW", "X5")
        result = await retry_queue.retry_all_pending(mock_lookup_service_success, max_concurrent=2)
        assert result["processed"] == 2
        assert result["succeeded"] == 2

    @pytest.mark.asyncio
    async def test_retry_all_empty_queue(self, retry_queue, mock_lookup_service_success):
        result = await retry_queue.retry_all_pending(mock_lookup_service_success)
        assert result["processed"] == 0


# ── RetryItem serialisation ────────────────────────────────

class TestRetryItemSerde:

    def test_to_dict_and_back(self):
        item = RetryItem(
            vehicle_make="Ford",
            vehicle_model="Focus",
            year=2020,
            original_errors=["timeout"],
            original_status="error",
        )
        d = item.to_dict()
        restored = RetryItem.from_dict(d)
        assert restored.vehicle_make == "Ford"
        assert restored.year == 2020
        assert restored.original_errors == ["timeout"]

    def test_to_dict_fields(self):
        item = RetryItem(vehicle_make="BMW", vehicle_model="X5")
        d = item.to_dict()
        assert "id" in d
        assert d["vehicle_make"] == "BMW"
        assert d["status"] == "pending"
        assert d["attempt_count"] == 0
