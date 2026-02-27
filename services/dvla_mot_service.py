"""
DVLA VES + MOT History API integration service.

Chains two UK government APIs to identify a vehicle from its registration number:
  1. DVLA Vehicle Enquiry Service (VES) — make, fuel, year, engine, tax/MOT status
  2. MOT History API (v1, OAuth 2.0) — model name, manufacture date, recalls

Then optionally feeds the result into the existing vehicle dimensions lookup.
"""

import logging
import time
from typing import Optional

import httpx

from config import (
    DVLA_API_KEY,
    MOT_API_KEY,
    MOT_CLIENT_ID,
    MOT_CLIENT_SECRET,
    MOT_TOKEN_URL,
    MOT_SCOPE,
)

logger = logging.getLogger(__name__)

DVLA_VES_URL = "https://driver-vehicle-licensing.api.gov.uk/vehicle-enquiry/v1/vehicles"
MOT_HISTORY_BASE = "https://history.mot.api.gov.uk"


class DvlaMotService:
    """Looks up vehicle identity from a UK registration number."""

    def __init__(self):
        self.dvla_api_key = DVLA_API_KEY
        self.mot_api_key = MOT_API_KEY
        self.mot_client_id = MOT_CLIENT_ID
        self.mot_client_secret = MOT_CLIENT_SECRET
        self.mot_token_url = MOT_TOKEN_URL
        self.mot_scope = MOT_SCOPE

        # Cached OAuth token
        self._mot_access_token: Optional[str] = None
        self._mot_token_expires_at: float = 0

    @property
    def is_configured(self) -> bool:
        """At least one API must be usable."""
        return bool(self.dvla_api_key or self.mot_configured)

    @property
    def dvla_configured(self) -> bool:
        return bool(self.dvla_api_key)

    @property
    def mot_configured(self) -> bool:
        """MOT needs API key + OAuth client credentials."""
        return bool(
            self.mot_api_key
            and self.mot_client_id
            and self.mot_client_secret
            and self.mot_token_url
        )

    # ── OAuth token management ────────────────────────────

    async def _get_mot_token(self, client: httpx.AsyncClient) -> Optional[str]:
        """Get or refresh the MOT API OAuth 2.0 access token."""
        now = time.time()
        if self._mot_access_token and now < self._mot_token_expires_at - 60:
            return self._mot_access_token

        try:
            resp = await client.post(
                self.mot_token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.mot_client_id,
                    "client_secret": self.mot_client_secret,
                    "scope": self.mot_scope,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.status_code == 200:
                token_data = resp.json()
                self._mot_access_token = token_data["access_token"]
                expires_in = int(token_data.get("expires_in", 1199))
                self._mot_token_expires_at = now + expires_in
                logger.info(f"MOT OAuth token acquired (expires in {expires_in}s)")
                return self._mot_access_token
            else:
                logger.error(f"MOT OAuth token request failed {resp.status_code}: {resp.text[:200]}")
                return None
        except httpx.HTTPError as exc:
            logger.error(f"MOT OAuth token request error: {exc}")
            return None

    async def lookup_registration(self, registration_number: str) -> dict:
        """
        Look up a vehicle by UK registration number.

        Returns a dict with keys:
            registration_number, make, model, year, fuel_type,
            engine_capacity_cc, colour, tax_status, tax_due_date,
            mot_status, mot_expiry_date, co2_emissions, revenue_weight_kg,
            wheelplan, month_first_registered, errors
        """
        reg = registration_number.upper().replace(" ", "")
        result = {
            "registration_number": reg,
            "make": None,
            "model": None,
            "year": None,
            "fuel_type": None,
            "engine_capacity_cc": None,
            "colour": None,
            "tax_status": None,
            "tax_due_date": None,
            "mot_status": None,
            "mot_expiry_date": None,
            "co2_emissions": None,
            "revenue_weight_kg": None,
            "wheelplan": None,
            "month_first_registered": None,
            "errors": [],
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            # ── DVLA VES ──────────────────────────────────────
            if self.dvla_api_key:
                try:
                    dvla_resp = await client.post(
                        DVLA_VES_URL,
                        json={"registrationNumber": reg},
                        headers={
                            "x-api-key": self.dvla_api_key,
                            "Content-Type": "application/json",
                        },
                    )
                    if dvla_resp.status_code == 200:
                        d = dvla_resp.json()
                        result["make"] = self._title(d.get("make"))
                        result["year"] = d.get("yearOfManufacture")
                        result["fuel_type"] = self._title(d.get("fuelType"))
                        result["engine_capacity_cc"] = d.get("engineCapacity")
                        result["colour"] = self._title(d.get("colour"))
                        result["tax_status"] = d.get("taxStatus")
                        result["tax_due_date"] = d.get("taxDueDate")
                        result["mot_status"] = d.get("motStatus")
                        result["mot_expiry_date"] = d.get("motExpiryDate")
                        result["co2_emissions"] = d.get("co2Emissions")
                        result["revenue_weight_kg"] = d.get("revenueWeight")
                        result["wheelplan"] = d.get("wheelplan")
                        result["month_first_registered"] = d.get("monthOfFirstRegistration")
                        logger.info(f"DVLA VES: {reg} → {result['make']} {result['year']}")
                    elif dvla_resp.status_code == 404:
                        result["errors"].append(
                            f"DVLA: registration {reg} not found"
                        )
                        logger.warning(f"DVLA VES 404 for {reg}")
                    else:
                        body = dvla_resp.text[:200]
                        result["errors"].append(
                            f"DVLA API error {dvla_resp.status_code}: {body}"
                        )
                        logger.error(f"DVLA VES {dvla_resp.status_code} for {reg}: {body}")
                except httpx.HTTPError as exc:
                    result["errors"].append(f"DVLA request failed: {exc}")
                    logger.error(f"DVLA VES request error for {reg}: {exc}")
            else:
                result["errors"].append("DVLA API key not configured")

            # ── MOT History (v1, OAuth 2.0) ──────────────────
            if self.mot_configured:
                try:
                    token = await self._get_mot_token(client)
                    if not token:
                        result["errors"].append("MOT: failed to obtain OAuth access token")
                    else:
                        mot_url = f"{MOT_HISTORY_BASE}/v1/trade/vehicles/registration/{reg}"
                        mot_resp = await client.get(
                            mot_url,
                            headers={
                                "Authorization": f"Bearer {token}",
                                "X-API-Key": self.mot_api_key,
                            },
                        )
                        if mot_resp.status_code == 200:
                            vehicle = mot_resp.json()
                            logger.debug(f"MOT raw response for {reg}: {vehicle}")
                            mot_model = vehicle.get("model")
                            if mot_model:
                                result["model"] = self._title(mot_model)
                            # Fill make from MOT if DVLA didn't provide it
                            if not result["make"] and vehicle.get("make"):
                                result["make"] = self._title(vehicle["make"])
                            # Fill fuel from MOT if DVLA didn't provide it
                            if not result["fuel_type"] and vehicle.get("fuelType"):
                                result["fuel_type"] = self._title(vehicle["fuelType"])
                            # Fill colour from MOT if DVLA didn't provide it
                            if not result["colour"] and vehicle.get("primaryColour"):
                                result["colour"] = self._title(vehicle["primaryColour"])
                            # Manufacture year (direct field or from date)
                            if not result["year"]:
                                mfg_year = vehicle.get("manufactureYear")
                                if mfg_year:
                                    try:
                                        result["year"] = int(mfg_year)
                                    except (ValueError, TypeError):
                                        pass
                                if not result["year"]:
                                    mfg_date = vehicle.get("manufactureDate")
                                    if mfg_date:
                                        try:
                                            result["year"] = int(mfg_date[:4])
                                        except (ValueError, TypeError):
                                            pass
                            # Engine size from MOT if DVLA didn't provide it
                            if not result["engine_capacity_cc"] and vehicle.get("engineSize"):
                                try:
                                    result["engine_capacity_cc"] = int(vehicle["engineSize"])
                                except (ValueError, TypeError):
                                    pass
                            # Outstanding recall info
                            recall = vehicle.get("hasOutstandingRecall")
                            if recall:
                                result["has_outstanding_recall"] = recall

                            logger.info(f"MOT History: {reg} → model={result['model']}")
                        elif mot_resp.status_code == 404:
                            result["errors"].append(
                                f"MOT History: registration {reg} not found (vehicle may be new / no MOT yet)"
                            )
                            logger.warning(f"MOT History 404 for {reg}")
                        else:
                            body = mot_resp.text[:200]
                            result["errors"].append(
                                f"MOT API error {mot_resp.status_code}: {body}"
                            )
                            logger.error(f"MOT History {mot_resp.status_code} for {reg}: {body}")
                except httpx.HTTPError as exc:
                    result["errors"].append(f"MOT request failed: {exc}")
                    logger.error(f"MOT History request error for {reg}: {exc}")
            else:
                result["errors"].append("MOT API not configured (need MOT_API_KEY + MOT_CLIENT_ID + MOT_CLIENT_SECRET + MOT_TOKEN_URL)")

        return result

    # ── helpers ────────────────────────────────────────────

    @staticmethod
    def _title(value: Optional[str]) -> Optional[str]:
        """Convert 'FORD' → 'Ford', None → None."""
        return value.strip().title() if value else None
