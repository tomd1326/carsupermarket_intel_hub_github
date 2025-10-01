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
    High-throughput CAP VRM client aligned with test_fixed.py.

    Endpoint:
      - POST https://soap.cap.co.uk/vrm/capvrm.asmx/VRMValuation
        Form fields:
          SubscriberID, Password, VRM, Mileage, StandardEquipmentRequired

    Returns a dict mirroring test_fixed.py keys:
      cap_success, cap_capid, cap_capcode, cap_make, cap_model, cap_derivative,
      cap_clean, cap_retail, cap_reg_date, cap_response_code, cap_error
    """

    def __init__(self, subscriber_id: str, password: str, base_url: str = "https://soap.cap.co.uk/vrm/capvrm.asmx/VRMValuation") -> None:
        self.subscriber_id = subscriber_id
        self.password = password
        self.url = base_url
        self._rl = AsyncTokenBucket(CAP_RPS)

    async def _retry(self, coro_fn, *args, **kwargs):
        backoff = HTTP_BACKOFF_BASE
        for attempt in range(HTTP_TOTAL_RETRIES + 1):
            try:
                return await coro_fn(*args, **kwargs)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
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

    @staticmethod
    def _get_xml_text(root: ET.Element, xpath: str, ns: Dict[str, str]) -> str:
        el = root.find(xpath, ns)
        return el.text if el is not None else ""

    async def vrm(self, vrm: str, mileage: int) -> Dict[str, Any]:
        """
        Perform VRM valuation call and parse response.
        """
        await self._rl.acquire()
        client = await get_http_client()

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
            root = ET.fromstring(text)
            ns = {"ns": "https://soap.cap.co.uk/vrm"}

            return {
                "cap_success": True,
                "cap_capid": self._get_xml_text(root, ".//ns:VRMLookup/ns:CAPID", ns),
                "cap_capcode": self._get_xml_text(root, ".//ns:VRMLookup/ns:CAPcode", ns),
                "cap_make": self._get_xml_text(root, ".//ns:VRMLookup/ns:CAPMan", ns),
                "cap_model": self._get_xml_text(root, ".//ns:VRMLookup/ns:CAPMod", ns),
                "cap_derivative": self._get_xml_text(root, ".//ns:VRMLookup/ns:CAPDer", ns),
                "cap_clean": self._get_xml_text(root, ".//ns:Valuation/ns:Clean", ns),
                "cap_retail": self._get_xml_text(root, ".//ns:Valuation/ns:Retail", ns),
                "cap_reg_date": self._get_xml_text(root, ".//ns:VRMLookup/ns:RegisteredDate", ns),
                "cap_response_code": resp.status_code,
            }
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            # Log-like behavior aligns to test script semantics
            return {
                "cap_success": False,
                "cap_error": f"Status {status}",
                "cap_response_code": status,
            }
        except Exception as e:
            return {
                "cap_success": False,
                "cap_error": str(e),
                "cap_response_code": 0,
            }
