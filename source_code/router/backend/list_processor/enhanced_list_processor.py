# FILE: source_code/router/backend/list_processor/enhanced_list_processor.py

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple
import datetime

import pandas as pd

from .autotrader_client import AutoTraderClient
from .cap_client import CAPClient
from ..utils.config import MAX_WORKERS
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
    # Snowflake columns (if present)
    "sf_stock",
    "sf_sales_30d",
    "sf_sales_7d",
]


def _extract_vehicle_data(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract VRM, mileage, CAPID, and registration date from input row.
    Handles various input column name formats (case-insensitive).
    
    Returns dict with: vrm, mileage, capid (optional), registration_date (optional)
    """
    import re
    
    result = {
        "vrm": None,
        "mileage": None,
        "capid": None,
        "registration_date": None
    }
    
    # Create lowercase key mapping
    lowercased_row = {key.lower(): (key, value) for key, value in row.items()}
    
    # Extract VRM
    vrm_keys = ["vrm", "reg", "regno", "registration"]
    for key in vrm_keys:
        if key in lowercased_row:
            original_key, value = lowercased_row[key]
            if value and not pd.isna(value):
                result["vrm"] = str(value).strip()
                break
    
    # Extract mileage
    mileage_keys = ["miles", "mileage", "odo", "odom", "odometer"]
    for key in mileage_keys:
        if key in lowercased_row:
            original_key, value = lowercased_row[key]
            if value and not pd.isna(value):
                if isinstance(value, (int, float)):
                    result["mileage"] = int(value)
                elif isinstance(value, str):
                    # Remove non-digit characters
                    mileage_str = re.sub(r"[^\d]", "", value)
                    if mileage_str:
                        result["mileage"] = int(mileage_str)
                break
    
    # Extract CAPID (optional)
    for key, (original_key, value) in lowercased_row.items():
        if "capid" in key or "cap_id" in key or "cap id" in key:
            if value and not pd.isna(value):
                try:
                    result["capid"] = int(value)
                except (ValueError, TypeError):
                    pass
                break
    
    # Extract registration date (optional)
    date_keys = ["registration_date", "reg_date", "regdate", "date_of_registration", "dfr", "date of registration"]
    for key in date_keys:
        if key in lowercased_row:
            original_key, value = lowercased_row[key]
            if value and not pd.isna(value):
                # Handle pandas Timestamp
                if isinstance(value, pd.Timestamp):
                    result["registration_date"] = value.strftime("%Y-%m-%d")
                    break
                # Handle datetime.date
                elif isinstance(value, datetime.date):
                    result["registration_date"] = value.strftime("%Y-%m-%d")
                    break
                # Handle string dates
                elif isinstance(value, str):
                    for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y-%m-%d", "%Y/%m/%d"]:
                        try:
                            dt = datetime.datetime.strptime(value, fmt)
                            result["registration_date"] = dt.strftime("%Y-%m-%d")
                            break
                        except ValueError:
                            continue
                    if result["registration_date"]:
                        break
    
    return result


def _clean_and_map_row(row: Dict[str, Any], vehicle_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Clean API results and map to ONLY the required output columns.
    Preserves numeric types for Excel.
    """
    # Start with original source columns only (exclude API response fields)
    internal_fields = {
        "row_index", "error", 
        "at_success", "at_error", "at_response_code", "at_make", "at_model", "at_derivative_id",
        "cap_success", "cap_error", "cap_response_code", "cap_registration_date", "cap_database"
    }
    
    source_row = {k: v for k, v in row.items() if k not in internal_fields and not k.startswith("at_") and not k.startswith("cap_") and not k.startswith("sf_")}
    
    # Build clean API column dict - PRESERVE NUMERIC TYPES
    api_data = {}
    
    # AutoTrader columns
    api_data["at_description"] = row.get("at_derivative", "")
    api_data["at_vrm"] = vehicle_data.get("vrm", "")
    api_data["at_mileage"] = vehicle_data.get("mileage", "")
    
    # at_valuation should be numeric
    val = row.get("at_valuation", "")
    if val and val != "":
        try:
            api_data["at_valuations_retail_valuation"] = int(val) if isinstance(val, (int, float, str)) and str(val).replace('.','').isdigit() else val
        except:
            api_data["at_valuations_retail_valuation"] = val
    else:
        api_data["at_valuations_retail_valuation"] = ""
    
    api_data["at_rating"] = row.get("at_rating", "")
    
    # at_days_to_sell should be numeric
    days = row.get("at_days_to_sell", "")
    if days and days != "":
        try:
            api_data["at_original_days_to_sell"] = int(days) if isinstance(days, (int, float, str)) and str(days).replace('.','').isdigit() else days
        except:
            api_data["at_original_days_to_sell"] = days
    else:
        api_data["at_original_days_to_sell"] = ""
    
    api_data["at_market_condition"] = row.get("at_market_condition", "")
    api_data["at_factory_fitted_feature_names"] = row.get("at_factory_features", "")
    api_data["at_first_registration_date"] = row.get("at_first_reg_date", "")
    
    # CAP columns - PRESERVE INTEGERS
    api_data["cap_capid"] = row.get("cap_capid", "")
    api_data["cap_capcode"] = row.get("cap_capcode", "")
    api_data["cap_manufacturer"] = row.get("cap_manufacturer", "")
    api_data["cap_model"] = row.get("cap_model", "")
    api_data["cap_derivative"] = row.get("cap_derivative", "")
    api_data["cap_rounded_mileage"] = row.get("cap_rounded_mileage", "")
    
    # These should already be integers from CAP client
    api_data["cap_live_clean"] = row.get("cap_live_clean", "")
    api_data["cap_live_retail"] = row.get("cap_live_retail", "")
    api_data["cap_monthly_clean"] = row.get("cap_monthly_clean", "")
    api_data["cap_monthly_retail"] = row.get("cap_monthly_retail", "")
    
    # Snowflake columns (if present) - should be integers
    api_data["sf_stock"] = row.get("sf_stock", "")
    api_data["sf_sales_30d"] = row.get("sf_sales_30d", "")
    api_data["sf_sales_7d"] = row.get("sf_sales_7d", "")
    
    # Combine: source columns + API columns
    return {**source_row, **api_data}


# ---------- Optimized Orchestration ----------

async def _process_one_parallel(
    idx: int,
    row: Dict[str, Any],
    at: AutoTraderClient,
    cap: CAPClient,
) -> Dict[str, Any]:
    """
    Process a single vehicle with PARALLEL CAP + AT vehicles calls.
    
    Key optimization: CAP and AT vehicles are independent - run simultaneously.
    Handles both VRM-only and CAPID+VRM scenarios.
    """
    result: Dict[str, Any] = dict(row)
    result["row_index"] = idx

    # Extract vehicle data from input row (handles various column name formats)
    vehicle_data = _extract_vehicle_data(row)
    
    vrm = vehicle_data.get("vrm")
    mileage = vehicle_data.get("mileage")
    capid = vehicle_data.get("capid")
    registration_date = vehicle_data.get("registration_date")

    if not vrm or not mileage or mileage <= 0:
        result["error"] = "Missing VRM or mileage"
        return result

    # Determine which CAP endpoint to use
    if capid and registration_date:
        # Use CAPID endpoint (provides both live and monthly valuations)
        cap_task = asyncio.create_task(cap.capid(capid, mileage, registration_date))
    else:
        # Use VRM endpoint (provides monthly valuation, and live if database=='CAR')
        cap_task = asyncio.create_task(cap.vrm(vrm, mileage))
    
    # AutoTrader always uses VRM
    atv_task = asyncio.create_task(at.vehicles(vrm, mileage))
    
    # **PARALLEL EXECUTION** - Wait for both to complete
    cap_data, atv = await asyncio.gather(cap_task, atv_task)
    
    result.update(cap_data)
    result.update(atv)

    # AutoTrader valuations (sequential - requires derivative from vehicles call)
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
    max_workers: int,
) -> List[Dict[str, Any]]:
    """
    Optimized batch processing with controlled concurrency.
    """
    sem = asyncio.Semaphore(max_workers)

    async def _wrap(i: int, r: Dict[str, Any]):
        async with sem:
            return await _process_one_parallel(i, r, at, cap)

    # Create all tasks at once for better scheduling
    tasks = [_wrap(i, r) for i, r in rows]
    
    # Gather with return_exceptions to prevent one failure from stopping everything
    results = await asyncio.gather(*tasks, return_exceptions=False)
    
    # Sort by original order
    results = sorted(results, key=lambda d: d.get("row_index", 0))
    return results


