"""
Tests for the GovDataService — CSV loading, indexing, and lookup logic.

Uses a small in-memory CSV fixture instead of real gov.uk files.
Also tests the dynamic URL-discovery logic that scrapes gov.uk.
"""

import os
import csv
import pytest
import pytest_asyncio
import httpx
from unittest.mock import patch, AsyncMock, MagicMock

from services.gov_data_service import (
    GovDataService,
    _discover_csv_urls,
    _FALLBACK_CSV_URLS,
    _CSV_URL_PATTERNS,
    GOV_CSV_FILES,
)


# ── CSV fixtures ────────────────────────────────────────────

VEH0124_HEADER = ["BodyType", "Make", "GenModel", "Model", "YearFirstUsed", "YearManufacture", "2023", "2024"]
VEH0124_ROWS = [
    ["Cars", "FORD", "FOCUS", "FOCUS 1.0T", "2018", "2018", "5000", "4500"],
    ["Cars", "FORD", "FOCUS", "FOCUS 2.0T", "2019", "2019", "3000", "2800"],
    ["Cars", "BMW", "3 SERIES", "320I", "2020", "2020", "2000", "1800"],
    ["Cars", "TOYOTA", "COROLLA", "COROLLA 1.8", "2019", "2019", "6000", "5500"],
]

VEH0220_HEADER = ["BodyType", "Make", "GenModel", "Model", "Fuel", "EngineSizeSimple", "EngineSizeDesc", "2023", "2024"]
VEH0220_ROWS = [
    ["Cars", "FORD", "FOCUS", "FOCUS 1.0T", "Petrol", "1000", "1001cc to 1500cc", "5000", "4500"],
    ["Cars", "FORD", "FOCUS", "FOCUS 2.0T", "Diesel", "2000", "1501cc to 2000cc", "3000", "2800"],
    ["Cars", "BMW", "3 SERIES", "320I", "Petrol", "2000", "1501cc to 2000cc", "2000", "1800"],
    ["Cars", "TOYOTA", "COROLLA", "COROLLA 1.8", "Hybrid", "1800", "1501cc to 2000cc", "6000", "5500"],
]


