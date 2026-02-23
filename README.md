# Vehicle Dimensions API

A FastAPI service that combines **UK government vehicle licensing data** with **AI-powered Bing Grounding search** to provide vehicle dimensions (length, width, height) and weight (kerb weight, gross weight) for any make and model.

## Data Sources

| Source | Data | Method |
|--------|------|--------|
| **UK Gov (DfT)** | Make, model, fuel type, engine size, registration counts | CSV download from gov.uk |
| **Bing Grounding** | Length, width, height, wheelbase, kerb weight, gross weight | Azure AI Foundry agent with Bing search |

**Gov.uk datasets used:**
- [VEH0124](https://www.gov.uk/government/statistical-data-sets/vehicle-licensing-statistics-data-files) – Vehicles by make, model, year of manufacture (A–M and N–Z)
- [VEH0220](https://www.gov.uk/government/statistical-data-sets/vehicle-licensing-statistics-data-files) – Vehicles by make, model, fuel type, engine size

## Architecture

```
Client Request
     │
     ▼
┌─────────────┐
│  FastAPI     │
│  main.py     │
└─────┬───────┘
      │
      ▼
┌─────────────────┐    ┌──────────────┐
│ VehicleLookup   │───▶│ Cache (Redis)│
│ Service         │    └──────────────┘
└────┬───────┬────┘
     │       │
     ▼       ▼
┌────────┐ ┌───────────────┐
│Gov CSV │ │Bing Grounding │
│Service │ │Service        │
└────────┘ └───────────────┘
     │             │
     ▼             ▼
 UK Gov CSVs   Azure AI Foundry
               + Bing Search
```

## Quick Start

### Prerequisites

- Python 3.12+
- Azure subscription with AI Foundry + Bing Search (for dimensions lookup)
- Redis (optional, for caching)

### Local Development

```bash
# Clone the repo
git clone https://github.com/PaulTozer/VehicleDimensionsAPI.git
cd VehicleDimensionsAPI

# Create virtual environment
python -m venv .venv
.venv\Scripts\activate   # Windows
# source .venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements.txt

# Copy environment config
cp .env.example .env
# Edit .env with your Azure credentials

# Run the API
uvicorn main:app --reload --port 8000
```

### Docker Compose

```bash
docker compose up --build
```

## API Endpoints

### Vehicle Lookup

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/vehicle/lookup` | Look up a single vehicle |
| `POST` | `/api/v1/vehicle/batch` | Batch lookup (max 500) |

**Single lookup request:**
```json
{
  "make": "BMW",
  "model": "3 Series",
  "year": 2020
}
```

**Response:**
```json
{
  "search_make": "BMW",
  "search_model": "3 Series",
  "search_year": 2020,
  "length_mm": 4709,
  "width_mm": 1827,
  "width_with_mirrors_mm": 2068,
  "height_mm": 1435,
  "wheelbase_mm": 2851,
  "kerb_weight_kg": 1530,
  "gross_weight_kg": 2060,
  "gov_data": {
    "fuel_type": "PETROL",
    "engine_size_cc": 1998,
    "body_type": "SALOON",
    "total_registered": 12345
  },
  "dimensions_source": "Bing Grounding",
  "weight_source": "Bing Grounding",
  "status": "SUCCESS",
  "confidence_score": 0.95
}
```

### Gov Data Browsing

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/gov/makes` | List all vehicle makes |
| `GET` | `/api/v1/gov/makes?q=BMW` | Search makes |
| `GET` | `/api/v1/gov/models/{make}` | List models for a make |
| `GET` | `/api/v1/gov/lookup/{make}/{model}` | Gov data only lookup |
| `GET` | `/api/v1/gov/stats` | Gov data statistics |

### Cache & Retry

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/cache/stats` | Cache statistics |
| `DELETE` | `/cache/clear` | Clear all cached data |
| `GET` | `/api/v1/retry/stats` | Retry queue statistics |
| `GET` | `/api/v1/retry/pending` | List pending retries |
| `POST` | `/api/v1/retry/process-all` | Process all pending retries |

### Health

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Health check |
| `GET` | `/metrics` | Service metrics |
| `GET` | `/docs` | Swagger UI |

## Configuration

Environment variables (see `.env.example`):

| Variable | Default | Description |
|----------|---------|-------------|
| `AZURE_AI_PROJECT_ENDPOINT` | — | Azure AI Foundry project endpoint |
| `AZURE_AI_MODEL_DEPLOYMENT_NAME` | `gpt-4.1-mini` | Model for Bing Grounding agent |
| `BING_CONNECTION_NAME` | `bing-grounding` | Bing connection name in AI Foundry |
| `USE_BING_GROUNDING` | `true` | Enable/disable Bing Grounding |
| `GOV_DATA_DIR` | `data` | Directory for gov CSV files |
| `GOV_DATA_AUTO_DOWNLOAD` | `true` | Auto-download missing CSV files |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `REDIS_ENABLED` | `true` | Enable/disable Redis caching |
| `CACHE_TTL_HOURS` | `168` | Cache TTL (7 days) |
| `BATCH_MAX_SIZE` | `500` | Max vehicles per batch |
| `BATCH_MAX_CONCURRENT` | `25` | Concurrent batch lookups |
| `LOG_LEVEL` | `INFO` | Logging level |

## Azure Deployment

Deploy the complete infrastructure with a single command:

```powershell
.\deploy.ps1
```

This creates all Azure resources:
- Azure AI Services with gpt-4.1-mini deployment
- Bing Search v7
- AI Hub + AI Project with Bing Grounding connection
- Container Registry + Container Apps
- Managed Identity with RBAC

To redeploy just the application:

```powershell
.\deploy.ps1 -SkipInfrastructure
```

## Project Structure

```
VehicleDimensionsAPI/
├── main.py                    # FastAPI application entry point
├── config.py                  # Environment variable configuration
├── models.py                  # Pydantic request/response models
├── requirements.txt           # Python dependencies
├── Dockerfile                 # Multi-stage Docker build
├── docker-compose.yml         # Local development with Redis
├── deploy.ps1                 # Azure deployment script
├── .env.example               # Environment variable template
├── .gitignore                 # Git ignore rules
├── services/
│   ├── __init__.py            # Service exports
│   ├── gov_data_service.py    # UK gov CSV download & lookup
│   ├── bing_grounding_service.py  # Azure AI Foundry Bing agent
│   ├── vehicle_lookup.py      # Orchestrator (cache → gov → bing → merge)
│   ├── cache_service.py       # Redis caching (7-day TTL)
│   └── retry_queue_service.py # Retry queue with backoff
├── infra/
│   └── main.bicep             # Azure Bicep infrastructure template
└── data/
    └── sample_vehicles.csv    # Sample vehicle list for testing
```

## License

MIT