async def process_dataframe_async(
    df: pd.DataFrame,
    *,
    env_region: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Async entrypoint with optimized client initialization.
    """
    # Pre-warm HTTP client (overlaps with secrets fetch)
    client_task = asyncio.create_task(get_http_client())
    
    # Determine environment
    import os
    env = "prod" if os.getenv("AWS_REGION") == "eu-west-1" else "dev"
    
    # Load secrets (I/O bound - happens while client initializes)
    secrets = get_secret(f"{env}/carsupermarket/car_pricer/api_access")
    
    # Ensure client is ready
    await client_task
    
    # Initialize API clients
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

    # Convert dataframe to list of (index, row_dict) tuples
    rows = [
        (i, row) 
        for i, row in enumerate(df.to_dict(orient="records"))
    ]
    
    log.info(f"Processing {len(rows)} vehicles with {MAX_WORKERS} workers")
    
    # Process with optimized batching
    results = await _process_batch_optimized(rows, at, cap, MAX_WORKERS)
    
    log.info(f"Completed processing {len(results)} vehicles")
    return results


def process_dataframe(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    Sync wrapper for environments that call synchronously.
    """
    try:
        loop = asyncio.get_running_loop()
        # If we're in a running loop, we need to create a new loop
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, process_dataframe_async(df))
            return future.result()
    except RuntimeError:
        # No running loop - clean case
        return asyncio.run(process_dataframe_async(df))


