"""
Tests for the VehicleLookupService orchestration layer.
"""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock

from models import VehicleSearchRequest, VehicleInfoResponse, StatusEnum, GovDataFields
from services.vehicle_lookup import VehicleLookupService


# ── Helpers ──────────────────────────────────────────────────

def _make_service(cache=None, bing=None, gov=None):
    return VehicleLookupService(
        cache_service=cache,
        bing_grounding_service=bing,
        gov_data_service=gov,
    )


# ── Tests ────────────────────────────────────────────────────

class TestLookupVehicle:
    """Tests for single vehicle lookup."""

    @pytest.mark.asyncio
    async def test_full_success_with_bing_and_gov(
        self, sample_request, mock_cache_service, mock_bing_service, mock_gov_service,
    ):
        """Full lookup returns SUCCESS when Bing provides dimensions+weight and gov data is found."""
        svc = _make_service(mock_cache_service, mock_bing_service, mock_gov_service)
        result = await svc.lookup_vehicle(sample_request)

        assert result.status == StatusEnum.SUCCESS
        assert result.length_mm == 4378
        assert result.width_mm == 1825
        assert result.height_mm == 1471
        assert result.kerb_weight_kg == 1319
        assert result.gov_data is not None
        assert result.gov_data.fuel_type == "Petrol"
        assert result.dimensions_source == "Bing Grounding"

    @pytest.mark.asyncio
    async def test_partial_when_only_gov_data(
        self, sample_request, mock_cache_service, mock_gov_service,
    ):
        """Returns PARTIAL when only gov data is available (no Bing)."""
        svc = _make_service(mock_cache_service, None, mock_gov_service)
        result = await svc.lookup_vehicle(sample_request)

        assert result.status == StatusEnum.PARTIAL
        assert result.gov_data is not None
        assert result.length_mm is None

    @pytest.mark.asyncio
    async def test_partial_when_only_bing_weight(
        self, sample_request, mock_cache_service, mock_gov_service,
    ):
        """Returns PARTIAL when Bing provides only weight (no dimensions)."""
        bing = AsyncMock()
        bing.is_configured = True
        bing.search_vehicle_async = AsyncMock(return_value={
            "kerb_weight_kg": 1319,
            "source": "Bing Grounding",
            "confidence": 0.7,
        })

        gov = MagicMock()
        gov.is_loaded = False

        svc = _make_service(mock_cache_service, bing, gov)
        result = await svc.lookup_vehicle(sample_request)

        assert result.status == StatusEnum.PARTIAL
        assert result.kerb_weight_kg == 1319
        assert result.length_mm is None

    @pytest.mark.asyncio
    async def test_not_found_when_no_data(self, sample_request, mock_cache_service):
        """Returns NOT_FOUND when no services return data."""
        gov = MagicMock()
        gov.is_loaded = True
        gov.lookup = MagicMock(return_value=None)

        bing = AsyncMock()
        bing.is_configured = True
        bing.search_vehicle_async = AsyncMock(return_value=None)

        svc = _make_service(mock_cache_service, bing, gov)
        result = await svc.lookup_vehicle(sample_request)

        assert result.status == StatusEnum.NOT_FOUND
        assert len(result.errors) > 0

    @pytest.mark.asyncio
    async def test_cached_result_returned(
        self, sample_request, mock_cache_service, sample_cached_data,
    ):
        """Returns cached result without calling Bing or gov."""
        mock_cache_service.get_vehicle_lookup = AsyncMock(return_value=sample_cached_data)

        bing = AsyncMock()
        bing.is_configured = True
        gov = MagicMock()
        gov.is_loaded = True

        svc = _make_service(mock_cache_service, bing, gov)
        result = await svc.lookup_vehicle(sample_request)

        assert result.status == StatusEnum.SUCCESS
        assert result.length_mm == 4378
        # Bing should never be called
        bing.search_vehicle_async.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_bypassed_when_disabled(
        self, sample_request, mock_bing_service, mock_gov_service,
    ):
        """Works correctly with no cache service at all."""
        svc = _make_service(None, mock_bing_service, mock_gov_service)
        result = await svc.lookup_vehicle(sample_request)

        assert result.status == StatusEnum.SUCCESS
        assert result.length_mm == 4378

    @pytest.mark.asyncio
    async def test_error_status_on_exception(self, sample_request, mock_cache_service):
        """Returns ERROR status when an exception occurs during lookup."""
        bing = AsyncMock()
        bing.is_configured = True
        bing.search_vehicle_async = AsyncMock(side_effect=Exception("API failure"))

        gov = MagicMock()
        gov.is_loaded = True
        gov.lookup = MagicMock(side_effect=Exception("CSV read error"))

        svc = _make_service(mock_cache_service, bing, gov)
        result = await svc.lookup_vehicle(sample_request)

        assert result.status == StatusEnum.ERROR
        assert any("CSV read error" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_result_cached_on_success(
        self, sample_request, mock_cache_service, mock_bing_service, mock_gov_service,
    ):
        """Successful results are written to cache."""
        svc = _make_service(mock_cache_service, mock_bing_service, mock_gov_service)
        await svc.lookup_vehicle(sample_request)

        mock_cache_service.set_vehicle_lookup.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_bing_service_adds_error(self, sample_request, mock_cache_service, mock_gov_service):
        """When Bing is None, an error about configuration is added."""
        svc = _make_service(mock_cache_service, None, mock_gov_service)
        result = await svc.lookup_vehicle(sample_request)

        assert any("not configured" in e.lower() for e in result.errors)


class TestLookupBatch:
    """Tests for batch vehicle lookup."""

    @pytest.mark.asyncio
    async def test_batch_returns_all_results(
        self, mock_cache_service, mock_bing_service, mock_gov_service,
    ):
        """Batch lookup returns a result for every request."""
        requests = [
            VehicleSearchRequest(make="Ford", model="Focus", year=2020),
            VehicleSearchRequest(make="BMW", model="3 Series", year=2021),
            VehicleSearchRequest(make="Toyota", model="Corolla"),
        ]
        svc = _make_service(mock_cache_service, mock_bing_service, mock_gov_service)
        results = await svc.lookup_batch(requests)

        assert len(results) == 3
        assert all(isinstance(r, VehicleInfoResponse) for r in results)

    @pytest.mark.asyncio
    async def test_batch_handles_individual_failures(self, mock_cache_service, mock_gov_service):
        """Batch lookup still returns results even if some individual lookups fail."""
        call_count = 0

        async def _alternating_bing(make, model, year=None):
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 0:
                return None
            return {
                "length_mm": 4000, "width_mm": 1800, "height_mm": 1400,
                "kerb_weight_kg": 1200, "source": "Bing Grounding", "confidence": 0.8,
            }

        bing = AsyncMock()
        bing.is_configured = True
        bing.search_vehicle_async = _alternating_bing

        requests = [
            VehicleSearchRequest(make="Ford", model="Focus"),
            VehicleSearchRequest(make="BMW", model="X5"),
        ]
        svc = _make_service(mock_cache_service, bing, mock_gov_service)
        results = await svc.lookup_batch(requests)

        assert len(results) == 2


class TestCacheKey:
    """Tests for cache key generation."""

    def test_cache_key_includes_year(self, sample_request):
        svc = _make_service()
        key = svc._build_cache_key(sample_request)
        assert "2020" in key

    def test_cache_key_case_insensitive(self):
        svc = _make_service()
        key1 = svc._build_cache_key(VehicleSearchRequest(make="Ford", model="Focus"))
        key2 = svc._build_cache_key(VehicleSearchRequest(make="FORD", model="FOCUS"))
        assert key1 == key2

    def test_different_models_different_keys(self):
        svc = _make_service()
        key1 = svc._build_cache_key(VehicleSearchRequest(make="Ford", model="Focus"))
        key2 = svc._build_cache_key(VehicleSearchRequest(make="Ford", model="Fiesta"))
        assert key1 != key2
