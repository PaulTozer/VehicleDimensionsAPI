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
import time
from collections import defaultdict
from typing import Optional, Dict, Any, List, Tuple

import httpx

from config import GOV_DATA_DIR, GOV_DATA_AUTO_DOWNLOAD

logger = logging.getLogger(__name__)

# Gov.uk download URLs for the CSV data files
GOV_CSV_URLS = {
    "veh0124_am": "https://assets.publishing.service.gov.uk/media/67a170e5ad1e4b41e585b1e7/df_VEH0124_AM.csv",
    "veh0124_nz": "https://assets.publishing.service.gov.uk/media/67a170f9e2fb9614db027f7a/df_VEH0124_NZ.csv",
    "veh0220": "https://assets.publishing.service.gov.uk/media/67a17155e2fb9614db027f7d/df_VEH0220.csv",
}

# Local file names
GOV_CSV_FILES = {
    "veh0124_am": "df_VEH0124_AM.csv",
    "veh0124_nz": "df_VEH0124_NZ.csv",
    "veh0220": "df_VEH0220.csv",
}


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
        """Download any missing CSV files from gov.uk"""
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            for key, url in GOV_CSV_URLS.items():
                filepath = os.path.join(self.data_dir, GOV_CSV_FILES[key])
                if os.path.exists(filepath):
                    size_mb = os.path.getsize(filepath) / (1024 * 1024)
                    logger.info(f"  {GOV_CSV_FILES[key]} exists ({size_mb:.1f} MB), skipping download")
                    continue
                
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
                        }
                    
                    entry = self._veh0124_index[key]
                    
                    # Track manufacture years
                    if year_mfr and year_mfr.isdigit():
                        entry["years_manufactured"].add(int(year_mfr))
                    
                    # Sum up the count columns (right-most columns are yearly counts)
                    for col_name, value in row.items():
                        if col_name.startswith("20") and value and value not in ("[c]", "[x]", "[z]"):
                            try:
                                entry["total_count"] += int(value)
                            except ValueError:
                                pass
                    
                    rows_loaded += 1
            
        except Exception as e:
            logger.error(f"Error loading VEH0124 from {filepath}: {e}")
        
        return rows_loaded
    
    def _load_veh0220(self, filepath: str) -> int:
        """
        Load VEH0220 CSV (vehicles by make, model, fuel type, engine size).
        
        Expected columns: BodyType, Make, GenModel, Model, Fuel, EngineSizeSimple, EngineSizeDesc, <year columns>
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
                        }
                    
                    entry = self._veh0220_index[key]
                    
                    if fuel:
                        entry["fuel_types"].add(fuel)
                    if engine_size and engine_size not in ("[x]", "[z]"):
                        try:
                            entry["engine_sizes"].add(int(engine_size))
                        except ValueError:
                            pass
                    if engine_desc and engine_desc not in ("[x]", "[z]"):
                        entry["engine_size_bands"].add(engine_desc)
                    
                    # Sum yearly counts
                    for col_name, value in row.items():
                        if col_name.startswith("20") and value and value not in ("[c]", "[x]", "[z]"):
                            try:
                                entry["total_count"] += int(value)
                            except ValueError:
                                pass
                    
                    rows_loaded += 1
            
        except Exception as e:
            logger.error(f"Error loading VEH0220 from {filepath}: {e}")
        
        return rows_loaded
    
    def lookup(self, make: str, model: str, year: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        Look up a vehicle in the gov data by make and model.
        
        Returns gov data fields if found, None otherwise.
        Tries exact match first, then generic model match, then fuzzy.
        """
        make_upper = make.strip().upper()
        model_upper = model.strip().upper()
        
        # Strategy 1: Exact match on (make, model)
        result = self._try_lookup(make_upper, model_upper, year)
        if result:
            return result
        
        # Strategy 2: Try matching just using the first word of model (generic model)
        model_first_word = model_upper.split()[0] if model_upper else ""
        if model_first_word and model_first_word != model_upper:
            result = self._try_lookup(make_upper, model_first_word, year)
            if result:
                return result
        
        # Strategy 3: Check if any indexed model starts with or contains the search model
        for indexed_key in list(self._veh0124_index.keys()) + list(self._veh0220_index.keys()):
            if indexed_key[0] == make_upper:
                indexed_model = indexed_key[1]
                if model_upper in indexed_model or indexed_model in model_upper:
                    result = self._try_lookup(make_upper, indexed_model, year)
                    if result:
                        return result
        
        return None
    
    def _try_lookup(self, make: str, model: str, year: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Try to find data for a specific make/model key"""
        key = (make, model)
        
        result = {}
        
        # Check VEH0124
        if key in self._veh0124_index:
            entry = self._veh0124_index[key]
            result["body_type"] = entry.get("body_type")
            result["generic_model"] = entry.get("generic_model")
            result["total_registered"] = entry.get("total_count", 0)
            
            years = entry.get("years_manufactured", set())
            if years:
                result["first_registered_year"] = min(years)
                if year and year not in years:
                    # The specific year wasn't found but we have data for this model
                    result["year_match"] = False
                else:
                    result["year_match"] = True
        
        # Check VEH0220
        if key in self._veh0220_index:
            entry = self._veh0220_index[key]
            if not result.get("body_type"):
                result["body_type"] = entry.get("body_type")
            if not result.get("generic_model"):
                result["generic_model"] = entry.get("generic_model")
            
            fuel_types = entry.get("fuel_types", set())
            if fuel_types:
                # Return most common/primary fuel type
                result["fuel_type"] = sorted(fuel_types)[0] if len(fuel_types) == 1 else ", ".join(sorted(fuel_types))
            
            engine_sizes = entry.get("engine_sizes", set())
            if engine_sizes:
                result["engine_size_cc"] = max(engine_sizes)  # Most common variant
            
            engine_bands = entry.get("engine_size_bands", set())
            if engine_bands:
                result["engine_size_band"] = sorted(engine_bands)[-1]
            
            if not result.get("total_registered"):
                result["total_registered"] = entry.get("total_count", 0)
        
        return result if result else None
    
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
