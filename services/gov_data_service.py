"""
UK Government Vehicle Licensing CSV Data Service

Downloads and queries the DfT vehicle licensing statistics CSV files:
- VEH0124: Vehicles by make, model, year of first use, year of manufacture
- VEH0220: Vehicles by make, model, fuel type, engine size

Data source: https://www.gov.uk/government/statistical-data-sets/vehicle-licensing-statistics-data-files
"""

import csv
import io
import logging
import os
import re
import time
from collections import defaultdict
from typing import Optional, Dict, Any, List, Tuple

import httpx

from config import GOV_DATA_DIR, GOV_DATA_AUTO_DOWNLOAD

logger = logging.getLogger(__name__)

# The gov.uk page that lists all CSV download links (link IDs change on each data refresh)
GOV_DATA_PAGE_URL = (
    "https://www.gov.uk/government/statistical-data-sets/"
    "vehicle-licensing-statistics-data-files"
)

# Fallback URLs — used only if the page scrape fails
_FALLBACK_CSV_URLS = {
    "veh0124_am": "https://assets.publishing.service.gov.uk/media/68ed0befa8398380cb4acfdb/df_VEH0124_AM.csv",
    "veh0124_nz": "https://assets.publishing.service.gov.uk/media/68ed0bbc82670806f9d5dfe2/df_VEH0124_NZ.csv",
    "veh0220": "https://assets.publishing.service.gov.uk/media/68ed09a42adc28a81b4acfec/df_VEH0220.csv",
}

# Local file names
GOV_CSV_FILES = {
    "veh0124_am": "df_VEH0124_AM.csv",
    "veh0124_nz": "df_VEH0124_NZ.csv",
    "veh0220": "df_VEH0220.csv",
}

# Regex patterns to extract download URLs from the gov.uk page HTML
# Each pattern matches the assets.publishing URL for the corresponding CSV file
_CSV_URL_PATTERNS: Dict[str, re.Pattern] = {
    "veh0124_am": re.compile(
        r'https://assets\.publishing\.service\.gov\.uk/media/[a-f0-9]+/df_VEH0124_AM\.csv',
        re.IGNORECASE,
    ),
    "veh0124_nz": re.compile(
        r'https://assets\.publishing\.service\.gov\.uk/media/[a-f0-9]+/df_VEH0124_NZ\.csv',
        re.IGNORECASE,
    ),
    "veh0220": re.compile(
        r'https://assets\.publishing\.service\.gov\.uk/media/[a-f0-9]+/df_VEH0220\.csv',
        re.IGNORECASE,
    ),
}


async def _discover_csv_urls() -> Dict[str, str]:
    """
    Scrape the gov.uk data-files page to find the current CSV download URLs.

    Returns a dict mapping file keys (e.g. "veh0124_am") to their download URLs.
    Falls back to hardcoded URLs if the page cannot be fetched or parsed.
    """
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(GOV_DATA_PAGE_URL)
            response.raise_for_status()
            html = response.text

        urls: Dict[str, str] = {}
        for key, pattern in _CSV_URL_PATTERNS.items():
            match = pattern.search(html)
            if match:
                urls[key] = match.group(0)
                logger.info(f"  Discovered {GOV_CSV_FILES[key]} URL: {urls[key]}")
            else:
                logger.warning(f"  Could not find {GOV_CSV_FILES[key]} link on gov.uk page")

        if urls:
            return urls

        logger.warning("No CSV URLs discovered from gov.uk page — using fallback URLs")
        return dict(_FALLBACK_CSV_URLS)

    except Exception as e:
        logger.warning(f"Failed to scrape gov.uk data page: {e} — using fallback URLs")
        return dict(_FALLBACK_CSV_URLS)