@pytest.fixture
def gov_data_dir(tmp_path):
    """Write small test CSV files and return the temp directory path."""
    # VEH0124 A-M
    am_path = tmp_path / "df_VEH0124_AM.csv"
    with open(am_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(VEH0124_HEADER)
        for row in VEH0124_ROWS[:2]:  # Ford rows → A-M
            writer.writerow(row)

    # VEH0124 N-Z
    nz_path = tmp_path / "df_VEH0124_NZ.csv"
    with open(nz_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(VEH0124_HEADER)
        for row in VEH0124_ROWS[2:]:  # BMW, Toyota → N-Z
            writer.writerow(row)

    # VEH0220
    veh0220_path = tmp_path / "df_VEH0220.csv"
    with open(veh0220_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(VEH0220_HEADER)
        for row in VEH0220_ROWS:
            writer.writerow(row)

    return str(tmp_path)


@pytest_asyncio.fixture
async def loaded_gov_service(gov_data_dir):
    """Return a GovDataService loaded with the test CSV data."""
    svc = GovDataService(data_dir=gov_data_dir)
    loaded = await svc.initialise(auto_download=False)
    assert loaded is True
    return svc


# ── Loading tests ───────────────────────────────────────────

class TestGovDataLoading:

    @pytest.mark.asyncio
    async def test_loads_successfully(self, loaded_gov_service):
        assert loaded_gov_service.is_loaded is True

    @pytest.mark.asyncio
    async def test_stats_after_load(self, loaded_gov_service):
        stats = loaded_gov_service.get_stats()
        assert stats["veh0124_loaded"] is True
        assert stats["veh0124_rows"] > 0
        assert stats["veh0220_loaded"] is True
        assert stats["veh0220_rows"] > 0
        assert stats["unique_makes"] >= 3  # FORD, BMW, TOYOTA

    @pytest.mark.asyncio
    async def test_no_csvs_returns_false(self, tmp_path):
        svc = GovDataService(data_dir=str(tmp_path))
        loaded = await svc.initialise(auto_download=False)
        assert loaded is False
        assert svc.is_loaded is False


# ── Lookup tests ────────────────────────────────────────────

class TestGovDataLookup:

    @pytest.mark.asyncio
    async def test_exact_make_model_lookup(self, loaded_gov_service):
        result = loaded_gov_service.lookup("Ford", "Focus")
        assert result is not None
        assert result.get("generic_model") == "FOCUS"

    @pytest.mark.asyncio
    async def test_case_insensitive_lookup(self, loaded_gov_service):
        result = loaded_gov_service.lookup("ford", "focus")
        assert result is not None

    @pytest.mark.asyncio
    async def test_lookup_with_fuel_type(self, loaded_gov_service):
        result = loaded_gov_service.lookup("Toyota", "Corolla")
        assert result is not None
        assert "Hybrid" in result.get("fuel_type", "")

    @pytest.mark.asyncio
    async def test_lookup_unknown_vehicle(self, loaded_gov_service):
        result = loaded_gov_service.lookup("Lamborghini", "Aventador")
        assert result is None

    @pytest.mark.asyncio
    async def test_lookup_returns_engine_size(self, loaded_gov_service):
        result = loaded_gov_service.lookup("BMW", "3 Series")
        assert result is not None
        assert result.get("engine_size_cc") is not None

    @pytest.mark.asyncio
    async def test_lookup_returns_body_type(self, loaded_gov_service):
        result = loaded_gov_service.lookup("Ford", "Focus")
        assert result is not None
        assert result.get("body_type") == "Cars"


# ── Year-filtered lookup tests ──────────────────────────────

class TestGovDataYearFiltering:

    @pytest.mark.asyncio
    async def test_year_filter_narrows_registration_count(self, loaded_gov_service):
        """When year is given, total_registered should be only that year's count."""
        all_years = loaded_gov_service.lookup("Ford", "Focus")
        year_2018 = loaded_gov_service.lookup("Ford", "Focus", year=2018)

        assert all_years is not None
        assert year_2018 is not None
        # Year-filtered count should be less than aggregate
        assert year_2018["total_registered"] < all_years["total_registered"]
        assert year_2018["total_registered"] > 0

    @pytest.mark.asyncio
    async def test_year_filter_sets_first_registered_to_requested_year(self, loaded_gov_service):
        """first_registered_year should match the requested year when data exists."""
        result = loaded_gov_service.lookup("Ford", "Focus", year=2019)
        assert result is not None
        assert result["first_registered_year"] == 2019

    @pytest.mark.asyncio
    async def test_year_filter_match_flag_true(self, loaded_gov_service):
        """year_match should be True when the year exists in data."""
        result = loaded_gov_service.lookup("Ford", "Focus", year=2018)
        assert result is not None
        assert result["year_match"] is True

    @pytest.mark.asyncio
    async def test_year_filter_match_flag_false_for_missing_year(self, loaded_gov_service):
        """year_match should be False when the year doesn't exist in data."""
        result = loaded_gov_service.lookup("Ford", "Focus", year=2000)
        assert result is not None
        assert result["year_match"] is False
        # Falls back to aggregate data
        assert result["first_registered_year"] == 2018  # min of {2018, 2019}

    @pytest.mark.asyncio
    async def test_no_year_gives_aggregate_count(self, loaded_gov_service):
        """Without year, total_registered is the sum across all years."""
        result = loaded_gov_service.lookup("Ford", "Focus")
        assert result is not None
        # Should be sum of both Ford Focus rows: (5000+4500) + (3000+2800) = 15300
        assert result["total_registered"] == 15300

    @pytest.mark.asyncio
    async def test_year_specific_count_is_correct(self, loaded_gov_service):
        """Year 2018 count should be only the rows with YearManufacture=2018."""
        result = loaded_gov_service.lookup("Ford", "Focus", year=2018)
        assert result is not None
        # Only the FOCUS 1.0T row has YearManufacture=2018: 5000+4500 = 9500
        assert result["total_registered"] == 9500

    @pytest.mark.asyncio
    async def test_different_year_different_count(self, loaded_gov_service):
        """Different years should return different counts."""
        result_2018 = loaded_gov_service.lookup("Ford", "Focus", year=2018)
        result_2019 = loaded_gov_service.lookup("Ford", "Focus", year=2019)
        assert result_2018 is not None
        assert result_2019 is not None
        assert result_2018["total_registered"] != result_2019["total_registered"]


# ── Search / autocomplete tests ─────────────────────────────

class TestGovDataSearch:

    @pytest.mark.asyncio
    async def test_get_all_makes(self, loaded_gov_service):
        makes = loaded_gov_service.get_all_makes()
        assert "FORD" in makes
        assert "BMW" in makes
        assert "TOYOTA" in makes

    @pytest.mark.asyncio
    async def test_search_makes(self, loaded_gov_service):
        matches = loaded_gov_service.search_makes("FOR")
        assert "FORD" in matches

    @pytest.mark.asyncio
    async def test_search_makes_no_match(self, loaded_gov_service):
        matches = loaded_gov_service.search_makes("ZZZZZ")
        assert len(matches) == 0

    @pytest.mark.asyncio
    async def test_search_models(self, loaded_gov_service):
        models = loaded_gov_service.search_models("FORD")
        assert len(models) > 0

    @pytest.mark.asyncio
    async def test_search_models_empty_make(self, loaded_gov_service):
        models = loaded_gov_service.search_models("NONEXISTENT")
        assert len(models) == 0


# ── Dynamic URL discovery tests ─────────────────────────────

# Realistic snippet of gov.uk page HTML containing asset URLs
_SAMPLE_GOV_HTML = """
<html><body>
<a href="https://assets.publishing.service.gov.uk/media/aaa111bbb/df_VEH0124_AM.csv">VEH0124 A-M</a>
<a href="https://assets.publishing.service.gov.uk/media/ccc222ddd/df_VEH0124_NZ.csv">VEH0124 N-Z</a>
<a href="https://assets.publishing.service.gov.uk/media/eee333fff/df_VEH0220.csv">VEH0220</a>
</body></html>
"""

_SAMPLE_GOV_HTML_PARTIAL = """
<html><body>
<a href="https://assets.publishing.service.gov.uk/media/aaa111bbb/df_VEH0124_AM.csv">VEH0124 A-M</a>
<!-- VEH0124_NZ and VEH0220 missing from this page -->
</body></html>
"""


class TestDiscoverCsvUrls:

    @pytest.mark.asyncio
    async def test_all_urls_discovered(self):
        """When the page contains all 3 CSV links, they should all be extracted."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = _SAMPLE_GOV_HTML
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("services.gov_data_service.httpx.AsyncClient", return_value=mock_client):
            urls = await _discover_csv_urls()

        assert len(urls) == 3
        assert "aaa111bbb" in urls["veh0124_am"]
        assert "ccc222ddd" in urls["veh0124_nz"]
        assert "eee333fff" in urls["veh0220"]

    @pytest.mark.asyncio
    async def test_partial_urls_returns_found_only(self):
        """When the page has only 1 of 3 links, only that one is returned."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = _SAMPLE_GOV_HTML_PARTIAL
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("services.gov_data_service.httpx.AsyncClient", return_value=mock_client):
            urls = await _discover_csv_urls()

        assert len(urls) == 1
        assert "veh0124_am" in urls

    @pytest.mark.asyncio
    async def test_no_urls_found_returns_fallback(self):
        """When the page contains no matching links, fallback URLs are used."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "<html><body>No CSVs here</body></html>"
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("services.gov_data_service.httpx.AsyncClient", return_value=mock_client):
            urls = await _discover_csv_urls()

        assert urls == _FALLBACK_CSV_URLS

    @pytest.mark.asyncio
    async def test_http_error_returns_fallback(self):
        """When the HTTP request fails, fallback URLs are returned."""
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.HTTPStatusError(
            "503 Service Unavailable", request=MagicMock(), response=MagicMock()
        ))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("services.gov_data_service.httpx.AsyncClient", return_value=mock_client):
            urls = await _discover_csv_urls()

        assert urls == _FALLBACK_CSV_URLS

    @pytest.mark.asyncio
    async def test_network_error_returns_fallback(self):
        """When the network is unreachable, fallback URLs are returned."""
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("DNS resolution failed"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("services.gov_data_service.httpx.AsyncClient", return_value=mock_client):
            urls = await _discover_csv_urls()

        assert urls == _FALLBACK_CSV_URLS


class TestDownloadMissingFilesWithDiscovery:

    @pytest.mark.asyncio
    async def test_skips_download_when_files_exist(self, gov_data_dir):
        """When all CSV files already exist, no HTTP calls are made."""
        svc = GovDataService(data_dir=gov_data_dir)

        with patch("services.gov_data_service._discover_csv_urls") as mock_discover:
            await svc._download_missing_files()
            mock_discover.assert_not_called()

    @pytest.mark.asyncio
    async def test_discovers_urls_for_missing_files(self, tmp_path):
        """When files are missing, it discovers URLs and downloads them."""
        svc = GovDataService(data_dir=str(tmp_path))

        mock_discover = AsyncMock(return_value={
            "veh0124_am": "https://example.com/AM.csv",
            "veh0124_nz": "https://example.com/NZ.csv",
            "veh0220": "https://example.com/0220.csv",
        })

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"header1,header2\nval1,val2\n"
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("services.gov_data_service._discover_csv_urls", mock_discover), \
             patch("services.gov_data_service.httpx.AsyncClient", return_value=mock_client):
            await svc._download_missing_files()

        mock_discover.assert_called_once()
        assert mock_client.get.call_count == 3  # 3 missing files

    @pytest.mark.asyncio
    async def test_downloads_only_missing_files(self, gov_data_dir):
        """When 2 of 3 files exist, only 1 download is attempted."""
        # Remove one file to simulate a partial download
        os.remove(os.path.join(gov_data_dir, "df_VEH0220.csv"))

        svc = GovDataService(data_dir=gov_data_dir)

        mock_discover = AsyncMock(return_value={
            "veh0220": "https://example.com/0220.csv",
        })

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"header1,header2\nval1,val2\n"
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("services.gov_data_service._discover_csv_urls", mock_discover), \
             patch("services.gov_data_service.httpx.AsyncClient", return_value=mock_client):
            await svc._download_missing_files()

        mock_discover.assert_called_once()
        assert mock_client.get.call_count == 1  # only veh0220

    @pytest.mark.asyncio
    async def test_regex_patterns_match_real_format(self):
        """Verify regex patterns match the actual gov.uk asset URL format."""
        real_url_am = "https://assets.publishing.service.gov.uk/media/68ed0befa8398380cb4acfdb/df_VEH0124_AM.csv"
        real_url_nz = "https://assets.publishing.service.gov.uk/media/68ed0bbc82670806f9d5dfe2/df_VEH0124_NZ.csv"
        real_url_0220 = "https://assets.publishing.service.gov.uk/media/68ed09a42adc28a81b4acfec/df_VEH0220.csv"

        assert _CSV_URL_PATTERNS["veh0124_am"].search(real_url_am) is not None
        assert _CSV_URL_PATTERNS["veh0124_nz"].search(real_url_nz) is not None
        assert _CSV_URL_PATTERNS["veh0220"].search(real_url_0220) is not None