# ---------- Fast XLSX Writing ----------

def results_to_excel_fast(results: List[Dict[str, Any]], output_path: str) -> None:
    """
    Optimized write-only XLSX generation with proper numeric formatting.
    """
    from openpyxl import Workbook

    if not results:
        wb = Workbook(write_only=True)
        ws = wb.create_sheet()
        wb.save(output_path)
        return

    # Clean and map all results
    cleaned_results = []
    for r in results:
        vehicle_data = _extract_vehicle_data(r)
        cleaned_row = _clean_and_map_row(r, vehicle_data)
        cleaned_results.append(cleaned_row)

    # Build header from first cleaned result
    if not cleaned_results:
        wb = Workbook(write_only=True)
        ws = wb.create_sheet()
        wb.save(output_path)
        return
    
    first_result = cleaned_results[0]
    
    # Get source columns (everything except API columns)
    api_column_set = set(API_COLUMNS)
    source_columns = [k for k in first_result.keys() if k not in api_column_set]
    
    # Final header: source columns + API columns
    header = source_columns + API_COLUMNS

    wb = Workbook(write_only=True)
    ws = wb.create_sheet()
    ws.append(header)

    # Write rows in bulk - openpyxl preserves int/float types automatically
    for r in cleaned_results:
        row_values = []
        for c in header:
            val = r.get(c, "")
            # openpyxl automatically handles int/float/str types correctly
            row_values.append(val)
        ws.append(row_values)

    wb.save(output_path)
    log.info(f"Wrote {len(cleaned_results)} rows with {len(header)} columns ({len(source_columns)} source + {len(API_COLUMNS)} API) to {output_path}")


# ---------- Convenience Pipeline ----------

def process_and_write_xlsx(
    df: pd.DataFrame,
    output_path: str,
) -> None:
    """
    Complete pipeline: fetch, enrich, write.
    """
    results = process_dataframe(df)
    
    # Clean up internal tracking field
    for r in results:
        r.pop("row_index", None)
    
    results_to_excel_fast(results, output_path)
    log.info(f"Pipeline complete: {len(results)} vehicles processed")
