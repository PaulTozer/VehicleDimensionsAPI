"""Pydantic models for the Vehicle Dimensions API"""

from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
from enum import Enum


class StatusEnum(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"  # Some data found but not all (e.g. got weight but not dimensions)
    NOT_FOUND = "not_found"
    ERROR = "error"


class VehicleSearchRequest(BaseModel):
    """Request model for vehicle dimensions lookup"""
    make: str = Field(..., description="Vehicle make (e.g. Ford, BMW, Toyota)", min_length=1)
    model: str = Field(..., description="Vehicle model (e.g. Focus, 3 Series, Corolla)", min_length=1)
    year: Optional[int] = Field(None, description="Year of manufacture (e.g. 2020)", ge=1900, le=2030)
    fuel_type: Optional[str] = Field(None, description="Fuel type (e.g. Petrol, Diesel, Electric, Hybrid). If omitted, will be inferred from gov data when available.")
    model_variant: Optional[str] = Field(None, description="Specific model variant or engine (e.g. '1.0T EcoBoost', 'ST-3', '320d', '1117 HC'). Helps distinguish different engines and trim levels that affect weight and dimensions.")
    
    class Config:
        json_schema_extra = {
            "example": {
                "make": "Ford",
                "model": "Focus",
                "year": 2020,
                "fuel_type": "Petrol",
                "model_variant": "1.0T EcoBoost"
            }
        }


class VehicleBatchRequest(BaseModel):
    """Request model for batch vehicle lookup"""
    vehicles: List[VehicleSearchRequest] = Field(..., min_length=1, max_length=500)


class GovDataFields(BaseModel):
    """Fields available from UK gov licensing CSV data"""
    body_type: Optional[str] = Field(None, description="Vehicle body type (Cars, Motorcycles, etc.)")
    generic_model: Optional[str] = Field(None, description="Generic model grouping from gov data")
    matched_variant: Optional[str] = Field(None, description="The specific model variant matched in gov data (e.g. 'FIESTA 1.0T ECOBOOST')")
    fuel_type: Optional[str] = Field(None, description="Fuel type (Petrol, Diesel, Electric, Hybrid, etc.)")
    engine_size_cc: Optional[int] = Field(None, description="Engine size in cc")
    engine_size_band: Optional[str] = Field(None, description="Engine size band (e.g. '1301cc to 1400cc')")
    total_registered: Optional[int] = Field(None, description="Total registered vehicles of this make/model")
    first_registered_year: Optional[int] = Field(None, description="Year first registered in UK")
    available_variants: Optional[List[str]] = Field(None, description="Known model variants from gov data for this make/model (e.g. ['FIESTA 1.0T', 'FIESTA ST-3', 'FIESTA 1117 HC'])")


class VehicleInfoResponse(BaseModel):
    """Response model with extracted vehicle information"""
    # Input echo
    search_make: str
    search_model: str
    search_year: Optional[int] = None
    search_variant: Optional[str] = None
    
    # Dimensions from Bing Grounding search
    length_mm: Optional[int] = Field(None, description="Vehicle length in millimetres")
    width_mm: Optional[int] = Field(None, description="Vehicle width in millimetres (excluding mirrors)")
    width_with_mirrors_mm: Optional[int] = Field(None, description="Vehicle width including mirrors")
    height_mm: Optional[int] = Field(None, description="Vehicle height in millimetres")
    wheelbase_mm: Optional[int] = Field(None, description="Wheelbase in millimetres")
    
    # Weight from Bing Grounding search
    kerb_weight_kg: Optional[int] = Field(None, description="Kerb weight in kilograms")
    gross_weight_kg: Optional[int] = Field(None, description="Gross vehicle weight in kilograms")
    
    # Gov data fields
    gov_data: Optional[GovDataFields] = Field(None, description="Data from UK gov vehicle licensing statistics")
    
    # Source tracking
    dimensions_source: Optional[str] = Field(None, description="Source URL/method for dimensions data")
    weight_source: Optional[str] = Field(None, description="Source URL/method for weight data")
    
    # Metadata
    status: StatusEnum = StatusEnum.NOT_FOUND
    last_checked: datetime = Field(default_factory=datetime.utcnow)
    confidence_score: Optional[float] = Field(None, ge=0.0, le=1.0, description="AI confidence in the extracted data")
    errors: List[str] = Field(default_factory=list)
    
    class Config:
        json_schema_extra = {
            "example": {
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
                "gov_data": {
                    "body_type": "Cars",
                    "generic_model": "FOCUS",
                    "fuel_type": "Petrol",
                    "engine_size_cc": 1000,
                    "total_registered": 45230
                },
                "dimensions_source": "Bing Grounding",
                "weight_source": "Bing Grounding",
                "status": "success",
                "confidence_score": 0.92,
                "errors": []
            }
        }


class BatchResponse(BaseModel):
    """Response model for batch requests"""
    total_requested: int
    successful: int
    partial: int
    failed: int
    processing_time_seconds: Optional[float] = None
    results: List[VehicleInfoResponse]


class RetryItemResponse(BaseModel):
    """A single retry queue item"""
    id: str
    vehicle_make: str
    vehicle_model: str
    year: Optional[int] = None
    original_errors: List[str] = Field(default_factory=list)
    original_status: str = "not_found"
    source_batch_id: Optional[str] = None
    status: str = "pending"
    attempt_count: int = 0
    max_attempts: int = 3
    created_at: str
    last_attempt_at: Optional[str] = None
    next_retry_at: Optional[str] = None
    last_errors: List[str] = Field(default_factory=list)
    result: Optional[dict] = None


class RetryQueueStatsResponse(BaseModel):
    """Retry queue statistics"""
    queue_size: int
    pending: int
    retrying: int
    history_size: int
    total_succeeded: int
    total_exhausted: int
    storage: str
    max_attempts: int
    backoff_base_seconds: float
    is_processing: bool


class RetryAllResponse(BaseModel):
    """Response from processing all pending retries"""
    processed: int
    succeeded: int
    still_failed: int
    exhausted: int
    error: Optional[str] = None


class GovDataStatsResponse(BaseModel):
    """Statistics about loaded gov.uk CSV data"""
    veh0124_loaded: bool = False
    veh0124_rows: int = 0
    veh0220_loaded: bool = False
    veh0220_rows: int = 0
    unique_makes: int = 0
    unique_models: int = 0
    last_refreshed: Optional[str] = None


class HealthResponse(BaseModel):
    """Health check response"""
    status: str
    version: str
    ai_provider: str
    ai_configured: bool
    search_provider: str = "Unknown"
    gov_data: Optional[str] = None
    retry_queue: Optional[str] = None