class GovDataService:
    """
    Loads, indexes, and queries UK government vehicle registration data.
    
    Provides:
    - Make/model validation against official registrations
    - Fuel type and engine size data from VEH0220
    - Year of manufacture data from VEH0124
    - Registration count statistics
    """
    
    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = data_dir or GOV_DATA_DIR
        
        # In-memory indexed data
        # Key: (make_upper, model_upper) -> aggregated info
        self._veh0124_index: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._veh0220_index: Dict[Tuple[str, str], Dict[str, Any]] = {}
        
        # Unique makes/models for autocomplete
        self._makes: set = set()
        self._models_by_make: Dict[str, set] = defaultdict(set)
        
        self._loaded = False
        self._veh0124_rows = 0
        self._veh0220_rows = 0
        self._last_refreshed: Optional[str] = None
    
    @property
    def is_loaded(self) -> bool:
        return self._loaded
    
    def get_stats(self) -> dict:
        """Return statistics about loaded data"""
        return {
            "veh0124_loaded": self._veh0124_rows > 0,
            "veh0124_rows": self._veh0124_rows,
            "veh0220_loaded": self._veh0220_rows > 0,
            "veh0220_rows": self._veh0220_rows,
            "unique_makes": len(self._makes),
            "unique_models": sum(len(models) for models in self._models_by_make.values()),
            "last_refreshed": self._last_refreshed,
        }
    
    async def initialise(self, auto_download: bool = True) -> bool:
        """
        Load CSV data files. Downloads them first if missing and auto_download is True.
        
        Returns True if at least one dataset was loaded successfully.
        """
        os.makedirs(self.data_dir, exist_ok=True)
        
        if auto_download and GOV_DATA_AUTO_DOWNLOAD:
            await self._download_missing_files()
        
        loaded_any = False
        
        # Load VEH0124 (make, model, year of manufacture)
        veh0124_am_path = os.path.join(self.data_dir, GOV_CSV_FILES["veh0124_am"])
        veh0124_nz_path = os.path.join(self.data_dir, GOV_CSV_FILES["veh0124_nz"])
        
        if os.path.exists(veh0124_am_path):
            rows = self._load_veh0124(veh0124_am_path)
            self._veh0124_rows += rows
            loaded_any = True
            logger.info(f"Loaded VEH0124 A-M: {rows} rows")
        
        if os.path.exists(veh0124_nz_path):
            rows = self._load_veh0124(veh0124_nz_path)
            self._veh0124_rows += rows
            loaded_any = True
            logger.info(f"Loaded VEH0124 N-Z: {rows} rows")
        
        # Load VEH0220 (make, model, fuel, engine size)
        veh0220_path = os.path.join(self.data_dir, GOV_CSV_FILES["veh0220"])
        if os.path.exists(veh0220_path):
            rows = self._load_veh0220(veh0220_path)
            self._veh0220_rows = rows
            loaded_any = True
            logger.info(f"Loaded VEH0220: {rows} rows")
        
        if loaded_any:
            self._loaded = True
            self._last_refreshed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            logger.info(
                f"Gov data loaded: {len(self._makes)} makes, "
                f"{sum(len(m) for m in self._models_by_make.values())} models"
            )
        else:
            logger.warning(
                "No gov CSV data files found. Place CSV files in the data/ directory "
                "or set GOV_DATA_AUTO_DOWNLOAD=true to download automatically."
            )
        
        return loaded_any
    
    async def _download_missing_files(self):
        """Download any missing CSV files from gov.uk, discovering current URLs dynamically."""
        # Check which files are missing before doing any network calls
        missing_keys = []
        for key in GOV_CSV_FILES:
            filepath = os.path.join(self.data_dir, GOV_CSV_FILES[key])
            if os.path.exists(filepath):
                size_mb = os.path.getsize(filepath) / (1024 * 1024)
                logger.info(f"  {GOV_CSV_FILES[key]} exists ({size_mb:.1f} MB), skipping download")
            else:
                missing_keys.append(key)

        if not missing_keys:
            return

        # Discover current download URLs from the gov.uk page
        logger.info("Discovering current CSV download URLs from gov.uk...")
        csv_urls = await _discover_csv_urls()

        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            for key in missing_keys:
                url = csv_urls.get(key)
                if not url:
                    logger.warning(f"  No URL available for {GOV_CSV_FILES[key]}, skipping")
                    continue

                filepath = os.path.join(self.data_dir, GOV_CSV_FILES[key])
                logger.info(f"  Downloading {GOV_CSV_FILES[key]} from gov.uk...")
                try:
                    response = await client.get(url)
                    response.raise_for_status()
                    
                    with open(filepath, "wb") as f:
                        f.write(response.content)
                    
                    size_mb = len(response.content) / (1024 * 1024)
                    logger.info(f"  Downloaded {GOV_CSV_FILES[key]} ({size_mb:.1f} MB)")
                except Exception as e:
                    logger.warning(f"  Failed to download {GOV_CSV_FILES[key]}: {e}")
    
    def _load_veh0124(self, filepath: str) -> int:
        """
        Load VEH0124 CSV (vehicles by make, model, year of manufacture).
        
        Expected columns: BodyType, Make, GenModel, Model, YearFirstUsed, YearManufacture, <year columns>
        Each year column contains vehicle counts.
        
        The index stores both overall totals and per-manufacture-year breakdowns
        so lookups can be filtered by year.
        """
        rows_loaded = 0
        try:
            with open(filepath, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                
                for row in reader:
                    make = row.get("Make", "").strip().upper()
                    model = row.get("Model", "").strip().upper()
                    gen_model = row.get("GenModel", "").strip().upper()
                    body_type = row.get("BodyType", "").strip()
                    year_mfr = row.get("YearManufacture", "").strip()
                    
                    if not make or not model:
                        continue
                    
                    # Track makes and models
                    self._makes.add(make)
                    self._models_by_make[make].add(model)
                    if gen_model:
                        self._models_by_make[make].add(gen_model)
                    
                    key = (make, gen_model if gen_model else model)
                    
                    if key not in self._veh0124_index:
                        self._veh0124_index[key] = {
                            "make": make,
                            "model": model,
                            "generic_model": gen_model,
                            "body_type": body_type,
                            "years_manufactured": set(),
                            "total_count": 0,
                            "by_year": {},  # year -> {body_type, count}
                        }
                    
                    entry = self._veh0124_index[key]
                    
                    # Parse the manufacture year
                    mfr_year = int(year_mfr) if year_mfr and year_mfr.isdigit() else None
                    if mfr_year:
                        entry["years_manufactured"].add(mfr_year)
                    
                    # Sum the count columns (yearly snapshot counts)
                    row_count = 0
                    for col_name, value in row.items():
                        if col_name.startswith("20") and value and value not in ("[c]", "[x]", "[z]"):
                            try:
                                row_count += int(value)
                            except ValueError:
                                pass
                    
                    entry["total_count"] += row_count
                    
                    # Store per-manufacture-year breakdown
                    if mfr_year:
                        if mfr_year not in entry["by_year"]:
                            entry["by_year"][mfr_year] = {
                                "body_types": set(),
                                "count": 0,
                            }
                        yr_entry = entry["by_year"][mfr_year]
                        if body_type:
                            yr_entry["body_types"].add(body_type)
                        yr_entry["count"] += row_count
                    
                    rows_loaded += 1
            
        except Exception as e:
            logger.error(f"Error loading VEH0124 from {filepath}: {e}")
        
        return rows_loaded
    
    def _load_veh0220(self, filepath: str) -> int:
        """
        Load VEH0220 CSV (vehicles by make, model, fuel type, engine size).
        
        Expected columns: BodyType, Make, GenModel, Model, Fuel, EngineSizeSimple, EngineSizeDesc, <year columns>
        
        VEH0220 doesn't have a YearManufacture column, but each row has a
        specific Model variant (e.g. "FIESTA 1.0T") plus fuel + engine.  We
        use the yearly snapshot columns to determine which fuel/engine combos
        are *currently registered* — if a row has a non-zero count in the most
        recent year column, that fuel/engine combination is still on the road.
        """
        rows_loaded = 0
        try:
            with open(filepath, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                
                for row in reader:
                    make = row.get("Make", "").strip().upper()
                    model = row.get("Model", "").strip().upper()
                    gen_model = row.get("GenModel", "").strip().upper()
                    body_type = row.get("BodyType", "").strip()
                    fuel = row.get("Fuel", "").strip()
                    engine_size = row.get("EngineSizeSimple", "").strip()
                    engine_desc = row.get("EngineSizeDesc", "").strip()
                    
                    if not make or not model:
                        continue
                    
                    # Track makes and models
                    self._makes.add(make)
                    self._models_by_make[make].add(model)
                    
                    key = (make, gen_model if gen_model else model)
                    
                    if key not in self._veh0220_index:
                        self._veh0220_index[key] = {
                            "make": make,
                            "model": model,
                            "generic_model": gen_model,
                            "body_type": body_type,
                            "fuel_types": set(),
                            "engine_sizes": set(),
                            "engine_size_bands": set(),
                            "total_count": 0,
                            "by_model_variant": {},  # model_variant -> {fuels, engines, ...}
                        }
                    
                    entry = self._veh0220_index[key]
                    
                    if fuel:
                        entry["fuel_types"].add(fuel)
                    
                    parsed_engine = None
                    if engine_size and engine_size not in ("[x]", "[z]"):
                        try:
                            parsed_engine = int(engine_size)
                            entry["engine_sizes"].add(parsed_engine)
                        except ValueError:
                            pass
                    if engine_desc and engine_desc not in ("[x]", "[z]"):
                        entry["engine_size_bands"].add(engine_desc)
                    
                    # Sum yearly counts
                    row_count = 0
                    for col_name, value in row.items():
                        if col_name.startswith("20") and value and value not in ("[c]", "[x]", "[z]"):
                            try:
                                row_count += int(value)
                            except ValueError:
                                pass
                    entry["total_count"] += row_count
                    
                    # Track per-model-variant data (e.g. "FIESTA 1.0T" -> Petrol, 1000cc)
                    if model not in entry["by_model_variant"]:
                        entry["by_model_variant"][model] = {
                            "fuel_types": set(),
                            "engine_sizes": set(),
                            "engine_size_bands": set(),
                            "count": 0,
                        }
                    variant = entry["by_model_variant"][model]
                    if fuel:
                        variant["fuel_types"].add(fuel)
                    if parsed_engine:
                        variant["engine_sizes"].add(parsed_engine)
                    if engine_desc and engine_desc not in ("[x]", "[z]"):
                        variant["engine_size_bands"].add(engine_desc)
                    variant["count"] += row_count
                    
                    rows_loaded += 1
            
        except Exception as e:
            logger.error(f"Error loading VEH0220 from {filepath}: {e}")
        
        return rows_loaded
    
    def lookup(self, make: str, model: str, year: Optional[int] = None,
               model_variant: Optional[str] = None,
               fuel_type: Optional[str] = None,
               engine_capacity_cc: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        Look up a vehicle in the gov data by make and model.
        
        When *model_variant* is supplied (e.g. "1.0T EcoBoost", "ST-3", "320d"),
        the service tries to match it against the detailed Model variants stored
        in VEH0220's by_model_variant index.  This allows returning variant-
        specific fuel type, engine size, and registration count.
        
        When *fuel_type* is supplied (e.g. "Diesel", "Petrol"), the VEH0220
        results are filtered to only include data for that fuel type —
        fuel_type, engine_size, available_variants are all scoped to matching
        rows so the response is specific to the known vehicle.
        
        When *engine_capacity_cc* is supplied (e.g. 1560), the VEH0220 results
        are further filtered to only include variants whose engine_sizes match
        the rounded-up EngineSizeSimple value (e.g. 1560 → 1600).
        
        Returns gov data fields if found, None otherwise.
        Tries exact match first, then generic model match, then fuzzy.
        """
        make_upper = make.strip().upper()
        model_upper = model.strip().upper()
        
        # Strategy 1: Exact match on (make, model)
        result = self._try_lookup(make_upper, model_upper, year, model_variant, fuel_type, engine_capacity_cc)
        if result:
            return result
        
        # Strategy 2: Try matching just using the first word of model (generic model)
        model_first_word = model_upper.split()[0] if model_upper else ""
        if model_first_word and model_first_word != model_upper:
            result = self._try_lookup(make_upper, model_first_word, year, model_variant, fuel_type, engine_capacity_cc)
            if result:
                return result
        
        # Strategy 3: Check if any indexed model starts with or contains the search model
        for indexed_key in list(self._veh0124_index.keys()) + list(self._veh0220_index.keys()):
            if indexed_key[0] == make_upper:
                indexed_model = indexed_key[1]
                if model_upper in indexed_model or indexed_model in model_upper:
                    result = self._try_lookup(make_upper, indexed_model, year, model_variant, fuel_type, engine_capacity_cc)
                    if result:
                        return result
        
        return None
    
    def _try_lookup(self, make: str, model: str, year: Optional[int] = None,
                    model_variant: Optional[str] = None,
                    fuel_type: Optional[str] = None,
                    engine_capacity_cc: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Try to find data for a specific make/model key.
        
        When *year* is supplied the result is scoped to that manufacture year:
        - body_type comes from only that year's registrations
        - total_registered is the count for that year only
        - first_registered_year is the requested year (confirmed in data)
        - fuel_type / engine_size are filtered where possible
        
        When *model_variant* is supplied (e.g. "1.0T", "ST-3", "320D") the
        service fuzzy-matches it against the detailed Model variants in VEH0220
        and returns variant-specific fuel/engine data plus a list of other
        available variants so the caller knows what options exist.
        
        When *fuel_type* is supplied (e.g. "DIESEL") the VEH0220 results are
        filtered so that only variants with that fuel type are included in
        fuel_type, engine_size, and available_variants.
        
        When *engine_capacity_cc* is supplied (e.g. 1560) the VEH0220 results
        are further filtered to only include variants whose engine_sizes set
        contains the matching EngineSizeSimple value (rounded up to nearest 100).
        
        When no year is given the result is the aggregate across all years.
        """
        key = (make, model)
        
        result = {}
        
        # ── VEH0124 ──────────────────────────────────────────────
        if key in self._veh0124_index:
            entry = self._veh0124_index[key]
            result["generic_model"] = entry.get("generic_model")
            
            all_years = entry.get("years_manufactured", set())
            by_year = entry.get("by_year", {})
            
            if year and year in by_year:
                # Year-specific data
                yr = by_year[year]
                body_types = yr.get("body_types", set())
                result["body_type"] = ", ".join(sorted(body_types)) if body_types else entry.get("body_type")
                result["total_registered"] = yr.get("count", 0)
                result["first_registered_year"] = year
                result["year_match"] = True
            elif year and all_years:
                # Year requested but not in data — fall back to aggregate
                # but signal the mismatch
                result["body_type"] = entry.get("body_type")
                result["total_registered"] = entry.get("total_count", 0)
                result["first_registered_year"] = min(all_years)
                result["year_match"] = False
            else:
                # No year filter — aggregate
                result["body_type"] = entry.get("body_type")
                result["total_registered"] = entry.get("total_count", 0)
                if all_years:
                    result["first_registered_year"] = min(all_years)
                result["year_match"] = True
        
        # ── VEH0220 ──────────────────────────────────────────────
        if key in self._veh0220_index:
            entry = self._veh0220_index[key]
            if not result.get("body_type"):
                result["body_type"] = entry.get("body_type")
            if not result.get("generic_model"):
                result["generic_model"] = entry.get("generic_model")
            
            by_variant = entry.get("by_model_variant", {})
            
            # ── Fuel-type filtering ────────────────────────────
            # When a fuel_type is known (e.g. from DVLA/MOT), narrow the
            # VEH0220 data to only include variants that match that fuel.
            fuel_filter_upper = fuel_type.strip().upper() if fuel_type else None

            # ── Engine-size filtering ─────────────────────────
            # VEH0220 EngineSizeSimple rounds up to the nearest 100cc.
            # e.g. 1560cc → 1600, 999cc → 1000, 1000cc → 1000.
            engine_simple = None
            if engine_capacity_cc is not None and engine_capacity_cc > 0:
                import math
                engine_simple = int(math.ceil(engine_capacity_cc / 100.0) * 100)

            # Collect all known variant names for this make/model,
            # optionally filtered by fuel type and engine size.
            def _variant_matches(vname: str, vdata: dict) -> bool:
                if fuel_filter_upper:
                    vf = {f.upper() for f in vdata.get("fuel_types", set())}
                    if fuel_filter_upper not in vf:
                        return False
                if engine_simple is not None:
                    ve = vdata.get("engine_sizes", set())
                    if ve and engine_simple not in ve:
                        return False
                return True

            if (fuel_filter_upper or engine_simple is not None) and by_variant:
                filtered_variant_names = sorted(
                    vname for vname, vdata in by_variant.items()
                    if _variant_matches(vname, vdata)
                )
            else:
                filtered_variant_names = sorted(by_variant.keys())

            all_variant_names = filtered_variant_names
            if len(all_variant_names) > 1:
                result["available_variants"] = all_variant_names
            
            # ── Variant matching ─────────────────────────────────
            matched_variant_key = None
            if model_variant:
                variant_upper = model_variant.strip().upper()
                matched_variant_key = self._match_variant(
                    variant_upper, model, all_variant_names
                )
            
            if matched_variant_key and matched_variant_key in by_variant:
                # Use variant-specific fuel/engine data
                vdata = by_variant[matched_variant_key]
                result["matched_variant"] = matched_variant_key
                
                vfuels = vdata.get("fuel_types", set())
                if fuel_filter_upper:
                    vfuels = {f for f in vfuels if f.upper() == fuel_filter_upper}
                if vfuels:
                    result["fuel_type"] = ", ".join(sorted(vfuels))
                
                vengines = vdata.get("engine_sizes", set())
                if engine_simple is not None and engine_simple in vengines:
                    result["engine_size_cc"] = engine_simple
                elif vengines:
                    result["engine_size_cc"] = max(vengines)
                
                vbands = vdata.get("engine_size_bands", set())
                if engine_simple is not None and vbands:
                    result["engine_size_band"] = self._match_engine_band(engine_simple, vbands)
                elif vbands:
                    result["engine_size_band"] = self._match_engine_band(None, vbands)
                
                result["variant_registered"] = vdata.get("count", 0)
            else:
                # No variant match — use aggregate fuel/engine data
                if fuel_filter_upper or engine_simple is not None:
                    # Aggregate only from variants matching the filters
                    agg_fuels: set = set()
                    agg_engines: set = set()
                    agg_bands: set = set()
                    agg_count = 0
                    for vname in all_variant_names:
                        vdata = by_variant.get(vname, {})
                        vf = vdata.get("fuel_types", set())
                        ve = vdata.get("engine_sizes", set())
                        
                        if fuel_filter_upper and fuel_filter_upper not in {f.upper() for f in vf}:
                            continue
                        if engine_simple is not None and ve and engine_simple not in ve:
                            continue
                        
                        if fuel_filter_upper:
                            agg_fuels |= {f for f in vf if f.upper() == fuel_filter_upper}
                        else:
                            agg_fuels |= vf
                        if engine_simple is not None:
                            agg_engines.add(engine_simple)
                        else:
                            agg_engines |= ve
                        agg_bands |= vdata.get("engine_size_bands", set())
                        agg_count += vdata.get("count", 0)
                    if agg_fuels:
                        result["fuel_type"] = ", ".join(sorted(agg_fuels))
                    if engine_simple is not None:
                        result["engine_size_cc"] = engine_simple
                    elif agg_engines:
                        result["engine_size_cc"] = max(agg_engines)
                    if agg_bands:
                        result["engine_size_band"] = self._match_engine_band(engine_simple, agg_bands)
                    if agg_count:
                        result["total_registered"] = agg_count
                else:
                    # No fuel filter — use full aggregate
                    fuel_types = entry.get("fuel_types", set())
                    if fuel_types:
                        result["fuel_type"] = ", ".join(sorted(fuel_types))
                    
                    engine_sizes = entry.get("engine_sizes", set())
                    if engine_sizes:
                        result["engine_size_cc"] = max(engine_sizes)
                    
                    engine_bands = entry.get("engine_size_bands", set())
                    if engine_bands:
                        result["engine_size_band"] = self._match_engine_band(None, engine_bands)
            
            if not result.get("total_registered"):
                result["total_registered"] = entry.get("total_count", 0)
        
        return result if result else None
    
    @staticmethod
    def _match_engine_band(
        engine_simple: Optional[int],
        bands: set,
    ) -> Optional[str]:
        """
        Pick the engine size band that matches the given EngineSizeSimple value.

        VEH0220 bands look like "1501cc to 1600cc", "Up to 100cc", "Over 15000cc".
        When *engine_simple* is provided (e.g. 1600), find the band whose upper
        bound matches.  Otherwise return the most common (largest count) or
        the numerically highest band.
        """
        if not bands:
            return None

        if engine_simple is not None:
            # Try to find the band whose range includes engine_simple
            target = str(engine_simple)
            for band in bands:
                # "1501cc to 1600cc" — check if target appears as the upper bound
                if f"to {target}cc" in band:
                    return band
                # "Up to 100cc" — for engine_simple == 100
                if band.startswith("Up to") and target + "cc" in band:
                    return band
                # "Over 15000cc" — for engine_simple >= 15000
                if band.startswith("Over") and engine_simple >= 15000:
                    return band

        # Fallback: sort bands by parsing their upper numeric bound
        def _upper_bound(band: str) -> int:
            import re as _re
            nums = _re.findall(r'\d+', band)
            return max(int(n) for n in nums) if nums else 0

        return max(bands, key=_upper_bound)

    @staticmethod
    def _match_variant(
        variant_query: str,
        base_model: str,
        variant_names: List[str],
    ) -> Optional[str]:
        """
        Fuzzy-match a user-supplied variant string against known gov data
        variant names.
        
        Matching strategies (in order):
        1. Exact match: query equals the variant name (or the variant minus the
           base model prefix).
        2. Contains: query appears as a substring of a variant name.
        3. Token overlap: pick the variant with the most overlapping words.
        
        Returns the best-matching variant key, or None.
        """
        if not variant_names:
            return None
        
        query_tokens = set(variant_query.split())
        base_upper = base_model.upper()
        
        # Strategy 1: Exact match (with or without base model prefix)
        for vname in variant_names:
            # "FIESTA 1.0T" with query "1.0T" → strip base model
            suffix = vname
            if suffix.startswith(base_upper):
                suffix = suffix[len(base_upper):].strip()
            if suffix == variant_query or vname == variant_query:
                return vname
        
        # Strategy 2: Query is a substring of a variant name
        candidates = []
        for vname in variant_names:
            if variant_query in vname:
                candidates.append(vname)
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            # Pick shortest (most specific) match
            return min(candidates, key=len)
        
        # Strategy 3: Token overlap scoring
        best_match = None
        best_score = 0
        for vname in variant_names:
            vname_tokens = set(vname.split()) - {base_upper}
            if not vname_tokens:
                continue
            overlap = len(query_tokens & vname_tokens)
            if overlap > best_score:
                best_score = overlap
                best_match = vname
        
        return best_match if best_score > 0 else None
    
    def search_makes(self, query: str, limit: int = 20) -> List[str]:
        """Search for matching vehicle makes"""
        query_upper = query.strip().upper()
        matches = [m for m in sorted(self._makes) if query_upper in m]
        return matches[:limit]
    
    def search_models(self, make: str, query: str = "", limit: int = 50) -> List[str]:
        """Search for matching models within a make"""
        make_upper = make.strip().upper()
        models = self._models_by_make.get(make_upper, set())
        
        if query:
            query_upper = query.strip().upper()
            matches = [m for m in sorted(models) if query_upper in m]
        else:
            matches = sorted(models)
        
        return matches[:limit]
    
    def get_all_makes(self) -> List[str]:
        """Return all known vehicle makes sorted alphabetically"""
        return sorted(self._makes)
