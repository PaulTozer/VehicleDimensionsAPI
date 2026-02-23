"""
Bing Grounding Service - Uses Azure AI Foundry Agent with Bing Grounding
to search for vehicle dimensions and weight data.

The VehicleDimensionsSearch agent uses Bing grounding to find:
- Vehicle length, width, height
- Kerb weight and gross weight
- Wheelbase

Optimised for high-throughput batch processing with:
- Dedicated thread pool for blocking Azure SDK calls
- Semaphore-based concurrency limiting at the API level
- Retry with exponential backoff for transient errors
"""

import json
import logging
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, Any, List

from azure.ai.projects import AIProjectClient
from azure.ai.agents.models import BingGroundingTool, AgentThreadCreationOptions, ThreadMessageOptions
from azure.identity import DefaultAzureCredential

from config import (
    AZURE_AI_PROJECT_ENDPOINT,
    AZURE_AI_MODEL_DEPLOYMENT,
    BING_CONNECTION_NAME,
    BING_MAX_CONCURRENT,
    BING_THREAD_POOL_SIZE,
    BING_RETRY_MAX,
    BING_RETRY_DELAY_BASE,
)

logger = logging.getLogger(__name__)

# Agent instructions for the VehicleDimensionsSearch agent
SEARCH_AGENT_INSTRUCTIONS = """You are a vehicle specifications research assistant called VehicleDimensionsSearch.
Your job is to search the web to find accurate dimensions and weight data for vehicles sold in the UK.

When given a vehicle make, model, and optionally a year, you must search for and return:

1. **Length**: The overall length of the vehicle in millimetres
2. **Width**: The width of the vehicle excluding mirrors in millimetres
3. **Width with mirrors**: The width including door mirrors in millimetres (if available)
4. **Height**: The overall height of the vehicle in millimetres
5. **Wheelbase**: The wheelbase in millimetres
6. **Kerb weight**: The kerb weight in kilograms (unladen weight, ready to drive)
7. **Gross weight**: The gross vehicle weight (GVW/GVWR) in kilograms

IMPORTANT RULES:
- Search using Bing for the vehicle's official specifications
- Prioritise manufacturer specification pages, Parkers, Auto Express, Autocar, What Car?, or similar trusted UK car review/specification sites
- All dimensions MUST be in millimetres (mm). Convert from metres if needed (multiply by 1000).
- All weights MUST be in kilograms (kg). Convert from pounds if needed (divide by 2.205).
- If a year is specified, look for that specific model year's specs
- If no year, look for the latest/current model specifications
- Only include data you are confident about
- If you cannot find a measurement, set it to null

You MUST respond with ONLY valid JSON in this exact format (no markdown, no explanation, just JSON):
{
    "length_mm": <integer or null>,
    "width_mm": <integer or null>,
    "width_with_mirrors_mm": <integer or null>,
    "height_mm": <integer or null>,
    "wheelbase_mm": <integer or null>,
    "kerb_weight_kg": <integer or null>,
    "gross_weight_kg": <integer or null>,
    "vehicle_name_found": "<the exact vehicle name/variant found online>",
    "confidence": <0.0 to 1.0>,
    "search_sources": ["<list of URLs used as sources>"]
}
"""


