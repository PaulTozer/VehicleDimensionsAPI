"""
Vehicle Dimensions API

A FastAPI service that combines UK government vehicle licensing data with
AI-powered Bing Grounding search to provide vehicle dimensions (length, width,
height) and weight (kerb weight, gross weight) for any make/model.

Gov data source: https://www.gov.uk/government/statistical-data-sets/vehicle-licensing-statistics-data-files
"""

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import (
    LOG_LEVEL, REDIS_URL, REDIS_ENABLED, USE_BING_GROUNDING,
    BATCH_MAX_CONCURRENT, BATCH_MAX_SIZE,
    RETRY_MAX_ATTEMPTS, RETRY_BACKOFF_BASE, RETRY_MAX_CONCURRENT, RETRY_AUTO_ENQUEUE,
)
from models import (
    VehicleSearchRequest,
    VehicleInfoResponse,
    VehicleBatchRequest,
    BatchResponse,
    HealthResponse,
    StatusEnum,
    RetryItemResponse,
    RetryQueueStatsResponse,
    RetryAllResponse,
    GovDataStatsResponse,
)
from services import VehicleLookupService, GovDataService
from services.cache_service import CacheService
from services.bing_grounding_service import BingGroundingService
from services.retry_queue_service import RetryQueueService

# Configure logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Service instances
lookup_service: VehicleLookupService = None
gov_service: GovDataService = None
cache_service: CacheService = None
bing_service: BingGroundingService = None
retry_queue: RetryQueueService = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    global lookup_service, gov_service, cache_service, bing_service, retry_queue

    logger.info("Starting Vehicle Dimensions API...")

    # Initialize cache service
    cache_service = None
    if REDIS_ENABLED:
        cache_service = CacheService(REDIS_URL)
        connected = await cache_service.connect()
        if connected:
            logger.info(f"Redis caching enabled at {REDIS_URL}")
        else:
            logger.warning("Redis caching disabled (connection failed)")
            cache_service = None
    else:
        logger.info("Redis caching disabled by configuration")

    # Initialize gov data service
    gov_service = GovDataService()
    loaded = await gov_service.initialise()
    if loaded:
        stats = gov_service.get_stats()
        logger.info(
            f"Gov data loaded: {stats['unique_makes']} makes, "
            f"{stats['unique_models']} models"
        )
    else:
        logger.warning("Gov data not loaded — CSV files not available")

    # Initialize Bing Grounding service
    bing_service = None
    if USE_BING_GROUNDING:
        bing_service = BingGroundingService()
        if bing_service.is_configured:
            logger.info("Bing Grounding enabled (primary dimensions search)")
        else:
            logger.warning(
                "Bing Grounding not configured — "
                "check AZURE_AI_PROJECT_ENDPOINT and BING_CONNECTION_NAME"
            )
            bing_service = None
    else:
        logger.info("Bing Grounding disabled by configuration")

    # Initialize lookup service
    lookup_service = VehicleLookupService(
        cache_service=cache_service,
        bing_grounding_service=bing_service,
        gov_data_service=gov_service,
    )

    # Initialize retry queue
    redis_client = cache_service._client if cache_service and cache_service.is_connected else None
    retry_queue = RetryQueueService(
        redis_client=redis_client,
        max_attempts=RETRY_MAX_ATTEMPTS,
        backoff_base=RETRY_BACKOFF_BASE,
    )
    storage = "Redis" if retry_queue.uses_redis else "in-memory"
    logger.info(
        f"Retry queue enabled (storage: {storage}, max attempts: {RETRY_MAX_ATTEMPTS}, "
        f"auto-enqueue: {RETRY_AUTO_ENQUEUE})"
    )

    yield

    # Cleanup
    if bing_service:
        await bing_service.cleanup_async()
    if cache_service:
        await cache_service.disconnect()
    logger.info("Shutting down Vehicle Dimensions API...")


