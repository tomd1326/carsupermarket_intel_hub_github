# FILE: source_code/router/backend/list_processor/cap_client.py

import asyncio
from typing import Any, Dict, Optional
import xml.etree.ElementTree as ET
import datetime
import logging

import httpx

from router.backend.utils.config import CAP_RPS, HTTP_TOTAL_RETRIES, HTTP_BACKOFF_BASE
from router.backend.utils.rate_limiter import AsyncTokenBucket
from router.backend.utils.lambda_http import get_http_client

logger = logging.getLogger(__name__)


class CAPClient:
    """
    CAP API client - ALL mileage values rounded to nearest 1000 (minimum 1000).
    
    Two processing paths:
    1. CAPID known from source → live + monthly valuations
    2. VRM only → VRM lookup, get CAPID, then live valuation if database=='CAR'
    """

    def __init__(
        self,
        subscriber_id: str,
        password: str,
    ) -> None:
        self.subscriber_id = subscriber_id
        self.password = password
        
        # API endpoints
        self.vrm_url = "https://soap.cap.co.uk/vrm/capvrm.asmx/VRMValuation"
        self.capid_monthly_url = "https://soap.cap.co.uk/vrm/capvrm.asmx/CAPIDValuation"
        self.capid_live_url = "https://soap.cap.co.uk/usedvalueslive/capusedvalueslive.asmx/GetUsedLive_IdRegDate"
        
        self.database = "CAR"
        self._rl = AsyncTokenBucket(CAP_RPS)
        
        # Pre-compile namespaces
        self._ns_vrm = {"ns": "https://soap.cap.co.uk/vrm"}
        self._ns_live = {"ns": "https://soap.cap.co.uk/usedvalueslive"}

    # ---------- Retry Logic ----------

    async def _retry(self, coro_fn, *args, **kwargs):
        """Optimized retry with exponential backoff."""
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
                if status in (429, 500, 502, 503, 504) and attempt < HTTP_TOTAL_RETRIES:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise
        
        if last_exception:
            raise last_exception

    # ---------- Helper Methods ----------

    @staticmethod
    def _round_to_nearest_thousand(mileage: int) -> int:
        """Round mileage to nearest 1000 (minimum 1000)."""
        if mileage < 1000:
            return 1000
        return round(mileage / 1000) * 1000

    @staticmethod
    def _round_to_nearest_25(value: float) -> int:
        """Round value to nearest 25."""
        return round(value / 25) * 25

    @staticmethod
    def _convert_date_format(date_str: str) -> Optional[datetime.date]:
        """Convert DD/MM/YYYY to date object."""
        try:
            return datetime.datetime.strptime(date_str, '%d/%m/%Y').date()
        except ValueError:
            return None

    def _get_valuations(self, mileage: int, valuations: list) -> tuple:
        """Interpolate/extrapolate valuation based on mileage bands."""
        if not valuations:
            return None, None
            
        valuations.sort()
        
        # Below first band
        if mileage < valuations[0][0]:
            return valuations[0][1], valuations[0][2]
        
        # Within bands
        for i in range(len(valuations) - 1):
            if valuations[i][0] <= mileage < valuations[i + 1][0]:
                lower_mileage, lower_retail, lower_clean = valuations[i]
                upper_mileage, upper_retail, upper_clean = valuations[i + 1]
                retail_value = lower_retail + (mileage - lower_mileage) * (upper_retail - lower_retail) / (upper_mileage - lower_mileage)
                clean_value = lower_clean + (mileage - lower_mileage) * (upper_clean - lower_clean) / (upper_mileage - lower_mileage)
                return retail_value, clean_value
        
        # Above highest band - extrapolate
        lower_mileage, lower_retail, lower_clean = valuations[-2]
        upper_mileage, upper_retail, upper_clean = valuations[-1]
        mileage_diff = upper_mileage - lower_mileage
        retail_diff = upper_retail - lower_retail
        clean_diff = upper_clean - lower_clean
        extrapolated_retail = upper_retail + ((mileage - upper_mileage) / mileage_diff) * retail_diff
        extrapolated_clean = upper_clean + ((mileage - upper_mileage) / mileage_diff) * clean_diff
        return extrapolated_retail, extrapolated_clean

    # ---------- Internal API Methods ----------

    async def _fetch_live_valuation(self, capid: int, rounded_mileage: int, reg_date: str) -> list:
        """Fetch live valuation bands."""
        await self._rl.acquire()
        client = await get_http_client()

        payload = {
            'subscriberId': self.subscriber_id,
            'password': self.password,
            'database': self.database,
            'capid': str(capid),
            'valuationDate': datetime.datetime.now().strftime('%Y-%m-%d'),
            'regDate': reg_date,
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

            valuations = root.findall('.//ns:Valuations/ns:Valuation', ns)
            valuation_list = []
            for valuation in valuations:
                mile_elem = valuation.find('ns:Mileage', ns)
                retail_elem = valuation.find('ns:Retail', ns)
                clean_elem = valuation.find('ns:Clean', ns)
                
                if mile_elem is not None and retail_elem is not None and clean_elem is not None:
                    try:
                        mile = int(mile_elem.text) * 1000
                        retail = int(retail_elem.text)
                        clean = int(clean_elem.text)
                        valuation_list.append((mile, retail, clean))
                    except:
                        continue
            
            return valuation_list
            
        except Exception as e:
            logger.error(f"Live valuation failed for CAPID {capid}: {str(e)}")
            return []

    async def _fetch_monthly_valuation(self, capid: int, rounded_mileage: int, reg_date: str) -> dict:
        """Fetch monthly valuation."""
        await self._rl.acquire()
        client = await get_http_client()

        payload = {
            'SubscriberID': self.subscriber_id,
            'Password': self.password,
            'Database': self.database,
            'CAPID': str(capid),
            'RegisteredDate': reg_date,
            'Mileage': str(rounded_mileage),  # ROUNDED mileage
            'StandardEquipmentRequired': 'false'
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        async def _do():
            r = await client.post(self.capid_monthly_url, data=payload, headers=headers)
            r.raise_for_status()
            return r

        try:
            resp = await self._retry(_do)
            root = ET.fromstring(resp.text)
            ns = self._ns_vrm

            success = root.find('.//ns:Success', ns)
            if success is not None and success.text == 'true':
                capid_lookup = root.find('.//ns:CAPIDLookup', ns)
                valuation = root.find('.//ns:Valuation', ns)
                
                if capid_lookup is not None and valuation is not None:
                    val_success = valuation.find('ns:Success', ns)
                    if val_success is not None and val_success.text == 'true':
                        retail_elem = valuation.find('ns:Retail', ns)
                        clean_elem = valuation.find('ns:Clean', ns)
                        
                        try:
                            retail = int(retail_elem.text) if retail_elem is not None and retail_elem.text else ""
                        except:
                            retail = ""
                        try:
                            clean = int(clean_elem.text) if clean_elem is not None and clean_elem.text else ""
                        except:
                            clean = ""
                        
                        return {
                            'Retail': retail,
                            'Clean': clean,
                            'CAPMan': capid_lookup.find('ns:CAPMan', ns).text if capid_lookup.find('ns:CAPMan', ns) is not None else "",
                            'CAPMod': capid_lookup.find('ns:CAPMod', ns).text if capid_lookup.find('ns:CAPMod', ns) is not None else "",
                            'CAPDer': capid_lookup.find('ns:CAPDer', ns).text if capid_lookup.find('ns:CAPDer', ns) is not None else "",
                            'CAPcode': capid_lookup.find('ns:CAPcode', ns).text if capid_lookup.find('ns:CAPcode', ns) is not None else ""
                        }
            
            return {}
            
        except Exception as e:
            logger.error(f"Monthly valuation failed for CAPID {capid}: {str(e)}")
            return {}

    # ---------- Public API Methods ----------

    async def vrm(self, vrm: str, mileage: int) -> Dict[str, Any]:
        """
        VRM-based valuation - for vehicles without CAPID.
        
        IMPORTANT: Sends ROUNDED mileage to API (nearest 1000, minimum 1000).
        
        Flow:
        1. Call VRM endpoint with ROUNDED mileage
        2. If database=='CAR' and got CAPID + reg_date → call live valuation
        3. Return all data with live values if available
        """
        # ROUND MILEAGE BEFORE API CALL
        rounded_mileage = self._round_to_nearest_thousand(mileage)
        
        await self._rl.acquire()
        client = await get_http_client()

        payload = {
            "SubscriberID": self.subscriber_id,
            "Password": self.password,
            "VRM": vrm,
            "Mileage": str(rounded_mileage),  # ROUNDED mileage sent to API
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

            # Check if vehicle found
            vehicle_found = root.find('.//ns:VRMLookup/ns:VehicleFound', ns)
            if vehicle_found is not None and vehicle_found.text == 'false':
                fail_reason = root.find('.//ns:VRMLookup/ns:FailReason', ns)
                return {
                    "cap_success": False,
                    "cap_error": fail_reason.text if fail_reason is not None else "Vehicle not found",
                }

            # Extract VRM data
            database = root.find('.//ns:VRMLookup/ns:Database', ns)
            capid = root.find('.//ns:VRMLookup/ns:CAPID', ns)
            clean = root.find('.//ns:Valuation/ns:Clean', ns)
            retail = root.find('.//ns:Valuation/ns:Retail', ns)
            capman = root.find('.//ns:VRMLookup/ns:CAPMan', ns)
            capmod = root.find('.//ns:VRMLookup/ns:CAPMod', ns)
            capder = root.find('.//ns:VRMLookup/ns:CAPDer', ns)
            capcode = root.find('.//ns:VRMLookup/ns:CAPcode', ns)
            registered_date = root.find('.//ns:VRMLookup/ns:RegisteredDate', ns)

            # Parse registration date
            reg_date_formatted = ""
            reg_date_obj = None
            if registered_date is not None and registered_date.text:
                try:
                    reg_date_obj = datetime.datetime.strptime(registered_date.text, '%Y-%m-%dT%H:%M:%S')
                    reg_date_formatted = reg_date_obj.strftime('%d/%m/%Y')
                except:
                    pass

            # Convert monthly prices to int
            try:
                monthly_clean = int(clean.text) if clean is not None and clean.text else ""
            except:
                monthly_clean = ""
                
            try:
                monthly_retail = int(retail.text) if retail is not None and retail.text else ""
            except:
                monthly_retail = ""

            # Build base result
            result = {
                "cap_success": True,
                "cap_database": database.text if database is not None else "",
                "cap_capid": capid.text if capid is not None else "",
                "cap_capcode": capcode.text if capcode is not None else "",
                "cap_manufacturer": capman.text if capman is not None else "",
                "cap_model": capmod.text if capmod is not None else "",
                "cap_derivative": capder.text if capder is not None else "",
                "cap_rounded_mileage": rounded_mileage,  # Return the rounded value used
                "cap_monthly_clean": monthly_clean,
                "cap_monthly_retail": monthly_retail,
                "cap_registration_date": reg_date_formatted,
                "cap_live_clean": "",
                "cap_live_retail": "",
            }

            # CRITICAL: If database=='CAR' and we have CAPID + reg_date → fetch live valuation
            if (database is not None and database.text == 'CAR' and 
                capid is not None and capid.text and 
                reg_date_obj is not None):
                
                try:
                    capid_int = int(capid.text)
                    reg_date_str = reg_date_obj.strftime('%Y-%m-%d')
                    
                    # Fetch live valuation using the SAME rounded mileage
                    live_result = await self._fetch_live_valuation(capid_int, rounded_mileage, reg_date_str)
                    
                    if live_result:
                        current_retail, current_clean = self._get_valuations(rounded_mileage, live_result)
                        if current_retail is not None and current_clean is not None:
                            result["cap_live_retail"] = self._round_to_nearest_25(current_retail)
                            result["cap_live_clean"] = self._round_to_nearest_25(current_clean)
                except Exception as e:
                    logger.error(f"Failed to fetch live valuation after VRM lookup for {vrm}: {str(e)}")

            return result
            
        except Exception as e:
            logger.error(f"VRM lookup failed for {vrm}: {str(e)}")
            return {
                "cap_success": False,
                "cap_error": str(e),
            }

    async def capid(self, capid: int, mileage: int, registration_date: str) -> Dict[str, Any]:
        """
        CAPID-based valuation - when CAPID known from source.
        Calls live and monthly in parallel.
        
        IMPORTANT: Sends ROUNDED mileage to API (nearest 1000, minimum 1000).
        
        Args:
            capid: CAP ID
            mileage: Vehicle mileage (will be rounded before API calls)
            registration_date: Registration date in 'YYYY-MM-DD' format
        """
        # Check registration_date
        if not registration_date:
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

        # ROUND MILEAGE BEFORE API CALLS
        rounded_mileage = self._round_to_nearest_thousand(mileage)

        # Parallel execution
        live_task = asyncio.create_task(self._fetch_live_valuation(capid, rounded_mileage, registration_date))
        monthly_task = asyncio.create_task(self._fetch_monthly_valuation(capid, rounded_mileage, registration_date))

        live_result, monthly_data = await asyncio.gather(live_task, monthly_task)

        # Process live result
        live_retail = ""
        live_clean = ""
        if live_result:
            current_retail, current_clean = self._get_valuations(rounded_mileage, live_result)
            if current_retail is not None and current_clean is not None:
                live_retail = self._round_to_nearest_25(current_retail)
                live_clean = self._round_to_nearest_25(current_clean)

        return {
            "cap_success": True,
            "cap_capid": str(capid),
            "cap_capcode": monthly_data.get('CAPcode', ""),
            "cap_manufacturer": monthly_data.get('CAPMan', ""),
            "cap_model": monthly_data.get('CAPMod', ""),
            "cap_derivative": monthly_data.get('CAPDer', ""),
            "cap_rounded_mileage": rounded_mileage,  # Return the rounded value used
            "cap_monthly_clean": monthly_data.get('Clean', ""),
            "cap_monthly_retail": monthly_data.get('Retail', ""),
            "cap_live_clean": live_clean,
            "cap_live_retail": live_retail,
        }
# End of file: source_code/router/backend/list_processor/cap_client.py