class BingGroundingService:
    """
    Service that uses Azure AI Foundry agents with Bing grounding
    to search for vehicle dimensions and weight data.
    
    Optimised for high-throughput batch processing:
    - Dedicated ThreadPoolExecutor (not the default executor)
    - Asyncio Semaphore to limit concurrent API calls
    - Retry with exponential backoff for transient errors
    """

    def __init__(
        self,
        project_endpoint: Optional[str] = None,
        model_deployment: Optional[str] = None,
        bing_connection_name: Optional[str] = None,
        max_concurrent: Optional[int] = None,
        thread_pool_size: Optional[int] = None,
        max_retries: Optional[int] = None,
        retry_delay_base: Optional[float] = None,
    ):
        self.project_endpoint = project_endpoint or AZURE_AI_PROJECT_ENDPOINT
        self.model_deployment = model_deployment or AZURE_AI_MODEL_DEPLOYMENT
        self.bing_connection_name = bing_connection_name or BING_CONNECTION_NAME
        self.max_concurrent = max_concurrent or BING_MAX_CONCURRENT
        self.max_retries = max_retries or BING_RETRY_MAX
        self.retry_delay_base = retry_delay_base or BING_RETRY_DELAY_BASE

        self._client: Optional[AIProjectClient] = None
        self._agent = None
        self._initialized = False
        self._bing_tool_definitions = None

        # Dedicated thread pool for blocking Azure SDK calls
        pool_size = thread_pool_size or BING_THREAD_POOL_SIZE
        self._executor = ThreadPoolExecutor(
            max_workers=pool_size,
            thread_name_prefix="bing-grounding"
        )

        # Semaphore to limit concurrent API calls
        self._semaphore = asyncio.Semaphore(self.max_concurrent)

        # Metrics
        self._total_requests = 0
        self._successful_requests = 0
        self._failed_requests = 0
        self._retry_count = 0

    @property
    def is_configured(self) -> bool:
        return bool(self.project_endpoint and self.bing_connection_name)

    @property
    def metrics(self) -> Dict[str, Any]:
        return {
            "total_requests": self._total_requests,
            "successful": self._successful_requests,
            "failed": self._failed_requests,
            "retries": self._retry_count,
            "success_rate": (self._successful_requests / max(self._total_requests, 1)) * 100,
            "max_concurrent": self.max_concurrent,
            "thread_pool_size": self._executor._max_workers,
        }

    def _get_client(self) -> AIProjectClient:
        if self._client is None:
            credential = DefaultAzureCredential()
            self._client = AIProjectClient(
                endpoint=self.project_endpoint,
                credential=credential,
            )
            logger.info(f"Connected to AI Foundry project: {self.project_endpoint}")
        return self._client

    def _ensure_agent(self) -> None:
        if self._agent is not None:
            return

        client = self._get_client()

        bing_connection = client.connections.get(self.bing_connection_name)
        logger.info(f"Using Bing connection: {bing_connection.id}")

        bing_tool = BingGroundingTool(
            connection_id=bing_connection.id,
            market="en-GB",
            count=10,
        )
        self._bing_tool_definitions = bing_tool.definitions

        self._agent = client.agents.create_agent(
            model=self.model_deployment,
            name="VehicleDimensionsSearch",
            description="Searches for vehicle dimensions and weight using Bing grounding",
            instructions=SEARCH_AGENT_INSTRUCTIONS,
            tools=self._bing_tool_definitions,
        )

        logger.info(f"Created VehicleDimensionsSearch agent: {self._agent.id}")
        self._initialized = True

    def _build_search_prompt(
        self,
        make: str,
        model: str,
        year: Optional[int] = None,
        fuel_type: Optional[str] = None,
        model_variant: Optional[str] = None,
    ) -> str:
        if model_variant:
            parts = [f'Find the dimensions and weight specifications for the vehicle: "{make} {model} {model_variant}"']
        else:
            parts = [f'Find the dimensions and weight specifications for the vehicle: "{make} {model}"']

        if year:
            parts.append(f"Model year: {year}")

        if model_variant:
            parts.append(
                f"IMPORTANT: The specific variant/engine is '{model_variant}'. "
                "Different variants of the same model can have significantly different weights "
                "and dimensions (e.g. a 1.0L 3-cylinder is much lighter than a 2.0L turbo, "
                "an ST/RS performance variant may be wider/heavier). "
                "Make sure to find specs for THIS EXACT variant, not just the generic model."
            )

        if fuel_type:
            parts.append(f"Fuel type / powertrain: {fuel_type}")
            parts.append(
                "IMPORTANT: The fuel type affects the vehicle's weight significantly. "
                "An electric version is typically heavier than petrol/diesel due to the battery. "
                "A hybrid will weigh more than a pure petrol/diesel. "
                "Make sure to find the weight for the SPECIFIC fuel type / powertrain variant above."
            )

        parts.append(
            "\nSearch for the vehicle's length, width, height, wheelbase, kerb weight, and gross weight. "
            "Return ONLY the JSON response as specified in your instructions."
        )

        return "\n".join(parts)

    def search_vehicle(
        self,
        make: str,
        model: str,
        year: Optional[int] = None,
        fuel_type: Optional[str] = None,
        model_variant: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Search for vehicle dimensions using the Bing grounding agent.
        Includes retry with exponential backoff for transient errors.
        """
        if not self.is_configured:
            logger.warning("BingGroundingService not configured")
            return {}

        self._total_requests += 1
        last_error = None

        for attempt in range(1, self.max_retries + 1):
            try:
                self._ensure_agent()
                client = self._get_client()

                prompt = self._build_search_prompt(make, model, year, fuel_type, model_variant)

                if attempt > 1:
                    logger.info(f"Retry {attempt}/{self.max_retries} for: {make} {model}")
                    self._retry_count += 1

                run = client.agents.create_thread_and_process_run(
                    agent_id=self._agent.id,
                    thread=AgentThreadCreationOptions(
                        messages=[
                            ThreadMessageOptions(
                                role="user",
                                content=prompt,
                            )
                        ]
                    ),
                )

                if run.status != "completed":
                    error_msg = ""
                    if hasattr(run, "last_error") and run.last_error:
                        error_msg = str(run.last_error)

                    if attempt < self.max_retries and self._is_retryable_error(run.status, error_msg):
                        delay = self.retry_delay_base * (2 ** (attempt - 1))
                        logger.warning(f"Agent run {run.status} for {make} {model}, retrying in {delay}s: {error_msg}")
                        time.sleep(delay)
                        continue

                    logger.error(f"Agent run failed with status: {run.status} - {error_msg}")
                    self._failed_requests += 1
                    return {}

                messages = client.agents.messages.list(thread_id=run.thread_id)

                for msg in messages:
                    if msg.role == "assistant":
                        for content_block in msg.content:
                            if hasattr(content_block, "text"):
                                response_text = content_block.text.value
                                logger.debug(f"Agent response: {response_text[:500]}")

                                result = self._parse_agent_response(response_text)
                                if result:
                                    result["source"] = "Bing Grounding"
                                    self._successful_requests += 1
                                    return result

                if attempt < self.max_retries:
                    delay = self.retry_delay_base * (2 ** (attempt - 1))
                    logger.warning(f"No valid response for {make} {model}, retrying in {delay}s")
                    time.sleep(delay)
                    continue

                logger.warning(f"No valid response from agent for: {make} {model}")
                self._failed_requests += 1
                return {}

            except Exception as e:
                last_error = e
                if attempt < self.max_retries and self._is_retryable_exception(e):
                    delay = self.retry_delay_base * (2 ** (attempt - 1))
                    logger.warning(f"Bing search error for {make} {model} (attempt {attempt}), retrying in {delay}s: {e}")
                    self._retry_count += 1
                    time.sleep(delay)
                    continue

                logger.error(f"Bing grounding search failed for {make} {model}: {e}")
                self._failed_requests += 1
                return {}

        logger.error(f"All {self.max_retries} attempts failed for {make} {model}: {last_error}")
        self._failed_requests += 1
        return {}

    async def search_vehicle_async(
        self,
        make: str,
        model: str,
        year: Optional[int] = None,
        fuel_type: Optional[str] = None,
        model_variant: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Async search with semaphore-based concurrency control."""
        async with self._semaphore:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                self._executor,
                self.search_vehicle, make, model, year, fuel_type, model_variant
            )

    async def search_vehicles_batch(
        self,
        vehicles: List[Dict[str, Any]],
        progress_callback=None,
    ) -> List[Dict[str, Any]]:
        """Search for multiple vehicles concurrently with optimal throughput."""
        total = len(vehicles)
        results = [None] * total
        completed = 0

        logger.info(f"Starting batch search: {total} vehicles, max {self.max_concurrent} concurrent")
        start_time = time.time()

        async def search_one(index: int, vehicle: Dict[str, Any]):
            nonlocal completed
            result = await self.search_vehicle_async(
                make=vehicle.get("make", ""),
                model=vehicle.get("model", ""),
                year=vehicle.get("year"),
                fuel_type=vehicle.get("fuel_type"),
                model_variant=vehicle.get("model_variant"),
            )
            results[index] = result
            completed += 1

            if progress_callback:
                await progress_callback(completed, total, f"{vehicle.get('make', '')} {vehicle.get('model', '')}", result)

            if completed % 10 == 0 or completed == total:
                elapsed = time.time() - start_time
                rate = completed / elapsed if elapsed > 0 else 0
                logger.info(f"Batch progress: {completed}/{total} ({rate:.1f} vehicles/sec)")

        tasks = [search_one(i, v) for i, v in enumerate(vehicles)]
        await asyncio.gather(*tasks, return_exceptions=True)

        for i in range(total):
            if results[i] is None:
                results[i] = {}

        elapsed = time.time() - start_time
        rate = total / elapsed if elapsed > 0 else 0
        logger.info(
            f"Batch complete: {total} vehicles in {elapsed:.1f}s "
            f"({rate:.1f} vehicles/sec) | {self.metrics}"
        )

        return results

    @staticmethod
    def _is_retryable_error(status: str, error_msg: str) -> bool:
        retryable_statuses = {"failed", "expired", "incomplete"}
        retryable_phrases = {"rate_limit", "429", "throttl", "server_error", "timeout", "503", "502"}
        status_lower = status.lower() if status else ""
        error_lower = error_msg.lower() if error_msg else ""
        return (
            status_lower in retryable_statuses
            or any(phrase in error_lower for phrase in retryable_phrases)
        )

    @staticmethod
    def _is_retryable_exception(exc: Exception) -> bool:
        retryable_types = ("ConnectionError", "TimeoutError", "ServerError", "HttpResponseError")
        exc_name = type(exc).__name__
        exc_msg = str(exc).lower()
        return (
            exc_name in retryable_types
            or "429" in exc_msg
            or "rate" in exc_msg
            or "throttl" in exc_msg
            or "timeout" in exc_msg
            or "temporarily" in exc_msg
            or "503" in exc_msg
            or "502" in exc_msg
        )

    def _parse_agent_response(self, response_text: str) -> Optional[Dict[str, Any]]:
        """Parse the agent's JSON response, handling markdown code blocks"""
        if not response_text:
            return None

        text = response_text.strip()
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        try:
            result = json.loads(text)

            if not isinstance(result, dict):
                logger.warning("Agent response is not a JSON object")
                return None

            return {
                "length_mm": result.get("length_mm"),
                "width_mm": result.get("width_mm"),
                "width_with_mirrors_mm": result.get("width_with_mirrors_mm"),
                "height_mm": result.get("height_mm"),
                "wheelbase_mm": result.get("wheelbase_mm"),
                "kerb_weight_kg": result.get("kerb_weight_kg"),
                "gross_weight_kg": result.get("gross_weight_kg"),
                "vehicle_name_found": result.get("vehicle_name_found"),
                "confidence": result.get("confidence", 0.0),
                "search_sources": result.get("search_sources", []),
            }

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse agent response as JSON: {e}")
            logger.debug(f"Raw response: {text[:500]}")

            try:
                start = text.index("{")
                end = text.rindex("}") + 1
                json_str = text[start:end]
                result = json.loads(json_str)
                return {
                    "length_mm": result.get("length_mm"),
                    "width_mm": result.get("width_mm"),
                    "width_with_mirrors_mm": result.get("width_with_mirrors_mm"),
                    "height_mm": result.get("height_mm"),
                    "wheelbase_mm": result.get("wheelbase_mm"),
                    "kerb_weight_kg": result.get("kerb_weight_kg"),
                    "gross_weight_kg": result.get("gross_weight_kg"),
                    "vehicle_name_found": result.get("vehicle_name_found"),
                    "confidence": result.get("confidence", 0.0),
                    "search_sources": result.get("search_sources", []),
                }
            except (ValueError, json.JSONDecodeError):
                logger.error("Could not extract JSON from agent response")
                return None

    def cleanup(self) -> None:
        if self._agent and self._client:
            try:
                self._client.agents.delete_agent(self._agent.id)
                logger.info(f"Deleted VehicleDimensionsSearch agent: {self._agent.id}")
            except Exception as e:
                logger.warning(f"Failed to delete agent: {e}")

        if self._client:
            self._client.close()
            self._client = None

        self._agent = None
        self._initialized = False

        if self._executor:
            self._executor.shutdown(wait=False)
            logger.info("Shut down Bing grounding thread pool")

    async def cleanup_async(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.cleanup)


# Module-level singleton
_bing_service: Optional[BingGroundingService] = None


def get_bing_grounding_service() -> BingGroundingService:
    global _bing_service
    if _bing_service is None:
        _bing_service = BingGroundingService()
    return _bing_service
