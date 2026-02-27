"""Configuration settings for the Vehicle Dimensions API"""

import os
from dotenv import load_dotenv

load_dotenv()

# Azure OpenAI Configuration
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4")
AZURE_OPENAI_FALLBACK_DEPLOYMENT = os.getenv("AZURE_OPENAI_FALLBACK_DEPLOYMENT", "gpt-4.1-mini")

# Azure AI Foundry Configuration (for Bing Grounding agent)
AZURE_AI_PROJECT_ENDPOINT = os.getenv("AZURE_AI_PROJECT_ENDPOINT")
AZURE_AI_MODEL_DEPLOYMENT = os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "gpt-4.1-mini")
BING_CONNECTION_NAME = os.getenv("BING_CONNECTION_NAME")
USE_BING_GROUNDING = os.getenv("USE_BING_GROUNDING", "true").lower() == "true"

# Redis Configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
REDIS_ENABLED = os.getenv("REDIS_ENABLED", "true").lower() == "true"
CACHE_TTL_HOURS = int(os.getenv("CACHE_TTL_HOURS", "168"))  # 7 days - vehicle specs rarely change

# Gov.uk CSV Data
GOV_DATA_DIR = os.getenv("GOV_DATA_DIR", "data")
GOV_DATA_AUTO_DOWNLOAD = os.getenv("GOV_DATA_AUTO_DOWNLOAD", "true").lower() == "true"

# Determine which AI provider to use
USE_AZURE_OPENAI = bool(AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY)

# Rate limiting
MAX_REQUESTS_PER_MINUTE = int(os.getenv("MAX_REQUESTS_PER_MINUTE", "60"))

# Batch processing
BATCH_MAX_CONCURRENT = int(os.getenv("BATCH_MAX_CONCURRENT", "25"))
BATCH_MAX_SIZE = int(os.getenv("BATCH_MAX_SIZE", "500"))

# Bing Grounding concurrency
BING_MAX_CONCURRENT = int(os.getenv("BING_MAX_CONCURRENT", "15"))
BING_THREAD_POOL_SIZE = int(os.getenv("BING_THREAD_POOL_SIZE", "20"))
BING_RETRY_MAX = int(os.getenv("BING_RETRY_MAX", "3"))
BING_RETRY_DELAY_BASE = float(os.getenv("BING_RETRY_DELAY_BASE", "2.0"))

# Retry queue
RETRY_MAX_ATTEMPTS = int(os.getenv("RETRY_MAX_ATTEMPTS", "3"))
RETRY_BACKOFF_BASE = float(os.getenv("RETRY_BACKOFF_BASE", "30.0"))
RETRY_MAX_CONCURRENT = int(os.getenv("RETRY_MAX_CONCURRENT", "5"))
RETRY_AUTO_ENQUEUE = os.getenv("RETRY_AUTO_ENQUEUE", "true").lower() == "true"

# DVLA VES & MOT History APIs
DVLA_API_KEY = os.getenv("DVLA_API_KEY")
MOT_API_KEY = os.getenv("MOT_API_KEY")
MOT_CLIENT_ID = os.getenv("MOT_CLIENT_ID")
MOT_CLIENT_SECRET = os.getenv("MOT_CLIENT_SECRET")
MOT_TOKEN_URL = os.getenv("MOT_TOKEN_URL")  # e.g. https://login.microsoftonline.com/{tenantId}/oauth2/v2.0/token
MOT_SCOPE = os.getenv("MOT_SCOPE", "https://tapi.dvsa.gov.uk/.default")

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
