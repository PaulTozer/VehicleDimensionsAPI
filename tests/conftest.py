"""
Shared fixtures for Vehicle Dimensions API tests.
"""

import os
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure we don't try to connect to real services during tests
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("USE_BING_GROUNDING", "false")
os.environ.setdefault("GOV_DATA_AUTO_DOWNLOAD", "false")

from models import VehicleSearchRequest, VehicleInfoResponse, StatusEnum, GovDataFields


# ── Fixtures ────────────────────────────────────────────────


@pytest.fixture
def sample_request():
    return VehicleSearchRequest(make="Ford", model="Focus", year=2020)


@pytest.fixture
def sample_request_no_year():
    return VehicleSearchRequest(make="BMW", model="3 Series")


@pytest.fixture
def sample_gov_result():
    return {
        "body_type": "Cars",
        "generic_model": "FOCUS",
        "fuel_type": "Petrol",
        "engine_size_cc": 1000,
        "engine_size_band": "1001cc to 1500cc",
        "total_registered": 45000,
        "first_registered_year": 2018,
    }


@pytest.fixture
def sample_bing_result():
    return {
        "length_mm": 4378,
        "width_mm": 1825,
        "width_with_mirrors_mm": 2063,
        "height_mm": 1471,
        "wheelbase_mm": 2700,
        "kerb_weight_kg": 1319,
        "gross_weight_kg": 1845,
        "vehicle_name_found": "Ford Focus 2020",
        "confidence": 0.92,
        "search_sources": ["https://example.com"],
        "source": "Bing Grounding",
    }


@pytest.fixture
def sample_cached_data():
    return {
        "search_make": "Ford",
        "search_model": "Focus",
        "search_year": 2020,
        "length_mm": 4378,
        "width_mm": 1825,
        "width_with_mirrors_mm": 2063,
        "height_mm": 1471,
        "wheelbase_mm": 2700,
        "kerb_weight_kg": 1319,
        "gross_weight_kg": 1845,
        "dimensions_source": "Bing Grounding",
        "weight_source": "Bing Grounding",
        "status": "success",
        "last_checked": "2025-01-01T00:00:00",
        "confidence_score": 0.92,
        "errors": [],
        "_cached_at": "2025-01-01T00:00:00",
        "gov_data": {
            "body_type": "Cars",
            "generic_model": "FOCUS",
            "fuel_type": "Petrol",
            "engine_size_cc": 1000,
            "engine_size_band": "1001cc to 1500cc",
            "total_registered": 45000,
            "first_registered_year": 2018,
        },
    }


@pytest.fixture
def mock_cache_service():
    """A mock CacheService that is connected but returns no cached data by default."""
    cache = AsyncMock()
    cache.is_connected = True
    cache.get_vehicle_lookup = AsyncMock(return_value=None)
    cache.set_vehicle_lookup = AsyncMock(return_value=True)
    cache.connect = AsyncMock(return_value=True)
    cache.disconnect = AsyncMock()
    cache.get_stats = AsyncMock(return_value={"connected": True, "total_vehicle_keys": 0})
    cache.clear_all = AsyncMock(return_value=0)
    return cache


@pytest.fixture
def mock_bing_service(sample_bing_result):
    """A mock BingGroundingService that returns sample data."""
    bing = AsyncMock()
    bing.is_configured = True
    bing.search_vehicle_async = AsyncMock(return_value=sample_bing_result)
    bing.cleanup_async = AsyncMock()
    bing.metrics = {
        "total_requests": 1,
        "successful": 1,
        "failed": 0,
        "retries": 0,
        "success_rate": 100.0,
        "max_concurrent": 15,
        "thread_pool_size": 20,
    }
    return bing


@pytest.fixture
def mock_gov_service(sample_gov_result):
    """A mock GovDataService that returns sample data."""
    gov = MagicMock()
    gov.is_loaded = True
    gov.lookup = MagicMock(return_value=sample_gov_result)
    gov.get_stats = MagicMock(return_value={
        "veh0124_loaded": True,
        "veh0124_rows": 1000,
        "veh0220_loaded": True,
        "veh0220_rows": 500,
        "unique_makes": 50,
        "unique_models": 300,
        "last_refreshed": "2025-01-01T00:00:00Z",
    })
    gov.get_all_makes = MagicMock(return_value=["BMW", "FORD", "TOYOTA"])
    gov.search_makes = MagicMock(return_value=["FORD"])
    gov.search_models = MagicMock(return_value=["FOCUS", "FIESTA", "MONDEO"])
    gov.initialise = AsyncMock(return_value=True)
    return gov
