"""Main vehicle lookup orchestration service"""

import logging
import time
import asyncio
from typing import Optional, List
from datetime import datetime

from models import VehicleSearchRequest, VehicleInfoResponse, GovDataFields, StatusEnum
from .cache_service import CacheService
from .bing_grounding_service import BingGroundingService
from .gov_data_service import GovDataService
from config import BATCH_MAX_CONCURRENT

logger = logging.getLogger(__name__)


class VehicleLookupService:
    """Orchestrates the full vehicle dimensions lookup process"""
    
    def __init__(
        self,
        cache_service: Optional[CacheService] = None,
        bing_grounding_service: Optional[BingGroundingService] = None,
        gov_data_service: Optional[GovDataService] = None,
    ):
        self.bing_service = bing_grounding_service
        self.cache_service = cache_service
        self.gov_service = gov_data_service
    
    async def lookup_vehicle(
        self,
        request: VehicleSearchRequest,
        use_cache: bool = True,
    ) -> VehicleInfoResponse:
        """
        Perform a complete vehicle dimensions lookup.
        
        Process:
        1. Check cache for existing result
        2. Look up gov.uk CSV data for make/model/fuel/engine info
        3. Search for dimensions & weight via Bing Grounding agent
        4. Merge results and cache
        """
        cache_key = self._build_cache_key(request)
        
        # Step 0: Check cache
        if use_cache and self.cache_service and self.cache_service.is_connected:
            cached = await self.cache_service.get_vehicle_lookup(
                request.make, request.model, request.year
            )
            if cached:
                logger.info(f"Returning cached result for: {request.make} {request.model}")
                response = self._dict_to_response(cached, request)
                response.errors.insert(0, f"[Cached result from {cached.get('_cached_at', 'unknown')}]")
                return response
        
        response = VehicleInfoResponse(
            search_make=request.make,
            search_model=request.model,
            search_year=request.year,
            last_checked=datetime.utcnow(),
        )
        
        try:
            # Step 1: Gov data lookup (fast, local)
            if self.gov_service and self.gov_service.is_loaded:
                gov_result = self.gov_service.lookup(
                    request.make, request.model, request.year
                )
                if gov_result:
                    response.gov_data = GovDataFields(
                        body_type=gov_result.get("body_type"),
                        generic_model=gov_result.get("generic_model"),
                        fuel_type=gov_result.get("fuel_type"),
                        engine_size_cc=gov_result.get("engine_size_cc"),
                        engine_size_band=gov_result.get("engine_size_band"),
                        total_registered=gov_result.get("total_registered"),
                        first_registered_year=gov_result.get("first_registered_year"),
                    )
                    logger.info(f"Gov data found for: {request.make} {request.model}")
            
            # Step 2: Bing Grounding agent search for dimensions & weight
            bing_result = None
            if self.bing_service and self.bing_service.is_configured:
                logger.info(f"Using Bing Grounding agent for: {request.make} {request.model}")
                bing_result = await self.bing_service.search_vehicle_async(
                    make=request.make,
                    model=request.model,
                    year=request.year,
                )
                
                if bing_result:
                    response.length_mm = bing_result.get("length_mm")
                    response.width_mm = bing_result.get("width_mm")
                    response.width_with_mirrors_mm = bing_result.get("width_with_mirrors_mm")
                    response.height_mm = bing_result.get("height_mm")
                    response.wheelbase_mm = bing_result.get("wheelbase_mm")
                    response.kerb_weight_kg = bing_result.get("kerb_weight_kg")
                    response.gross_weight_kg = bing_result.get("gross_weight_kg")
                    response.confidence_score = bing_result.get("confidence", 0.0)
                    response.dimensions_source = bing_result.get("source", "Bing Grounding")
                    response.weight_source = bing_result.get("source", "Bing Grounding")
                    logger.info(f"Bing Grounding returned data for: {request.make} {request.model}")
            else:
                response.errors.append("Bing Grounding not configured - dimensions search unavailable")
            
            # Step 3: Determine status
            has_dimensions = any([
                response.length_mm, response.width_mm, response.height_mm
            ])
            has_weight = response.kerb_weight_kg is not None
            has_gov = response.gov_data is not None
            
            if has_dimensions and has_weight:
                response.status = StatusEnum.SUCCESS
            elif has_dimensions or has_weight or has_gov:
                response.status = StatusEnum.PARTIAL
            else:
                response.status = StatusEnum.NOT_FOUND
                if not bing_result:
                    response.errors.append(f"No dimensions found for {request.make} {request.model}")
            
            # Step 4: Cache the result
            if self.cache_service and self.cache_service.is_connected:
                if response.status in (StatusEnum.SUCCESS, StatusEnum.PARTIAL):
                    await self.cache_service.set_vehicle_lookup(
                        request.make, request.model, request.year,
                        self._response_to_dict(response)
                    )
        
        except Exception as e:
            logger.error(f"Error looking up {request.make} {request.model}: {e}")
            response.status = StatusEnum.ERROR
            response.errors.append(str(e))
        
        return response
    
    async def lookup_batch(
        self,
        requests: List[VehicleSearchRequest],
        progress_callback=None,
    ) -> List[VehicleInfoResponse]:
        """Process a batch of vehicle lookups with controlled concurrency."""
        total = len(requests)
        results = [None] * total
        completed = 0
        semaphore = asyncio.Semaphore(BATCH_MAX_CONCURRENT)
        
        start_time = time.time()
        logger.info(f"Starting batch lookup: {total} vehicles")
        
        async def _process_one(index: int, request: VehicleSearchRequest):
            nonlocal completed
            async with semaphore:
                result = await self.lookup_vehicle(request)
                results[index] = result
                completed += 1
                
                if progress_callback:
                    await progress_callback(
                        completed, total,
                        f"{request.make} {request.model}",
                        result
                    )
                
                if completed % 10 == 0 or completed == total:
                    elapsed = time.time() - start_time
                    rate = completed / elapsed if elapsed > 0 else 0
                    logger.info(f"Batch progress: {completed}/{total} ({rate:.1f}/sec)")
        
        tasks = [_process_one(i, req) for i, req in enumerate(requests)]
        await asyncio.gather(*tasks, return_exceptions=True)
        
        # Replace any None results
        for i in range(total):
            if results[i] is None:
                results[i] = VehicleInfoResponse(
                    search_make=requests[i].make,
                    search_model=requests[i].model,
                    search_year=requests[i].year,
                    status=StatusEnum.ERROR,
                    errors=["Lookup failed unexpectedly"],
                )
        
        elapsed = time.time() - start_time
        logger.info(f"Batch complete: {total} vehicles in {elapsed:.1f}s")
        
        return results
    
    def _build_cache_key(self, request: VehicleSearchRequest) -> str:
        parts = [request.make.lower(), request.model.lower()]
        if request.year:
            parts.append(str(request.year))
        return ":".join(parts)
    
    def _response_to_dict(self, response: VehicleInfoResponse) -> dict:
        """Convert response to dict for caching"""
        d = {
            "search_make": response.search_make,
            "search_model": response.search_model,
            "search_year": response.search_year,
            "length_mm": response.length_mm,
            "width_mm": response.width_mm,
            "width_with_mirrors_mm": response.width_with_mirrors_mm,
            "height_mm": response.height_mm,
            "wheelbase_mm": response.wheelbase_mm,
            "kerb_weight_kg": response.kerb_weight_kg,
            "gross_weight_kg": response.gross_weight_kg,
            "dimensions_source": response.dimensions_source,
            "weight_source": response.weight_source,
            "status": response.status.value,
            "last_checked": response.last_checked.isoformat(),
            "confidence_score": response.confidence_score,
            "errors": response.errors,
        }
        if response.gov_data:
            d["gov_data"] = {
                "body_type": response.gov_data.body_type,
                "generic_model": response.gov_data.generic_model,
                "fuel_type": response.gov_data.fuel_type,
                "engine_size_cc": response.gov_data.engine_size_cc,
                "engine_size_band": response.gov_data.engine_size_band,
                "total_registered": response.gov_data.total_registered,
                "first_registered_year": response.gov_data.first_registered_year,
            }
        return d
    
    def _dict_to_response(self, data: dict, request: VehicleSearchRequest) -> VehicleInfoResponse:
        """Convert cached dict back to response"""
        gov_data = None
        if data.get("gov_data"):
            gd = data["gov_data"]
            gov_data = GovDataFields(
                body_type=gd.get("body_type"),
                generic_model=gd.get("generic_model"),
                fuel_type=gd.get("fuel_type"),
                engine_size_cc=gd.get("engine_size_cc"),
                engine_size_band=gd.get("engine_size_band"),
                total_registered=gd.get("total_registered"),
                first_registered_year=gd.get("first_registered_year"),
            )
        
        return VehicleInfoResponse(
            search_make=data.get("search_make", request.make),
            search_model=data.get("search_model", request.model),
            search_year=data.get("search_year", request.year),
            length_mm=data.get("length_mm"),
            width_mm=data.get("width_mm"),
            width_with_mirrors_mm=data.get("width_with_mirrors_mm"),
            height_mm=data.get("height_mm"),
            wheelbase_mm=data.get("wheelbase_mm"),
            kerb_weight_kg=data.get("kerb_weight_kg"),
            gross_weight_kg=data.get("gross_weight_kg"),
            gov_data=gov_data,
            dimensions_source=data.get("dimensions_source"),
            weight_source=data.get("weight_source"),
            status=StatusEnum(data.get("status", "partial")),
            last_checked=datetime.fromisoformat(data["last_checked"]) if data.get("last_checked") else datetime.utcnow(),
            confidence_score=data.get("confidence_score"),
            errors=data.get("errors", []),
        )
