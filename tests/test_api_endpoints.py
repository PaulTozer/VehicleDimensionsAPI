"""
Tests for the FastAPI endpoints (main.py).

Uses httpx + FastAPI TestClient with mocked services.
"""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport

from models import VehicleInfoResponse, StatusEnum, GovDataFields
from datetime import datetime


# ── App fixture with mocked lifespan ────────────────────────

@pytest_asyncio.fixture
async def client(mock_cache_service, mock_bing_service, mock_gov_service):
    """Create a test client with mocked services injected into the app."""
    # Patch the services before importing the app
    import main as main_module

    # Store originals
    orig_lookup = main_module.lookup_service
    orig_gov = main_module.gov_service
    orig_cache = main_module.cache_service
    orig_bing = main_module.bing_service
    orig_retry = main_module.retry_queue

    from services.vehicle_lookup import VehicleLookupService
    from services.retry_queue_service import RetryQueueService

    main_module.gov_service = mock_gov_service
    main_module.cache_service = mock_cache_service
    main_module.bing_service = mock_bing_service
    main_module.lookup_service = VehicleLookupService(
        cache_service=mock_cache_service,
        bing_grounding_service=mock_bing_service,
        gov_data_service=mock_gov_service,
    )
    main_module.retry_queue = RetryQueueService(
        redis_client=None, max_attempts=3, backoff_base=1.0
    )

    transport = ASGITransport(app=main_module.app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    # Restore
    main_module.lookup_service = orig_lookup
    main_module.gov_service = orig_gov
    main_module.cache_service = orig_cache
    main_module.bing_service = orig_bing
    main_module.retry_queue = orig_retry


# ── Health & Metrics ────────────────────────────────────────

class TestHealthEndpoint:

    @pytest.mark.asyncio
    async def test_health_returns_200(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["version"] == "1.0.0"

    @pytest.mark.asyncio
    async def test_health_reports_ai_configured(self, client):
        data = (await client.get("/health")).json()
        assert data["ai_configured"] is True

    @pytest.mark.asyncio
    async def test_metrics_returns_200(self, client):
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "bing_grounding" in data
        assert "gov_data" in data


# ── Vehicle Lookup ──────────────────────────────────────────

class TestVehicleLookupEndpoint:

    @pytest.mark.asyncio
    async def test_single_lookup_success(self, client):
        resp = await client.post(
            "/api/v1/vehicle/lookup",
            json={"make": "Ford", "model": "Focus", "year": 2020},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        assert data["search_make"] == "Ford"
        assert data["length_mm"] == 4378

    @pytest.mark.asyncio
    async def test_single_lookup_without_year(self, client):
        resp = await client.post(
            "/api/v1/vehicle/lookup",
            json={"make": "Ford", "model": "Focus"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["search_year"] is None

    @pytest.mark.asyncio
    async def test_lookup_validation_no_make(self, client):
        resp = await client.post(
            "/api/v1/vehicle/lookup",
            json={"make": "", "model": "Focus"},
        )
        assert resp.status_code == 422  # Pydantic validation error

    @pytest.mark.asyncio
    async def test_lookup_validation_missing_model(self, client):
        resp = await client.post(
            "/api/v1/vehicle/lookup",
            json={"make": "Ford"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_batch_lookup_success(self, client):
        resp = await client.post(
            "/api/v1/vehicle/batch",
            json={
                "vehicles": [
                    {"make": "Ford", "model": "Focus", "year": 2020},
                    {"make": "BMW", "model": "3 Series", "year": 2021},
                ]
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_requested"] == 2
        assert len(data["results"]) == 2

    @pytest.mark.asyncio
    async def test_batch_empty_returns_422(self, client):
        resp = await client.post(
            "/api/v1/vehicle/batch",
            json={"vehicles": []},
        )
        assert resp.status_code == 422


# ── Gov Data Endpoints ──────────────────────────────────────

class TestGovDataEndpoints:

    @pytest.mark.asyncio
    async def test_gov_stats(self, client):
        resp = await client.get("/api/v1/gov/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["unique_makes"] == 50

    @pytest.mark.asyncio
    async def test_list_makes(self, client):
        resp = await client.get("/api/v1/gov/makes")
        assert resp.status_code == 200
        data = resp.json()
        assert "makes" in data
        assert "FORD" in data["makes"]

    @pytest.mark.asyncio
    async def test_search_makes(self, client):
        resp = await client.get("/api/v1/gov/makes?q=FOR")
        assert resp.status_code == 200
        data = resp.json()
        assert "FORD" in data["makes"]

    @pytest.mark.asyncio
    async def test_list_models(self, client):
        resp = await client.get("/api/v1/gov/models/Ford")
        assert resp.status_code == 200
        data = resp.json()
        assert "models" in data

    @pytest.mark.asyncio
    async def test_gov_lookup(self, client):
        resp = await client.get("/api/v1/gov/lookup/Ford/Focus")
        assert resp.status_code == 200
        data = resp.json()
        assert data["gov_data"]["fuel_type"] == "Petrol"

    @pytest.mark.asyncio
    async def test_gov_lookup_not_found(self, client, mock_gov_service):
        mock_gov_service.lookup = MagicMock(return_value=None)
        resp = await client.get("/api/v1/gov/lookup/ZZZ/Unknown")
        assert resp.status_code == 404


# ── Cache Endpoints ─────────────────────────────────────────

class TestCacheEndpoints:

    @pytest.mark.asyncio
    async def test_cache_stats(self, client):
        resp = await client.get("/cache/stats")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_cache_clear(self, client):
        resp = await client.delete("/cache/clear")
        assert resp.status_code == 200


# ── Retry Queue Endpoints ──────────────────────────────────

class TestRetryQueueEndpoints:

    @pytest.mark.asyncio
    async def test_retry_stats(self, client):
        resp = await client.get("/api/v1/retry/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["storage"] == "in-memory"
        assert data["queue_size"] == 0

    @pytest.mark.asyncio
    async def test_enqueue_and_list(self, client):
        # Enqueue
        resp = await client.post(
            "/api/v1/retry/enqueue",
            json={"make": "Tesla", "model": "Model 3", "year": 2022},
        )
        assert resp.status_code == 200
        item = resp.json()
        assert item["vehicle_make"] == "Tesla"
        assert item["status"] == "pending"

        # List pending
        resp = await client.get("/api/v1/retry/pending")
        assert resp.status_code == 200
        pending = resp.json()
        assert len(pending) == 1

    @pytest.mark.asyncio
    async def test_retry_clear_pending(self, client):
        await client.post(
            "/api/v1/retry/enqueue",
            json={"make": "Audi", "model": "A4"},
        )
        resp = await client.delete("/api/v1/retry/clear/pending")
        assert resp.status_code == 200
        assert resp.json()["cleared"] == 1

    @pytest.mark.asyncio
    async def test_retry_remove_not_found(self, client):
        resp = await client.delete("/api/v1/retry/nonexistent")
        assert resp.status_code == 404
