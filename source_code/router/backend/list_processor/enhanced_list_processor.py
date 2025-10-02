# FILE: source_code/router/backend/list_processor/enhanced_list_processor.py

import os
import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple, Callable
import datetime

import pandas as pd

from .autotrader_client import AutoTraderClient
from .cap_client import CAPClient
from .columns_mapper import get_columns_mapper, BaseColumnsMapper
from ..utils.config import MAX_WORKERS, PROGRESS_UPDATE_INTERVAL
from ..utils.secrets_manager_client import get_secret
from ..utils.lambda_http import get_http_client

log = logging.getLogger(__name__)

# ---------- Output Column Configuration ----------

# EXACT API enrichment columns to output (in exact order)
API_COLUMNS = [
    # AutoTrader columns
    "at_description",
    "at_vrm",
    "at_mileage",
    "at_valuations_retail_valuation",
    "at_rating",
    "at_original_days_to_sell",
    "at_market_condition",
    "at_factory_fitted_feature_names",
    "at_first_registration_date",
    # CAP columns
    "cap_capid",
    "cap_capcode",
    "cap_manufacturer",
    "cap_model",
    "cap_derivative",
    "cap_rounded_mileage",
    "cap_live_clean",
    "cap_live_retail",
    "cap_monthly_clean",
    "cap_monthly_retail",
    # Snowflake (7 columns: 5 CAPID-specific + 2 market-wide)
    "capid_stock",
    "capid_sales",
    "capid_sales_7_days",
    "capid_avg_days_in_stock_acknowledged",
    "capid_sales_percentage",
    "total_sales",
    "total_stock",
    # Calculated columns (added after Snowflake enrichment in processing.py)
    "buying_target",
    "total_stock_target",
    "capid_ideal_stock",
    "capid_requirement",
]

# ---------- Helpers ----------

def _round_mileage(mileage: int) -> int:
    """Round mileage to nearest 1000 (minimum 1000)."""
    if mileage < 1000:
        return 1000
    return round(mileage / 1000) * 1000


def _is_van(row: Dict[str, Any], supplier_name: str) -> bool:
    """
    Check if this vehicle is a van.

    Rules:
    - 1link: Check "Body Style" column for "Van"
    """
    if supplier_name.lower() == "1link":
        body_style = row.get("Body Style", "").strip().lower()
        return body_style == "van"
    return False


async def _prewarm_endpoints() -> None:
    """
    Fire a couple of tiny requests to establish DNS/TLS and warm the httpx pool.
    All errors ignored by design.
    """
    try:
        client = get_http_client()
        tasks = [
            client.get("https://soap.cap.co.uk/vrm/capvrm.asmx", timeout=5.0),
            client.get("https://soap.cap.co.uk/usedvalueslive/capusedvalueslive.asmx", timeout=5.0),
        ]
        # AutoTrader root often 403s but still warms DNS; ignore failures
        tasks.append(client.get("https://api.autotrader.co.uk/", timeout=5.0))
        await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:
        pass


def _effective_workers(supplier_name: str) -> int:
    """
    Choose concurrency per supplier.
    - Motability (CAPID-heavy) benefits from higher concurrency to fully utilize CAP.
    - Others default to MAX_WORKERS (100) which matches Auto Trader 100 RPS.
    You can override with env MAX_WORKERS_CAP (default 150).
    """
    name = (supplier_name or "").lower()
    if "motability" in name:
        return int(os.getenv("MAX_WORKERS_CAP", "150"))
    return MAX_WORKERS


# ---------- Vehicle processing ----------