app = FastAPI(
    title="Vehicle Dimensions API",
    description="""
    An API that combines UK government vehicle licensing data with AI-powered
    Bing Grounding search to provide vehicle dimensions and weight.
    
    ## Data Sources
    - **UK Gov CSV**: Make, model, fuel type, engine size, registration counts
      (from DfT vehicle licensing statistics)
    - **Bing Grounding**: Length, width, height, kerb weight, gross weight
      (via Azure AI Foundry agent with Bing search)
    
    ## Endpoints
    - **Single lookup**: POST /api/v1/vehicle/lookup
    - **Batch lookup**: POST /api/v1/vehicle/batch
    - **Gov data browsing**: GET /api/v1/gov/makes, /api/v1/gov/models/{make}
    """,
    version="1.0.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────
# Health & Metrics
# ──────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse, tags=["Health"])
async def health_check():
    search_provider = "Bing Grounding" if bing_service else "Not configured"
    gov_status = None
    if gov_service:
        stats = gov_service.get_stats()
        gov_status = f"{stats['unique_makes']} makes loaded" if gov_service.is_loaded else "not loaded"
    retry_storage = None
    if retry_queue:
        retry_storage = "Redis" if retry_queue.uses_redis else "in-memory"
    return HealthResponse(
        status="healthy",
        version="1.0.0",
        ai_provider="Azure AI Foundry" if bing_service else "None",
        ai_configured=bing_service is not None and bing_service.is_configured,
        search_provider=search_provider,
        gov_data=gov_status,
        retry_queue=retry_storage,
    )


@app.get("/metrics", tags=["Health"])
async def get_metrics():
    bing_metrics = bing_service.metrics if bing_service else {}
    gov_stats = gov_service.get_stats() if gov_service else {}
    return {
        "bing_grounding": bing_metrics,
        "gov_data": gov_stats,
        "batch_config": {
            "max_concurrent_lookups": BATCH_MAX_CONCURRENT,
            "max_batch_size": BATCH_MAX_SIZE,
        },
    }


# ──────────────────────────────────────────────
# Vehicle Lookup
# ──────────────────────────────────────────────

@app.post("/api/v1/vehicle/lookup", response_model=VehicleInfoResponse, tags=["Vehicle Lookup"])
async def lookup_vehicle(request: VehicleSearchRequest):
    """
    Look up dimensions and weight for a single vehicle.
    
    Combines gov.uk licensing data (fuel, engine, registration stats)
    with AI-powered Bing search (length, width, height, weight).
    """
    if not lookup_service:
        raise HTTPException(status_code=503, detail="Service not ready")

    result = await lookup_service.lookup_vehicle(request)
    return result


@app.post("/api/v1/vehicle/batch", response_model=BatchResponse, tags=["Vehicle Lookup"])
async def batch_lookup(
    request: VehicleBatchRequest,
    skip_cache: bool = Query(False, description="Skip cache and force fresh lookup"),
):
    """
    Look up dimensions for multiple vehicles in parallel.
    
    Max batch size: 500 vehicles.
    """
    if not lookup_service:
        raise HTTPException(status_code=503, detail="Service not ready")

    if len(request.vehicles) > BATCH_MAX_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"Batch size {len(request.vehicles)} exceeds maximum of {BATCH_MAX_SIZE}"
        )

    start = time.time()
    results = await lookup_service.lookup_batch(request.vehicles)
    elapsed = time.time() - start

    successful = sum(1 for r in results if r.status == StatusEnum.SUCCESS)
    partial = sum(1 for r in results if r.status == StatusEnum.PARTIAL)
    failed = len(results) - successful - partial

    # Auto-enqueue failures
    if retry_queue and RETRY_AUTO_ENQUEUE:
        failed_results = [
            (req, res) for req, res in zip(request.vehicles, results)
            if res.status in (StatusEnum.NOT_FOUND, StatusEnum.ERROR)
        ]
        if failed_results:
            batch_id = str(uuid.uuid4())[:8]
            await retry_queue.enqueue_batch_failures(
                results=[r for _, r in failed_results],
                requests=[r for r, _ in failed_results],
                batch_id=batch_id,
            )
            logger.info(f"Auto-enqueued {len(failed_results)} failures (batch {batch_id})")

    return BatchResponse(
        total_requested=len(request.vehicles),
        successful=successful,
        partial=partial,
        failed=failed,
        processing_time_seconds=round(elapsed, 2),
        results=results,
    )


# ──────────────────────────────────────────────
# Gov Data Browsing
# ──────────────────────────────────────────────

@app.get("/api/v1/gov/stats", response_model=GovDataStatsResponse, tags=["Gov Data"])
async def gov_data_stats():
    """Get statistics about loaded gov.uk vehicle data"""
    if not gov_service:
        raise HTTPException(status_code=503, detail="Gov data service not available")
    stats = gov_service.get_stats()
    return GovDataStatsResponse(**stats)


@app.get("/api/v1/gov/makes", tags=["Gov Data"])
async def list_makes(q: str = Query("", description="Filter makes by search query")):
    """List all known vehicle makes from gov.uk data"""
    if not gov_service or not gov_service.is_loaded:
        raise HTTPException(status_code=503, detail="Gov data not loaded")
    if q:
        return {"makes": gov_service.search_makes(q)}
    return {"makes": gov_service.get_all_makes()}


