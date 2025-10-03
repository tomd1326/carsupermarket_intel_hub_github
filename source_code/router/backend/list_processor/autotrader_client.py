# FILE: source_code/router/backend/list_processor/autotrader_client.py

import asyncio
import datetime as dt
from typing import Any, Dict, List, Optional
import logging

import httpx

from utils.config import (
    AT_VEHICLES_RPS,
    AT_VALUATIONS_RPS,
    HTTP_TOTAL_RETRIES,
    HTTP_BACKOFF_BASE,
)
from utils.rate_limiter import AsyncTokenBucket
from utils.lambda_http import get_http_client

logger = logging.getLogger(__name__)


class AutoTraderClient:
    """
    Auto Trader API client with enhanced logging for debugging.
    """

    def __init__(
        self,
        base_url: str,
        advertiser_id: str,
        key: str,
        secret: str,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.advertiser_id = advertiser_id
        self.key = key
        self.secret = secret

        self._veh_rl = AsyncTokenBucket(AT_VEHICLES_RPS)
        self._val_rl = AsyncTokenBucket(AT_VALUATIONS_RPS)

        # Token cache with async lock
        self._token: Optional[str] = None
        self._token_expiry: Optional[dt.datetime] = None
        self._token_lock = asyncio.Lock()

    # ---------- Retry wrapper ----------

    async def _retry(self, coro_fn, *args, **kwargs):
        """Backoff retry for transient network/server failures."""
        backoff = HTTP_BACKOFF_BASE
        last_exc: Optional[Exception] = None

        for attempt in range(HTTP_TOTAL_RETRIES + 1):
            try:
                return await coro_fn(*args, **kwargs)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                last_exc = e
                if attempt >= HTTP_TOTAL_RETRIES:
                    raise
                await asyncio.sleep(backoff)
                backoff *= 2
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                # Retry transient errors and rate limits
                if status in (429, 500, 502, 503, 504) and attempt < HTTP_TOTAL_RETRIES:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise

        if last_exc:
            raise last_exc

    # ---------- Authentication ----------

    async def _authenticate(self) -> str:
        """Authenticate and cache token with ~15 min expiry."""
        if self._token and self._token_expiry and dt.datetime.now(dt.timezone.utc) < self._token_expiry:
            return self._token

        async with self._token_lock:
            if self._token and self._token_expiry and dt.datetime.now(dt.timezone.utc) < self._token_expiry:
                return self._token

            logger.debug("AT: Authenticating...")
            client = get_http_client()
            url = f"{self.base_url}/authenticate"

            async def _do():
                r = await client.post(url, data={"key": self.key, "secret": self.secret})
                r.raise_for_status()
                return r.json()

            data = await self._retry(_do)
            token = data.get("access_token")
            if not token:
                raise ValueError("No access_token in auth response")

            expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=15)
            self._token = token
            self._token_expiry = expiry
            logger.debug("AT: Authentication successful")
            return token

    async def _get_token(self) -> str:
        return await self._authenticate()

    # ---------- API calls ----------

    async def vehicles(self, vrm: str, mileage_miles: int) -> Dict[str, Any]:
        """GET /vehicles — vehicle details, metrics, first registration date, features."""
        logger.debug(f"AT Vehicles: vrm={vrm}, mileage={mileage_miles}")
        
        await self._veh_rl.acquire()
        client = get_http_client()
        token = await self._get_token()

        params = {
            "advertiserId": self.advertiser_id,
            "registration": vrm,
            "valuations": "true",
            "vehicleMetrics": "true",
            "odometerReadingMiles": mileage_miles,
            "includeFirstRegistrationDate": "true",
            "competitors": "false",
            "features": "true",
        }
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{self.base_url}/vehicles"

        async def _do():
            r = await client.get(url, params=params, headers=headers)
            r.raise_for_status()
            return r

        try:
            resp = await self._retry(_do)
            data = resp.json()

            vehicle = data.get("vehicle") or {}
            metrics_retail = ((data.get("vehicleMetrics") or {}).get("retail")) or {}
            features = data.get("features") or []

            factory_features = [
                f.get("name", "")
                for f in features
                if f.get("factoryFitted")
            ]

            derivative_id = vehicle.get("derivativeId", "")
            logger.debug(f"AT Vehicles: {vrm} success - derivative_id={derivative_id}")

            return {
                "at_success": True,
                "at_make": vehicle.get("make", ""),
                "at_model": vehicle.get("model", ""),
                "at_derivative": vehicle.get("derivative", ""),
                "at_derivative_id": derivative_id,
                "at_first_reg_date": vehicle.get("firstRegistrationDate", ""),
                "at_rating": (metrics_retail.get("rating") or {}).get("value", ""),
                "at_days_to_sell": (metrics_retail.get("daysToSell") or {}).get("value", ""),
                "at_market_condition": (metrics_retail.get("marketCondition") or {}).get("value", ""),
                "at_factory_features": ", ".join(factory_features),
                "at_response_code": resp.status_code,
            }
        except httpx.HTTPStatusError as e:
            logger.error(f"AT Vehicles HTTP {e.response.status_code} for {vrm}: {e.response.text[:200]}")
            return {
                "at_success": False,
                "at_error": f"Status {e.response.status_code}",
                "at_response_code": e.response.status_code,
            }
        except Exception as e:
            logger.exception(f"AT Vehicles failed for {vrm}")
            return {
                "at_success": False,
                "at_error": str(e),
                "at_response_code": 0,
            }

    async def valuations(
        self,
        vrm: str,
        derivative_id: str,
        first_reg_date: str,
        mileage_miles: int,
        features: List[str],
    ) -> Dict[str, Any]:
        """POST /valuations — retail valuation for given derivative and options."""
        if not derivative_id:
            logger.warning(f"AT Valuations: No derivative_id for {vrm}")
            return {"at_valuation": "No derivative ID", "at_valuation_success": False}

        logger.debug(f"AT Valuations: vrm={vrm}, derivative_id={derivative_id}, features={len(features)}")

        await self._val_rl.acquire()
        client = get_http_client()
        token = await self._get_token()

        url = f"{self.base_url}/valuations"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"advertiserId": self.advertiser_id}

        payload = {
            "vehicle": {
                "derivativeId": derivative_id,
                "firstRegistrationDate": first_reg_date,
                "odometerReadingMiles": mileage_miles,
            },
            "features": [{"name": f} for f in features if f][:10],
        }

        async def _do():
            r = await client.post(url, params=params, json=payload, headers=headers)
            r.raise_for_status()
            return r

        try:
            resp = await self._retry(_do)
            data = resp.json()
            valuation = (((data.get("valuations") or {}).get("retail")) or {}).get("amountGBP", "")

            logger.debug(f"AT Valuations: {vrm} success - valuation={valuation}")

            return {
                "at_valuation": valuation,
                "at_valuation_success": True,
            }
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            logger.error(f"AT Valuations HTTP {status} for {vrm}: {e.response.text[:200]}")
            return {
                "at_valuation": f"Error {status}",
                "at_valuation_success": False,
            }
        except Exception as e:
            logger.exception(f"AT Valuations failed for {vrm}")
            return {
                "at_valuation": f"Error: {str(e)}",
                "at_valuation_success": False,
            }