async def _process_one_parallel(
    idx: int,
    row: Dict[str, Any],
    at: AutoTraderClient,
    cap: CAPClient,
    mapper: BaseColumnsMapper,
    supplier_name: str,
    *,
    cap_capid_cache: Dict[Tuple[int, str, int], Dict[str, Any]],
    at_vehicles_cache: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Process a single vehicle with PARALLEL CAP + AT calls.

    Always call Auto Trader (vehicles -> valuations).
    CAP rules:
      - If CAPID present: use CAPIDValuation (monthly) + GetUsedLive_IdRegDate (live)
      - Else: VRMValuation (monthly + CAPID/regDate), AND if database=='CAR' call live
    """
    result: Dict[str, Any] = dict(row)
    result["row_index"] = idx

    # Supplier-specific extraction (VRM, mileage, CAPID, registration_date)
    try:
        vehicle = mapper.map_row(row)
        if not vehicle:
            result["error"] = "Mapper returned None"
            return result
    except Exception as e:
        result["error"] = f"Mapper error: {str(e)}"
        log.error(f"Row {idx}: Mapper error: {str(e)}")
        return result

    vrm = vehicle.vrm
    mileage = vehicle.mileage
    capid = vehicle.capid
    registration_date = vehicle.registration_date

    if not vrm or not mileage or mileage <= 0:
        result["error"] = "Missing VRM or mileage"
        return result

    rounded_mileage = _round_mileage(mileage)

    # CAP (skip for vans only)
    if _is_van(row, supplier_name):
        cap_data: Dict[str, Any] = {}
    else:
        if capid and registration_date:
            # Cache on (CAPID, reg_date, rounded_mileage)
            reg_date_str = (
                registration_date.strftime("%Y-%m-%d")
                if isinstance(registration_date, (datetime.date, datetime.datetime))
                else str(registration_date)
            )
            cache_key = (int(capid), reg_date_str, int(rounded_mileage))
            cached = cap_capid_cache.get(cache_key)
            if cached is not None:
                cap_data = dict(cached)
            else:
                cap_data = await cap.capid(int(capid), rounded_mileage, reg_date_str)
                cap_capid_cache[cache_key] = dict(cap_data)
        else:
            # No CAPID in source ⇒ VRM route (includes monthly; may add live inside)
            cap_data = await cap.vrm(vrm, rounded_mileage)

    result.update(cap_data)

    # Auto Trader: vehicles (cache by VRM) then valuations
    atv = at_vehicles_cache.get(vrm)
    if atv is None:
        atv = await at.vehicles(vrm, mileage)
        at_vehicles_cache[vrm] = dict(atv)
    result.update(atv)

    if atv.get("at_success") and atv.get("at_derivative_id"):
        features = [
            f.strip()
            for f in (atv.get("at_factory_features") or "").split(",")
            if f.strip()
        ]
        val = await at.valuations(
            vrm=vrm,
            derivative_id=atv["at_derivative_id"],
            first_reg_date=atv.get("at_first_reg_date", ""),
            mileage_miles=mileage,
            features=features,
        )
        result.update(val)

    return result


async def _process_batch_optimized(
    rows: List[Tuple[int, Dict[str, Any]]],
    at: AutoTraderClient,
    cap: CAPClient,
    mapper: BaseColumnsMapper,
    supplier_name: str,
    max_workers: int,
    progress_callback: Optional[Callable[[int], None]] = None,
) -> List[Dict[str, Any]]:
    """
    Optimized batch processing with controlled concurrency and progress tracking.
    """
    sem = asyncio.Semaphore(max_workers)
    completed_count = 0

    # Per-task caches (memory only, dropped after run)
    cap_capid_cache: Dict[Tuple[int, str, int], Dict[str, Any]] = {}
    at_vehicles_cache: Dict[str, Dict[str, Any]] = {}

    async def _wrap(i: int, r: Dict[str, Any]):
        nonlocal completed_count
        async with sem:
            out = await _process_one_parallel(
                i,
                r,
                at,
                cap,
                mapper,
                supplier_name,
                cap_capid_cache=cap_capid_cache,
                at_vehicles_cache=at_vehicles_cache,
            )
            completed_count += 1
            if progress_callback and completed_count % PROGRESS_UPDATE_INTERVAL == 0:
                progress_callback(completed_count)
            return out

    tasks = [_wrap(i, r) for i, r in rows]
    results = await asyncio.gather(*tasks, return_exceptions=False)

    if progress_callback:
        progress_callback(completed_count)

    # Preserve input order
    results.sort(key=lambda d: d.get("row_index", 0))
    return results


# ---------- Entrypoints ----------

async def process_dataframe_async(
    df: pd.DataFrame,
    supplier_name: str,
    *,
    progress_callback: Optional[Callable[[int], None]] = None,
    env_region: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Async entrypoint with optimized client initialization + progress tracking.
    """
    mapper = get_columns_mapper(supplier_name)
    log.info(f"Using {mapper.__class__.__name__} for supplier '{supplier_name}'")

    # Environment (dev/prod) for secrets
    env = "prod" if os.getenv("AWS_REGION") == "eu-west-1" else "dev"

    # Secrets (sync)
    log.info("Loading API secrets...")
    secrets = get_secret(f"{env}/carsupermarket/car_pricer/api_access")
    log.info("Secrets loaded successfully")

    # Warm up HTTP pools in parallel with client acquisition
    log.info("Initializing HTTP client + prewarm...")
    client = get_http_client()  # thread-local AsyncClient
    await _prewarm_endpoints()
    log.info(f"HTTP client initialized: {client}")

    # API clients
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
    log.info("API clients initialized")

    # Prepare rows
    rows = [(i, row) for i, row in enumerate(df.to_dict(orient="records"))]

    # Dynamic concurrency for workload
    workers = _effective_workers(supplier_name)
    log.info(f"Processing {len(rows)} vehicles with {workers} workers")

    results = await _process_batch_optimized(
        rows,
        at,
        cap,
        mapper,
        supplier_name,
        workers,
        progress_callback=progress_callback,
    )

    log.info(f"Completed processing {len(results)} vehicles")
    return results


def process_dataframe(df: pd.DataFrame, supplier_name: str) -> List[Dict[str, Any]]:
    """
    Sync wrapper — ALWAYS create a fresh event loop (thread-safe).
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(process_dataframe_async(df, supplier_name))
    finally:
        try:
            # Cancel pending & close loop quickly (no lingering executors)
            pending = asyncio.all_tasks(loop)
            for t in pending:
                t.cancel()
        except Exception:
            pass
        loop.close()


# ---------- XLSX writing with correct column order ----------

def write_results_to_xlsx(
    merged_df: pd.DataFrame,
    output_path: str,
    mapper: BaseColumnsMapper,  # Kept for compatibility
) -> None:
    """
    Write DataFrame to XLSX with EXACT column order: [SOURCE COLUMNS] + [API COLUMNS].
    """
    from openpyxl import Workbook

    if merged_df.empty:
        wb = Workbook(write_only=True)
        ws = wb.create_sheet()
        wb.save(output_path)
        return

    # Internal fields not to export
    internal_fields = {
        "row_index",
        "error",
        "at_success",
        "at_error",
        "at_response_code",
        "at_make",
        "at_model",
        "at_derivative_id",
        "cap_success",
        "cap_error",
        "cap_response_code",
        "cap_registration_date",
        "cap_database",
    }

    api_set = set(API_COLUMNS)
    all_cols = merged_df.columns.tolist()
    source_cols = [c for c in all_cols if c not in internal_fields and c not in api_set]

    final_cols = [c for c in source_cols + API_COLUMNS if c in merged_df.columns]
    out_df = merged_df[final_cols].copy()

    log.info(
        f"Writing XLSX with {len(out_df)} rows and {len(final_cols)} columns "
        f"({len(source_cols)} source + {len([c for c in API_COLUMNS if c in merged_df.columns])} API)"
    )

    wb = Workbook(write_only=True)
    ws = wb.create_sheet()
    ws.append(final_cols)
    for _, r in out_df.iterrows():
        ws.append(r.tolist())
    wb.save(output_path)
    log.info(f"Wrote XLSX to {output_path}")