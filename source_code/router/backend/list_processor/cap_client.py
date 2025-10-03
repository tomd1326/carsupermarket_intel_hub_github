# FILE: source_code/router/backend/list_processor/cap_client.py

import asyncio
from typing import Any, Dict, Optional, List, Tuple
import xml.etree.ElementTree as ET
import datetime
import logging

import httpx

from utils.config import CAP_RPS, HTTP_TOTAL_RETRIES, HTTP_BACKOFF_BASE
from utils.rate_limiter import AsyncTokenBucket
from utils.lambda_http import get_http_client

logger = logging.getLogger(__name__)


class CAPClient:
    """
    CAP API client.

    Transport:
      - Use HTTP POST (application/x-www-form-urlencoded) for speed and reliability.

    Endpoints:
      - VRMValuation (monthly via VRM + CAPID/regDate discovery)
      - CAPIDValuation (monthly via CAPID) — expects RegisteredDate in yyyy-MM-dd format
      - GetUsedLive_IdRegDate (live valuations; returns multiple mileage bands)

    Rules:
      - When CAPID is present from source: call CAPIDValuation + GetUsedLive_IdRegDate (in parallel)
      - When CAPID not present: call VRMValuation (gets monthly + CAPID/regDate), then live if possible
    """

    def __init__(self, subscriber_id: str, password: str) -> None:
        self.subscriber_id = subscriber_id
        self.password = password

        # HTTP endpoints (method-specific)
        self.vrm_url = "https://soap.cap.co.uk/vrm/capvrm.asmx/VRMValuation"
        self.capid_monthly_url = "https://soap.cap.co.uk/vrm/capvrm.asmx/CAPIDValuation"
        self.capid_live_url = "https://soap.cap.co.uk/usedvalueslive/capusedvalueslive.asmx/GetUsedLive_IdRegDate"

        self.database = "CAR"
        self._rl = AsyncTokenBucket(CAP_RPS)

        # XML namespaces
        self._ns_vrm = {"ns": "https://soap.cap.co.uk/vrm"}
        self._ns_live = {"ns": "https://soap.cap.co.uk/usedvalueslive"}

    # ---------- Retry wrapper ----------

    async def _retry(self, coro_fn, *args, **kwargs):
        """Exponential backoff retry for transient failures."""
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
                # Retry only transient server errors and rate limits
                if e.response.status_code in (429, 500, 502, 503, 504) and attempt < HTTP_TOTAL_RETRIES:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise

        if last_exc:
            raise last_exc

    # ---------- Helpers ----------

    @staticmethod
    def _round_to_nearest_thousand(mileage: int) -> int:
        if mileage < 1000:
            return 1000
        return round(mileage / 1000) * 1000

    @staticmethod
    def _round_to_nearest_25(value: float) -> int:
        return round(value / 25) * 25

    @staticmethod
    def _parse_date_ddmmyyyy(date_str: str) -> Optional[datetime.date]:
        try:
            return datetime.datetime.strptime(date_str, "%d/%m/%Y").date()
        except Exception:
            return None

    def _interpolate_valuations(self, mileage: int, valuations: List[Tuple[int, int, int]]) -> Tuple[Optional[float], Optional[float]]:
        """Return (retail, clean) for specific mileage from banded valuations."""
        if not valuations:
            return None, None
        valuations.sort()

        # Below lowest band
        if mileage < valuations[0][0]:
            return float(valuations[0][1]), float(valuations[0][2])

        # Within bands
        for i in range(len(valuations) - 1):
            ml, r1, c1 = valuations[i]
            mu, r2, c2 = valuations[i + 1]
            if ml <= mileage < mu:
                span = mu - ml
                if span <= 0:
                    return float(r1), float(c1)
                ratio = (mileage - ml) / span
                return r1 + ratio * (r2 - r1), c1 + ratio * (c2 - c1)

        # Above top band: linear extrapolation from last two points
        ml, r1, c1 = valuations[-2]
        mu, r2, c2 = valuations[-1]
        span = mu - ml if (mu - ml) != 0 else 1
        dr = r2 - r1
        dc = c2 - c1
        overshoot = mileage - mu
        return r2 + (overshoot / span) * dr, c2 + (overshoot / span) * dc

    # ---------- Internal API calls ----------

    async def _fetch_live_valuation(self, capid: int, rounded_mileage: int, reg_date_yyyy_mm_dd: str) -> List[Tuple[int, int, int]]:
        """Return list of (mileage, retail, clean) bands for a vehicle."""
        await self._rl.acquire()
        client = get_http_client()

        payload = {
            "subscriberId": str(self.subscriber_id),
            "password": self.password,
            "database": self.database,
            "capid": str(capid),
            "valuationDate": datetime.datetime.now().strftime("%Y-%m-%d"),
            "regDate": reg_date_yyyy_mm_dd,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        async def _do():
            r = await client.post(self.capid_live_url, data=payload, headers=headers)
            r.raise_for_status()
            return r

        try:
            resp = await self._retry(_do)
            root = ET.fromstring(resp.text)
            ns = self._ns_live

            out: List[Tuple[int, int, int]] = []
            for v in root.findall(".//ns:Valuations/ns:Valuation", ns):
                mile = v.find("ns:Mileage", ns)
                ret = v.find("ns:Retail", ns)
                cle = v.find("ns:Clean", ns)
                if mile is None or ret is None or cle is None:
                    continue
                try:
                    # CAP returns mileage bands in thousands (e.g., 10 => 10,000)
                    out.append((int(mile.text) * 1000, int(ret.text), int(cle.text)))
                except Exception:
                    continue
            return out
        except Exception as e:
            logger.exception(f"Live valuation failed for CAPID {capid}")
            return []

    async def _fetch_monthly_capid(self, capid: int, rounded_mileage: int, reg_date_yyyy_mm_dd: str) -> Dict[str, Any]:
        """
        Monthly valuation by CAPID.
        
        IMPORTANT: When using HTTP POST (not SOAP XML), CAPIDValuation expects
        RegisteredDate in yyyy-MM-dd format (ISO 8601), NOT dd/mm/yyyy.
        """
        # Validate registration date presence
        if not reg_date_yyyy_mm_dd:
            logger.warning(f"Missing RegisteredDate for CAPID {capid} — skipping CAPIDValuation")
            return {}

        await self._rl.acquire()
        client = get_http_client()

        payload = {
            "SubscriberID": str(self.subscriber_id),
            "Password": self.password,
            "Database": self.database,
            "CAPID": str(capid),
            "RegisteredDate": reg_date_yyyy_mm_dd,  # yyyy-MM-dd format required
            "Mileage": str(rounded_mileage),
            "StandardEquipmentRequired": "false",
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        async def _do():
            r = await client.post(self.capid_monthly_url, data=payload, headers=headers)
            r.raise_for_status()
            return r

        try:
            resp = await self._retry(_do)
            root = ET.fromstring(resp.text)
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            # Log body for diagnostics
            try:
                body = e.response.text[:500]  # first 500 chars
            except Exception:
                body = "<no response body>"
            logger.exception(
                f"CAPIDValuation HTTP {status} for CAPID {capid}. "
                f"Payload: {payload}. Response body: {body}"
            )
            return {}
        except Exception as e:
            logger.exception(f"Monthly CAPID valuation failed for CAPID {capid}")
            return {}

        # Parse XML response
        ns = self._ns_vrm
        success = root.find(".//ns:Success", ns)
        if not (success is not None and success.text == "true"):
            return {}

        capid_lookup = root.find(".//ns:CAPIDLookup", ns)
        valuation = root.find(".//ns:Valuation", ns)
        if capid_lookup is None or valuation is None:
            return {}

        val_success = valuation.find("ns:Success", ns)
        if not (val_success is not None and val_success.text == "true"):
            return {}

        # Parse monthly values
        def _to_int(node):
            try:
                return int(node.text) if node is not None and node.text else ""
            except Exception:
                return ""

        retail = _to_int(valuation.find("ns:Retail", ns))
        clean = _to_int(valuation.find("ns:Clean", ns))

        return {
            "Retail": retail,
            "Clean": clean,
            "CAPMan": capid_lookup.find("ns:CAPMan", ns).text if capid_lookup.find("ns:CAPMan", ns) is not None else "",
            "CAPMod": capid_lookup.find("ns:CAPMod", ns).text if capid_lookup.find("ns:CAPMod", ns) is not None else "",
            "CAPDer": capid_lookup.find("ns:CAPDer", ns).text if capid_lookup.find("ns:CAPDer", ns) is not None else "",
            "CAPcode": capid_lookup.find("ns:CAPcode", ns).text if capid_lookup.find("ns:CAPcode", ns) is not None else "",
        }

    # ---------- Public API ----------

    async def vrm(self, vrm: str, mileage: int) -> Dict[str, Any]:
        """
        VRM-based monthly valuation. Returns monthly and (if database=='CAR' and CAPID present)
        fetches live valuations using the returned CAPID/regDate.
        """
        rounded_mileage = self._round_to_nearest_thousand(mileage)

        await self._rl.acquire()
        client = get_http_client()

        payload = {
            "SubscriberID": str(self.subscriber_id),
            "Password": self.password,
            "VRM": vrm,
            "Mileage": str(rounded_mileage),
            "StandardEquipmentRequired": "false",
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        async def _do():
            r = await client.post(self.vrm_url, data=payload, headers=headers)
            r.raise_for_status()
            return r

        try:
            resp = await self._retry(_do)
            root = ET.fromstring(resp.text)
            ns = self._ns_vrm

            # Vehicle found?
            vehicle_found = root.find(".//ns:VRMLookup/ns:VehicleFound", ns)
            if vehicle_found is not None and vehicle_found.text == "false":
                fail_reason = root.find(".//ns:VRMLookup/ns:FailReason", ns)
                return {"cap_success": False, "cap_error": fail_reason.text if fail_reason is not None else "Vehicle not found"}

            # Extract fields
            database = root.find(".//ns:VRMLookup/ns:Database", ns)
            capid_node = root.find(".//ns:VRMLookup/ns:CAPID", ns)
            capcode = root.find(".//ns:VRMLookup/ns:CAPcode", ns)
            capman = root.find(".//ns:VRMLookup/ns:CAPMan", ns)
            capmod = root.find(".//ns:VRMLookup/ns:CAPMod", ns)
            capder = root.find(".//ns:VRMLookup/ns:CAPDer", ns)
            registered_date = root.find(".//ns:VRMLookup/ns:RegisteredDate", ns)
            clean = root.find(".//ns:Valuation/ns:Clean", ns)
            retail = root.find(".//ns:Valuation/ns:Retail", ns)

            # Parse monthly values
            def _to_int(node):
                try:
                    return int(node.text) if node is not None and node.text else ""
                except Exception:
                    return ""

            monthly_clean = _to_int(clean)
            monthly_retail = _to_int(retail)

            # Registration date formats
            reg_date_formatted = ""
            reg_date_obj = None
            if registered_date is not None and registered_date.text:
                try:
                    # Example: 2021-06-30T00:00:00
                    reg_date_obj = datetime.datetime.strptime(registered_date.text, "%Y-%m-%dT%H:%M:%S")
                    reg_date_formatted = reg_date_obj.strftime("%d/%m/%Y")
                except Exception:
                    pass

            result = {
                "cap_success": True,
                "cap_database": database.text if database is not None else "",
                "cap_capid": capid_node.text if capid_node is not None else "",
                "cap_capcode": capcode.text if capcode is not None else "",
                "cap_manufacturer": capman.text if capman is not None else "",
                "cap_model": capmod.text if capmod is not None else "",
                "cap_derivative": capder.text if capder is not None else "",
                "cap_rounded_mileage": rounded_mileage,
                "cap_monthly_clean": monthly_clean,
                "cap_monthly_retail": monthly_retail,
                "cap_registration_date": reg_date_formatted,
                "cap_live_clean": "",
                "cap_live_retail": "",
            }

            # Live valuation if we have CAPID + reg date and in CAR DB
            if (
                database is not None and database.text == "CAR" and
                capid_node is not None and capid_node.text and
                reg_date_obj is not None
            ):
                try:
                    capid_int = int(capid_node.text)
                    reg_date_iso = reg_date_obj.strftime("%Y-%m-%d")
                    bands = await self._fetch_live_valuation(capid_int, rounded_mileage, reg_date_iso)
                    if bands:
                        r, c = self._interpolate_valuations(rounded_mileage, bands)
                        if r is not None and c is not None:
                            result["cap_live_retail"] = self._round_to_nearest_25(float(r))
                            result["cap_live_clean"] = self._round_to_nearest_25(float(c))
                except Exception as e:
                    logger.exception(f"Live valuation after VRM lookup failed for {vrm}")

            return result

        except Exception as e:
            logger.exception(f"VRM valuation failed for {vrm}")
            return {"cap_success": False, "cap_error": str(e)}

    async def capid(self, capid: int, mileage: int, registration_date: str) -> Dict[str, Any]:
        """
        CAPID-based path (monthly + live), when CAPID and reg date are known.
        registration_date: 'YYYY-MM-DD' (ISO format)
        
        Both CAPIDValuation and GetUsedLive_IdRegDate expect yyyy-MM-dd format.
        """
        # If reg date missing, still return monthly fields with blanks
        if not registration_date:
            logger.warning(f"Missing registration_date for CAPID {capid} — returning empty monthly/live values")
            return {
                "cap_success": True,
                "cap_capid": str(capid),
                "cap_rounded_mileage": self._round_to_nearest_thousand(mileage),
                "cap_capcode": "",
                "cap_manufacturer": "",
                "cap_model": "",
                "cap_derivative": "",
                "cap_monthly_clean": "",
                "cap_monthly_retail": "",
                "cap_live_clean": "",
                "cap_live_retail": "",
            }

        rounded_mileage = self._round_to_nearest_thousand(mileage)

        # Validate date format (should be yyyy-MM-dd from source)
        try:
            reg_dt = datetime.datetime.strptime(registration_date, "%Y-%m-%d").date()
            reg_date_iso = reg_dt.strftime("%Y-%m-%d")  # Already in correct format
        except Exception:
            logger.warning(f"Invalid registration_date format for CAPID {capid}: {registration_date}")
            return {
                "cap_success": True,
                "cap_capid": str(capid),
                "cap_rounded_mileage": rounded_mileage,
                "cap_capcode": "",
                "cap_manufacturer": "",
                "cap_model": "",
                "cap_derivative": "",
                "cap_monthly_clean": "",
                "cap_monthly_retail": "",
                "cap_live_clean": "",
                "cap_live_retail": "",
            }

        # Monthly (CAPIDValuation) and live (GetUsedLive_IdRegDate) in parallel
        # Both now use yyyy-MM-dd format
        monthly_task = asyncio.create_task(self._fetch_monthly_capid(capid, rounded_mileage, reg_date_iso))
        live_task = asyncio.create_task(self._fetch_live_valuation(capid, rounded_mileage, reg_date_iso))

        monthly_data, live_bands = await asyncio.gather(monthly_task, live_task)

        live_retail = ""
        live_clean = ""
        if live_bands:
            r, c = self._interpolate_valuations(rounded_mileage, live_bands)
            if r is not None and c is not None:
                live_retail = self._round_to_nearest_25(float(r))
                live_clean = self._round_to_nearest_25(float(c))

        return {
            "cap_success": True,
            "cap_capid": str(capid),
            "cap_capcode": monthly_data.get("CAPcode", ""),
            "cap_manufacturer": monthly_data.get("CAPMan", ""),
            "cap_model": monthly_data.get("CAPMod", ""),
            "cap_derivative": monthly_data.get("CAPDer", ""),
            "cap_rounded_mileage": rounded_mileage,
            "cap_monthly_clean": monthly_data.get("Clean", ""),
            "cap_monthly_retail": monthly_data.get("Retail", ""),
            "cap_live_clean": live_clean,
            "cap_live_retail": live_retail,
        }
