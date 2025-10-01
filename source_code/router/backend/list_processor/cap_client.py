# FILE: source_code/router/backend/list_processor/cap_client.py

import asyncio
from typing import Any, Dict, Optional
import xml.etree.ElementTree as ET

import httpx

from router.backend.utils.config import CAP_RPS, HTTP_TOTAL_RETRIES, HTTP_BACKOFF_BASE
from router.backend.utils.rate_limiter import AsyncTokenBucket
from router.backend.utils.lambda_http import get_http_client


class CAPClient:
    """
    Optimized CAP VRM client for high-throughput processing.

    Endpoint:
      POST https://soap.cap.co.uk/vrm/capvrm.asmx/VRMValuation

    Returns parsed vehicle and valuation data from XML SOAP response.
    
    Optimizations:
      - Efficient XML parsing
      - Minimal retry overhead
      - Fast path for common responses
    """

    def __init__(
        self,
        subscriber_id: str,
        password: str,
        base_url: str = "https://soap.cap.co.uk/vrm/capvrm.asmx/VRMValuation"
    ) -> None:
        self.subscriber_id = subscriber_id
        self.password = password
        self.url = base_url
        self._rl = AsyncTokenBucket(CAP_RPS)
        
        # Pre-compile namespace for faster XML parsing
        self._ns = {"ns": "https://soap.cap.co.uk/vrm"}

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
                # Retry transient errors only
                if status in (429, 500, 502, 503, 504) and attempt < HTTP_TOTAL_RETRIES:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                raise
        
        if last_exception:
            raise last_exception

    # ---------- XML Parsing ----------

    def _get_xml_text(self, root: ET.Element, xpath: str) -> str:
        """Fast XML text extraction with pre-compiled namespace."""
        el = root.find(xpath, self._ns)
        return el.text if el is not None and el.text else ""

    # ---------- API Call ----------

    async def vrm(self, vrm: str, mileage: int) -> Dict[str, Any]:
        """
        Perform VRM lookup and valuation.
        
        Returns dict with keys:
          cap_success, cap_capid, cap_capcode, cap_make, cap_model,
          cap_derivative, cap_clean, cap_retail, cap_reg_date,
          cap_response_code, cap_error (if failed)
        """
        await self._rl.acquire()
        client = await get_http_client()

        # Prepare form data
        data = {
            "SubscriberID": self.subscriber_id,
            "Password": self.password,
            "VRM": vrm,
            "Mileage": str(int(mileage) if mileage else 0),
            "StandardEquipmentRequired": "false",
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        async def _do():
            r = await client.post(self.url, data=data, headers=headers)
            r.raise_for_status()
            return r

        try:
            resp = await self._retry(_do)
            text = resp.text
            
            # Parse XML response
            root = ET.fromstring(text)

            # Extract all fields efficiently
            return {
                "cap_success": True,
                "cap_capid": self._get_xml_text(root, ".//ns:VRMLookup/ns:CAPID"),
                "cap_capcode": self._get_xml_text(root, ".//ns:VRMLookup/ns:CAPcode"),
                "cap_make": self._get_xml_text(root, ".//ns:VRMLookup/ns:CAPMan"),
                "cap_model": self._get_xml_text(root, ".//ns:VRMLookup/ns:CAPMod"),
                "cap_derivative": self._get_xml_text(root, ".//ns:VRMLookup/ns:CAPDer"),
                "cap_clean": self._get_xml_text(root, ".//ns:Valuation/ns:Clean"),
                "cap_retail": self._get_xml_text(root, ".//ns:Valuation/ns:Retail"),
                "cap_reg_date": self._get_xml_text(root, ".//ns:VRMLookup/ns:RegisteredDate"),
                "cap_response_code": resp.status_code,
            }
        except httpx.HTTPStatusError as e:
            # HTTP errors
            return {
                "cap_success": False,
                "cap_error": f"Status {e.response.status_code}",
                "cap_response_code": e.response.status_code,
            }
        except ET.ParseError as e:
            # XML parsing errors
            return {
                "cap_success": False,
                "cap_error": f"XML parse error: {str(e)}",
                "cap_response_code": 0,
            }
        except Exception as e:
            # Other errors
            return {
                "cap_success": False,
                "cap_error": str(e),
                "cap_response_code": 0,
            }