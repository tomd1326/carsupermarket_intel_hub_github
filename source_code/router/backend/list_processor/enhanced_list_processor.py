# FILE: source_code/router/backend/list_processor/enhanced_list_processor.py

import asyncio
import logging
from typing import Any, Dict, List, Optional, Tuple
import datetime

import pandas as pd

from .autotrader_client import AutoTraderClient
from .cap_client import CAPClient
from .columns_mapper import get_columns_mapper, BaseColumnsMapper
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


# ---------- Helper Function ----------

def _round_mileage(mileage: int) -> int:
    """Round mileage to nearest 1000 (minimum 1000)."""
    if mileage < 1000:
        return 1000
    return round(mileage / 1000) * 1000


# ---------- Optimized Orchestration ----------

async def _process_one_parallel(
    idx: int,
    row: Dict[str, Any],
    at: AutoTraderClient,
    cap: CAPClient,
    mapper: BaseColumnsMapper,
) -> Dict[str, Any]:
    """
    Process a single vehicle with PARALLEL CAP + AT vehicles calls.
    
    Uses columns_mapper to extract VRM, mileage, CAPID, registration_date.
    Handles both VRM-only and CAPID+VRM scenarios.
    """
    result: Dict[str, Any] = dict(row)
    result["row_index"] = idx

    # Use columns_mapper to extract vehicle data (supplier-specific logic)
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

    # CRITICAL: Round mileage BEFORE any CAP API calls
    rounded_mileage = _round_mileage(mileage)

    # Determine which CAP endpoint to use
    if capid and registration_date:
        # Use CAPID endpoint (provides both live and monthly valuations)
        # Calls: capid_monthly_url + capid_live_url (NOT vrm_url)
        reg_date_str = registration_date.strftime("%Y-%m-%d") if isinstance(registration_date, datetime.date) else str(registration_date)
        cap_task = asyncio.create_task(cap.capid(capid, rounded_mileage, reg_date_str))
        # Removed verbose logging
    else:
        # Use VRM endpoint (provides monthly valuation, and live if database=='CAR')
        # Calls: vrm_url (and possibly capid_live_url if database=='CAR')
        cap_task = asyncio.create_task(cap.vrm(vrm, rounded_mileage))
        # Removed verbose logging
    
    # AutoTrader always uses VRM (with ORIGINAL mileage, not rounded)
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
    mapper: BaseColumnsMapper,
    max_workers: int,
) -> List[Dict[str, Any]]:
    """
    Optimized batch processing with controlled concurrency.
    """
    sem = asyncio.Semaphore(max_workers)

    async def _wrap(i: int, r: Dict[str, Any]):
        async with sem:
            return await _process_one_parallel(i, r, at, cap, mapper)

    # Create all tasks at once for better scheduling
    tasks = [_wrap(i, r) for i, r in rows]
    
    # Gather with return_exceptions to prevent one failure from stopping everything
    results = await asyncio.gather(*tasks, return_exceptions=False)
    
    # Sort by original order
    results = sorted(results, key=lambda d: d.get("row_index", 0))
    return results


async def process_dataframe_async(
    df: pd.DataFrame,
    supplier_name: str,
    *,
    env_region: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Async entrypoint with optimized client initialization.
    
    Args:
        df: Input dataframe
        supplier_name: Supplier name for columns_mapper (e.g., "Motability", "Enterprise", "1link", "Universal")
    """
    # Get supplier-specific columns mapper
    mapper = get_columns_mapper(supplier_name)
    log.info(f"Using {mapper.__class__.__name__} for supplier '{supplier_name}'")
    
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
    results = await _process_batch_optimized(rows, at, cap, mapper, MAX_WORKERS)
    
    log.info(f"Completed processing {len(results)} vehicles")
    return results


def process_dataframe(df: pd.DataFrame, supplier_name: str) -> List[Dict[str, Any]]:
    """
    Sync wrapper - CREATES NEW EVENT LOOP.
    
    Args:
        df: Input dataframe
        supplier_name: Supplier name for columns_mapper
    """
    # Always create a new event loop for sync calls
    return asyncio.run(process_dataframe_async(df, supplier_name))


# ---------- Fast XLSX Writing with Correct Column Order ----------

def write_results_to_xlsx(
    merged_df: pd.DataFrame,
    output_path: str,
    mapper: BaseColumnsMapper,
) -> None:
    """
    Write DataFrame to XLSX with EXACT column order: [SOURCE COLUMNS] + [API COLUMNS].
    
    Args:
        merged_df: DataFrame with all columns (source + API + Snowflake)
        output_path: Output XLSX file path
        mapper: Columns mapper (not used but kept for compatibility)
    """
    from openpyxl import Workbook
    
    if merged_df.empty:
        wb = Workbook(write_only=True)
        ws = wb.create_sheet()
        wb.save(output_path)
        return
    
    # Define internal fields to exclude
    internal_fields = {
        "row_index", "error", 
        "at_success", "at_error", "at_response_code", "at_make", "at_model", "at_derivative_id",
        "cap_success", "cap_error", "cap_response_code", "cap_registration_date", "cap_database"
    }
    
    # Get API column set
    api_column_set = set(API_COLUMNS)
    
    # Identify source columns (not internal, not API)
    all_columns = merged_df.columns.tolist()
    source_columns = [
        col for col in all_columns 
        if col not in internal_fields and col not in api_column_set
    ]
    
    # Build final column order: source + API
    final_columns = source_columns + API_COLUMNS
    
    # Filter to only columns that exist in DataFrame
    final_columns = [col for col in final_columns if col in merged_df.columns]
    
    # Reorder DataFrame
    output_df = merged_df[final_columns].copy()
    
    log.info(f"Writing XLSX with {len(output_df)} rows and {len(final_columns)} columns ({len(source_columns)} source + {len([c for c in API_COLUMNS if c in merged_df.columns])} API)")
    
    # Write to XLSX using openpyxl for speed
    wb = Workbook(write_only=True)
    ws = wb.create_sheet()
    
    # Write header
    ws.append(final_columns)
    
    # Write data rows
    for _, row in output_df.iterrows():
        ws.append(row.tolist())
    
    wb.save(output_path)
    log.info(f"Wrote XLSX to {output_path}")


# ---------- Convenience Pipeline ----------

def process_and_write_xlsx(
    df: pd.DataFrame,
    output_path: str,
    supplier_name: str,
) -> None:
    """
    Complete pipeline: fetch, enrich, write.
    
    Args:
        df: Input dataframe
        output_path: Output XLSX file path
        supplier_name: Supplier name for columns_mapper
    """
    mapper = get_columns_mapper(supplier_name)
    results = process_dataframe(df, supplier_name)
    
    # Clean up internal tracking field
    for r in results:
        r.pop("row_index", None)
    
    # Convert to DataFrame and write
    merged_df = pd.DataFrame(results)
    write_results_to_xlsx(merged_df, output_path, mapper)
    log.info(f"Pipeline complete: {len(results)} vehicles processed")
