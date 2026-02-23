"""
Tests for the BingGroundingService.

All Azure SDK calls are mocked — no real API calls are made.
"""

import json
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from services.bing_grounding_service import BingGroundingService


# ── Response parsing ────────────────────────────────────────

class TestParseAgentResponse:
    """Tests for _parse_agent_response — the JSON extraction from agent output."""

    def _service(self):
        return BingGroundingService(
            project_endpoint="https://fake.endpoint",
            bing_connection_name="fake-connection",
        )

    def test_parse_clean_json(self):
        svc = self._service()
        raw = json.dumps({
            "length_mm": 4500,
            "width_mm": 1800,
            "height_mm": 1450,
            "wheelbase_mm": 2700,
            "kerb_weight_kg": 1400,
            "gross_weight_kg": 1900,
            "width_with_mirrors_mm": 2050,
            "vehicle_name_found": "Test Car",
            "confidence": 0.95,
            "search_sources": ["https://example.com"],
        })
        result = svc._parse_agent_response(raw)
        assert result is not None
        assert result["length_mm"] == 4500
        assert result["confidence"] == 0.95

    def test_parse_json_in_markdown_code_block(self):
        svc = self._service()
        raw = '```json\n{"length_mm": 4200, "width_mm": 1750}\n```'
        result = svc._parse_agent_response(raw)
        assert result is not None
        assert result["length_mm"] == 4200

    def test_parse_json_in_plain_code_block(self):
        svc = self._service()
        raw = '```\n{"length_mm": 4200}\n```'
        result = svc._parse_agent_response(raw)
        assert result is not None
        assert result["length_mm"] == 4200

    def test_parse_json_with_surrounding_text(self):
        svc = self._service()
        raw = 'Here are the specs:\n{"length_mm": 4100, "width_mm": 1700}\nHope that helps!'
        result = svc._parse_agent_response(raw)
        assert result is not None
        assert result["length_mm"] == 4100

    def test_parse_empty_response(self):
        svc = self._service()
        assert svc._parse_agent_response("") is None
        assert svc._parse_agent_response(None) is None

    def test_parse_invalid_json(self):
        svc = self._service()
        result = svc._parse_agent_response("This is not JSON at all.")
        assert result is None

    def test_parse_non_object_json(self):
        svc = self._service()
        result = svc._parse_agent_response("[1, 2, 3]")
        assert result is None

    def test_parse_partial_fields(self):
        svc = self._service()
        raw = json.dumps({"length_mm": 4000, "kerb_weight_kg": 1200})
        result = svc._parse_agent_response(raw)
        assert result is not None
        assert result["length_mm"] == 4000
        assert result["width_mm"] is None
        assert result["kerb_weight_kg"] == 1200


# ── Configuration ───────────────────────────────────────────

class TestBingServiceConfig:

    def test_is_configured_true(self):
        svc = BingGroundingService(
            project_endpoint="https://fake.endpoint",
            bing_connection_name="connection-1",
        )
        assert svc.is_configured is True

    def test_is_configured_false_no_endpoint(self):
        svc = BingGroundingService(
            project_endpoint=None,
            bing_connection_name="connection-1",
        )
        assert svc.is_configured is False

    def test_is_configured_false_no_connection(self):
        svc = BingGroundingService(
            project_endpoint="https://fake.endpoint",
            bing_connection_name=None,
        )
        assert svc.is_configured is False

    def test_metrics_initial(self):
        svc = BingGroundingService(
            project_endpoint="https://fake.endpoint",
            bing_connection_name="conn",
        )
        m = svc.metrics
        assert m["total_requests"] == 0
        assert m["successful"] == 0
        assert m["failed"] == 0

    def test_search_returns_empty_when_not_configured(self):
        svc = BingGroundingService(
            project_endpoint=None,
            bing_connection_name=None,
        )
        result = svc.search_vehicle("Ford", "Focus")
        assert result == {}


# ── Prompt building ─────────────────────────────────────────

class TestBuildSearchPrompt:

    def test_prompt_contains_make_model(self):
        svc = BingGroundingService(
            project_endpoint="https://fake.endpoint",
            bing_connection_name="conn",
        )
        prompt = svc._build_search_prompt("Ford", "Focus")
        assert "Ford" in prompt
        assert "Focus" in prompt

    def test_prompt_contains_year_when_given(self):
        svc = BingGroundingService(
            project_endpoint="https://fake.endpoint",
            bing_connection_name="conn",
        )
        prompt = svc._build_search_prompt("BMW", "3 Series", 2021)
        assert "2021" in prompt

    def test_prompt_no_year_when_none(self):
        svc = BingGroundingService(
            project_endpoint="https://fake.endpoint",
            bing_connection_name="conn",
        )
        prompt = svc._build_search_prompt("Toyota", "Corolla")
        assert "Model year" not in prompt


# ── Retryable error detection ──────────────────────────────

class TestRetryableErrors:

    def test_retryable_status_failed(self):
        assert BingGroundingService._is_retryable_error("failed", "") is True

    def test_retryable_status_expired(self):
        assert BingGroundingService._is_retryable_error("expired", "") is True

    def test_retryable_429_in_error(self):
        assert BingGroundingService._is_retryable_error("", "rate_limit 429") is True

    def test_non_retryable_status(self):
        assert BingGroundingService._is_retryable_error("completed", "") is False

    def test_retryable_exception_timeout(self):
        assert BingGroundingService._is_retryable_exception(TimeoutError("timed out")) is True

    def test_retryable_exception_connection(self):
        assert BingGroundingService._is_retryable_exception(ConnectionError("refused")) is True

    def test_non_retryable_exception(self):
        assert BingGroundingService._is_retryable_exception(ValueError("bad value")) is False
