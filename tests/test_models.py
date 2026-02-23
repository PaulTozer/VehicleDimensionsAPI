"""
Tests for Pydantic models — validation, defaults, serialisation.
"""

import pytest
from pydantic import ValidationError

from models import (
    VehicleSearchRequest,
    VehicleInfoResponse,
    VehicleBatchRequest,
    BatchResponse,
    GovDataFields,
    StatusEnum,
    HealthResponse,
    GovDataStatsResponse,
    RetryItemResponse,
    RetryQueueStatsResponse,
    RetryAllResponse,
)


class TestVehicleSearchRequest:

    def test_valid_request(self):
        req = VehicleSearchRequest(make="Ford", model="Focus", year=2020)
        assert req.make == "Ford"
        assert req.year == 2020

    def test_valid_without_year(self):
        req = VehicleSearchRequest(make="BMW", model="3 Series")
        assert req.year is None

    def test_empty_make_rejected(self):
        with pytest.raises(ValidationError):
            VehicleSearchRequest(make="", model="Focus")

    def test_empty_model_rejected(self):
        with pytest.raises(ValidationError):
            VehicleSearchRequest(make="Ford", model="")

    def test_year_too_low_rejected(self):
        with pytest.raises(ValidationError):
            VehicleSearchRequest(make="Ford", model="Focus", year=1800)

    def test_year_too_high_rejected(self):
        with pytest.raises(ValidationError):
            VehicleSearchRequest(make="Ford", model="Focus", year=2050)


class TestVehicleInfoResponse:

    def test_default_status_is_not_found(self):
        resp = VehicleInfoResponse(search_make="Ford", search_model="Focus")
        assert resp.status == StatusEnum.NOT_FOUND

    def test_all_dimensions_optional(self):
        resp = VehicleInfoResponse(search_make="Ford", search_model="Focus")
        assert resp.length_mm is None
        assert resp.width_mm is None
        assert resp.height_mm is None
        assert resp.kerb_weight_kg is None

    def test_errors_default_empty(self):
        resp = VehicleInfoResponse(search_make="Ford", search_model="Focus")
        assert resp.errors == []

    def test_confidence_score_bounds(self):
        with pytest.raises(ValidationError):
            VehicleInfoResponse(search_make="X", search_model="Y", confidence_score=1.5)
        with pytest.raises(ValidationError):
            VehicleInfoResponse(search_make="X", search_model="Y", confidence_score=-0.1)


class TestVehicleBatchRequest:

    def test_valid_batch(self):
        batch = VehicleBatchRequest(vehicles=[
            VehicleSearchRequest(make="Ford", model="Focus"),
            VehicleSearchRequest(make="BMW", model="X5"),
        ])
        assert len(batch.vehicles) == 2

    def test_empty_batch_rejected(self):
        with pytest.raises(ValidationError):
            VehicleBatchRequest(vehicles=[])


class TestBatchResponse:

    def test_batch_response_fields(self):
        resp = BatchResponse(
            total_requested=2,
            successful=1,
            partial=1,
            failed=0,
            results=[
                VehicleInfoResponse(search_make="A", search_model="B", status=StatusEnum.SUCCESS),
                VehicleInfoResponse(search_make="C", search_model="D", status=StatusEnum.PARTIAL),
            ],
        )
        assert resp.total_requested == 2
        assert resp.successful == 1


class TestGovDataFields:

    def test_all_optional(self):
        fields = GovDataFields()
        assert fields.body_type is None
        assert fields.fuel_type is None


class TestHealthResponse:

    def test_health_response(self):
        hr = HealthResponse(
            status="healthy",
            version="1.0.0",
            ai_provider="Azure",
            ai_configured=True,
        )
        assert hr.status == "healthy"


class TestStatusEnum:

    def test_values(self):
        assert StatusEnum.SUCCESS == "success"
        assert StatusEnum.PARTIAL == "partial"
        assert StatusEnum.NOT_FOUND == "not_found"
        assert StatusEnum.ERROR == "error"
