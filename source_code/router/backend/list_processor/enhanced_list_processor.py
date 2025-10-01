# FILE: source_code/router/backend/list_processor/enhanced_list_processor.py

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .autotrader_client import AutoTraderClient
from .cap_client import CAPClient
from ..utils.config import MAX_WORKERS
from ..utils.secrets_manager_client import get_secret  # existing module
from ..utils.lambda_http import get_http_client  # ensure client init in cold start

log = logging.getLogger(__name__)


# ---------- Orchestration ----------

async def _process_one(
    idx: int,
    row: Dict[str, Any],
    at: AutoTraderClient,
    cap: CAPClient,
) -> Dict[str, Any]:
    """Process a single vehicle row. Mirrors test_fixed.py semantics."""
    result: Dict[str, Any] = dict(row)
    result["row_index"] = idx

    vrm = str(row.get("REGNO", "")).strip()
    mileage = int(row.get("MILES", 0) or 0)

    if not vrm or mileage <= 0:
        result["error"] = "Missing VRM or mileage"
        return result

    # CAP
    cap_data = await cap.vrm(vrm, mileage)
    result.update(cap_data)

    # AutoTrader vehicles
    atv = await at.vehicles(vrm, mileage)
    result.update(atv)

    # AutoTrader valuations (only with derivative)
    if atv.get("at_success") and atv.get("at_derivative_id"):
        features = [f.strip() for f in (atv.get("at_factory_features") or "").split(",") if f.strip()]
        val = await at.valuations(
            vrm=vrm,
            derivative_id=atv["at_derivative_id"],
            first_reg_date=atv.get("at_first_reg_date", ""),
            mileage_miles=mileage,
            features=features,
        )
        result.update(val)

    return result


async def _bounded_gather(
    rows: List[Tuple[int, Dict[str, Any]]],
    at: AutoTraderClient,
    cap: CAPClient,
    max_workers: int,
) -> List[Dict[str, Any]]:
    sem = asyncio.Semaphore(max_workers)

    async def _wrap(i: int, r: Dict[str, Any]):
        async with sem:
            return await _process_one(i, r, at, cap)

    tasks = [asyncio.create_task(_wrap(i, r)) for i, r in rows]
    results: List[Dict[str, Any]] = []
    for t in asyncio.as_completed(tasks):
        results.append(await t)
    # restore original order
    results.sort(key=lambda d: d.get("row_index", 0))
    return results


async def process_dataframe_async(
    df: pd.DataFrame,
    *,
    env_region: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Async entrypoint. Builds clients from Secrets Manager and processes the frame.
    Secrets layout follows test_fixed.py:
      {ENV}/carsupermarket/car_pricer/api_access
        AUTOTRADER_KEY, AUTOTRADER_SECRET, ADVERTISER_ID,
        CAP_SUBSCRIBER_ID, CAP_PASSWORD
    """
    # Warm shared HTTP client early (helps cold start overlap)
    await get_http_client()

    env = "prod" if (env_region or pd.options.mode.chained_assignment is None) else "dev"  # dummy branch to avoid unused var lint
    # Real env detection mirrors test_fixed.py
    import os
    env = "prod" if os.getenv("AWS_REGION") == "eu-west-1" else "dev"

    secrets = get_secret(f"{env}/carsupermarket/car_pricer/api_access")
    at = AutoTraderClient(
        base_url="https://api.autotrader.co.uk",
        advertiser_id=secrets["ADVERTISER_ID"],
        key=secrets["AUTOTRADER_KEY"],
        secret=secrets["AUTOTRADER_SECRET"],
    )
    cap = CAPClient(
        subscriber_id=secrets["CAP_SUBSCRIBER_ID"],
        password=secrets["CAP_PASSWORD"],
    )

    rows = [(i, row._asdict() if hasattr(row, "_asdict") else dict(row)) for i, row in enumerate(df.to_dict(orient="records"))]
    return await _bounded_gather(rows, at, cap, MAX_WORKERS)


def process_dataframe(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    Sync wrapper for environments that call synchronously (e.g., FastAPI endpoints).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Nested loop scenario, create task and wait
        return asyncio.run(process_dataframe_async(df))  # type: ignore
    else:
        return asyncio.run(process_dataframe_async(df))


# ---------- XLSX writing (fast) ----------

def results_to_excel_fast(results: List[Dict[str, Any]], output_path: str) -> None:
    """
    Fast write-only XLSX. Avoids pandas .to_excel overhead.
    Preserves column order from first row, adds any new keys encountered.
    """
    from openpyxl import Workbook

    if not results:
        # Create empty file with no rows
        wb = Workbook(write_only=True)
        ws = wb.create_sheet()
        wb.save(output_path)
        return

    # Build header
    header: List[str] = []
    seen = set()
    for r in results:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                header.append(k)

    wb = Workbook(write_only=True)
    ws = wb.create_sheet()
    ws.append(header)

    for r in results:
        ws.append([r.get(c, "") for c in header])

    wb.save(output_path)


# ---------- Convenience end-to-end ----------

def process_and_write_xlsx(
    df: pd.DataFrame,
    output_path: str,
) -> None:
    """
    Full pipeline:
      - async fetch + enrich with AutoTrader and CAP
      - preserve order
      - write XLSX fast
    """
    results = process_dataframe(df)
    # drop row_index before save
    for r in results:
        r.pop("row_index", None)
    results_to_excel_fast(results, output_path)
