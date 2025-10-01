# FILE: source_code/router/backend/list_processor/autotrader_client.py

import asyncio
import datetime as dt
from typing import Any, Dict, List, Optional

import httpx

from router.backend.utils.config import (
    AT_VEHICLES_RPS,
    AT_VALUATIONS_RPS,
    HTTP_TOTAL_RETRIES,
    HTTP_BACKOFF_BASE,
)
from router.backend.utils.rate_limiter import AsyncTokenBucket
from router.backend.utils.lambda_http import get_http_client


class AutoTraderClient:
    """
    Optimized AutoTrader API client for high-throughput Lambda execution.

    Endpoints:
      - POST {base_url}/authenticate
      - GET  {base_url}/vehicles
      - POST {base_url}/valuations?advertiserId=...

    Optimizations:
      - Parallel-safe token caching with lock
      - Efficient retry strategy
      - Minimal memory allocation per request
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

        # Token cache with thread-safe access
        self._token: Optional[str] = None
        self._token_expiry: Optional[dt.datetime] = None
        self._token_lock = asyncio.Lock()

    # ---------- Optimized Retry Logic ----------

    async def _retry(self, coro_fn, *args, **kwargs):
        """
        Retry with exponential backoff for transient failures.
        Optimized: faster initial backoff, max 3 retries total.
        """
        backoff = HTTP_BACKOFF_BASE
        last_exception = None
        
        for attempt in range(HTTP_TOTAL_RETRIES + 1):
            try:
                return await coro_fn(*args, **kwargs)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
                last_exception = e
                if attempt >= HTTP_TOTAL_RETRIES:
                    raise
                await asyncio.sleep(backoff)
                backoff *= 2
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                # Retry only transient server errors and rate limits
                if status in (429, 500, 502, 503, 504) and attempt < HTTP_TOTAL_RETRIES:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise
        
        # Should never reach here, but handle gracefully
        if last_exception:
            raise last_exception

    # ---------- Token Management ----------

    async def _authenticate(self) -> str:
        """Authenticate and cache token with 15min expiry."""
        client = await get_http_client()
        url = f"{self.base_url}/authenticate"

        async def _do():
            r = await client.post(url, data={"key": self.key, "secret": self.secret})
            r.raise_for_status()
            return r.json()

        data = await self._retry(_do)
        token = data.get("access_token")
        if not token:
            raise ValueError("No access_token in auth response")
        
        # Cache with 15min expiry (conservative to avoid edge cases)
        expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=15)
        
        async with self._token_lock:
            self._token = token
            self._token_expiry = expiry
        
        return token

    async def _get_token(self) -> str:
        """Get cached token or refresh if expired."""
        # Fast path: check without lock
        if self._token and self._token_expiry:
            if dt.datetime.now(dt.timezone.utc) < self._token_expiry:
                return self._token
        
        # Slow path: refresh token (outside lock to avoid blocking)
        return await self._authenticate()

    # ---------- API Calls ----------

    async def vehicles(self, vrm: str, mileage_miles: int) -> Dict[str, Any]:
        """
        Fetch vehicle details, metrics, and features.
        
        Returns dict with keys:
          at_success, at_make, at_model, at_derivative, at_derivative_id,
          at_first_reg_date, at_rating, at_days_to_sell, at_market_condition,
          at_factory_features, at_response_code, at_error (if failed)
        """
        await self._veh_rl.acquire()
        client = await get_http_client()
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
            
            # Parse response with safe dict access
            vehicle = data.get("vehicle") or {}
            metrics = ((data.get("vehicleMetrics") or {}).get("retail")) or {}
            features = data.get("features") or []
            
            # Extract factory-fitted features
            factory_features = [
                f.get("name", "") 
                for f in features 
                if f.get("factoryFitted")
            ]

            return {
                "at_success": True,
                "at_make": vehicle.get("make", ""),
                "at_model": vehicle.get("model", ""),
                "at_derivative": vehicle.get("derivative", ""),
                "at_derivative_id": vehicle.get("derivativeId", ""),
                "at_first_reg_date": vehicle.get("firstRegistrationDate", ""),
                "at_rating": (metrics.get("rating") or {}).get("value", ""),
                "at_days_to_sell": (metrics.get("daysToSell") or {}).get("value", ""),
                "at_market_condition": (metrics.get("marketCondition") or {}).get("value", ""),
                "at_factory_features": ", ".join(factory_features),
                "at_response_code": resp.status_code,
            }
        except httpx.HTTPStatusError as e:
            # 404 = vehicle not found (expected for some VRMs)
            return {
                "at_success": False,
                "at_error": f"Status {e.response.status_code}",
                "at_response_code": e.response.status_code,
            }
        except Exception as e:
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
        """
        Fetch retail valuation for a specific derivative.
        
        Returns dict with keys:
          at_valuation, at_valuation_success
        """
        if not derivative_id:
            return {
                "at_valuation": "No derivative ID",
                "at_valuation_success": False
            }

        await self._val_rl.acquire()
        client = await get_http_client()
        token = await self._get_token()

        url = f"{self.base_url}/valuations"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"advertiserId": self.advertiser_id}
        
        # Limit to 10 features as per API spec
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
            
            # Navigate to retail valuation
            valuation = (
                ((data.get("valuations") or {}).get("retail")) or {}
            ).get("amountGBP", "")
            
            return {
                "at_valuation": valuation,
                "at_valuation_success": True
            }
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            # 400/404 are non-fatal (invalid derivative, etc.)
            return {
                "at_valuation": f"Error {status}",
                "at_valuation_success": False
            }
        except Exception as e:
            return {
                "at_valuation": f"Error: {str(e)}",
                "at_valuation_success": False
            }