@app.get("/api/v1/gov/models/{make}", tags=["Gov Data"])
async def list_models(make: str, q: str = Query("", description="Filter models by search query")):
    """List all known models for a given make from gov.uk data"""
    if not gov_service or not gov_service.is_loaded:
        raise HTTPException(status_code=503, detail="Gov data not loaded")
    return {"make": make, "models": gov_service.search_models(make, q)}


@app.get("/api/v1/gov/lookup/{make}/{model}", tags=["Gov Data"])
async def gov_lookup(make: str, model: str, year: Optional[int] = None):
    """Look up a vehicle in gov.uk data only (no Bing search)"""
    if not gov_service or not gov_service.is_loaded:
        raise HTTPException(status_code=503, detail="Gov data not loaded")
    result = gov_service.lookup(make, model, year)
    if not result:
        raise HTTPException(status_code=404, detail=f"No gov data found for {make} {model}")
    return {"make": make, "model": model, "year": year, "gov_data": result}


# ──────────────────────────────────────────────
# Cache Management
# ──────────────────────────────────────────────

@app.get("/cache/stats", tags=["Cache"])
async def cache_stats():
    if not cache_service:
        return {"enabled": False, "message": "Caching is disabled"}
    return await cache_service.get_stats()


@app.delete("/cache/clear", tags=["Cache"])
async def cache_clear():
    if not cache_service:
        raise HTTPException(status_code=400, detail="Caching is disabled")
    cleared = await cache_service.clear_all()
    return {"cleared": cleared}


# ──────────────────────────────────────────────
# Retry Queue — static routes FIRST
# ──────────────────────────────────────────────

@app.get("/api/v1/retry/stats", response_model=RetryQueueStatsResponse, tags=["Retry Queue"])
async def retry_stats():
    if not retry_queue:
        raise HTTPException(status_code=503, detail="Retry queue not available")
    return await retry_queue.get_stats()


@app.get("/api/v1/retry/pending", tags=["Retry Queue"])
async def retry_pending():
    if not retry_queue:
        raise HTTPException(status_code=503, detail="Retry queue not available")
    items = await retry_queue.get_pending()
    return [i.to_dict() for i in items]


@app.get("/api/v1/retry/all", tags=["Retry Queue"])
async def retry_all_items():
    if not retry_queue:
        raise HTTPException(status_code=503, detail="Retry queue not available")
    items = await retry_queue.get_all()
    return [i.to_dict() for i in items]


@app.get("/api/v1/retry/history", tags=["Retry Queue"])
async def retry_history():
    if not retry_queue:
        raise HTTPException(status_code=503, detail="Retry queue not available")
    return await retry_queue.get_history()


@app.post("/api/v1/retry/enqueue", tags=["Retry Queue"])
async def retry_enqueue(request: VehicleSearchRequest):
    if not retry_queue:
        raise HTTPException(status_code=503, detail="Retry queue not available")
    item = await retry_queue.enqueue(
        vehicle_make=request.make,
        vehicle_model=request.model,
        year=request.year,
    )
    return item.to_dict()


@app.post("/api/v1/retry/process-all", response_model=RetryAllResponse, tags=["Retry Queue"])
async def retry_process_all():
    if not retry_queue or not lookup_service:
        raise HTTPException(status_code=503, detail="Service not ready")
    result = await retry_queue.retry_all_pending(
        lookup_service, max_concurrent=RETRY_MAX_CONCURRENT
    )
    return result


@app.delete("/api/v1/retry/clear/pending", tags=["Retry Queue"])
async def retry_clear_pending():
    if not retry_queue:
        raise HTTPException(status_code=503, detail="Retry queue not available")
    cleared = await retry_queue.clear_queue()
    return {"cleared": cleared}


@app.delete("/api/v1/retry/clear/history", tags=["Retry Queue"])
async def retry_clear_history():
    if not retry_queue:
        raise HTTPException(status_code=503, detail="Retry queue not available")
    cleared = await retry_queue.clear_history()
    return {"cleared": cleared}


# Parameterised retry routes AFTER static routes
@app.post("/api/v1/retry/{item_id}", tags=["Retry Queue"])
async def retry_one(item_id: str):
    if not retry_queue or not lookup_service:
        raise HTTPException(status_code=503, detail="Service not ready")
    result = await retry_queue.retry_one(item_id, lookup_service)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Retry item {item_id} not found")
    return result


@app.delete("/api/v1/retry/{item_id}", tags=["Retry Queue"])
async def retry_remove(item_id: str):
    if not retry_queue:
        raise HTTPException(status_code=503, detail="Retry queue not available")
    removed = await retry_queue.remove_item(item_id)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
    return {"removed": item_id}
