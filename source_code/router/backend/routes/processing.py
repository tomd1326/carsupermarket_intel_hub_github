# FILE PATH: source_code/router/backend/routes/processing.py
# Enhanced version with better column mapping for CAP client

import os
import time
import logging
import datetime
import tempfile
import asyncio
import traceback
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from utils.db_utils import get_database_connection
from utils.s3_tools import upload_file, download_file
from utils.file_handler import read_dataframe_file, write_dataframe_file
from list_processor.autotrader_client import AutoTraderClient
from list_processor.cap_client import CAPClient
from list_processor.columns_mapper import get_columns_mapper
from list_processor.snowflake_enrichment import enrich_with_stock_sales

logger = logging.getLogger(__name__)

# S3 bucket
s3_bucketname = "aa-raw-sources-uk"

# -------- Background Processing --------

def _run_elaborate_background(task_id: int):
    """Run elaborate_task in a background thread with its own event loop."""
    try:
        asyncio.run(elaborate_task({"task_id": task_id}))
    except Exception as e:
        logger.error(f"Background processing failed for task {task_id}: {e}")

# -------- Helper Functions --------

def _prepare_df_for_cap_client(df: pd.DataFrame, columns_mapper) -> pd.DataFrame:
    """
    Ensure the DataFrame has the columns CAP client expects.
    CAP client looks for: vrm, registration_date, mileage
    """
    prepared_df = df.copy()
    
    # Try to map common VRM column variations
    vrm_candidates = [col for col in df.columns if any(x in col.lower() for x in ['vrm', 'reg', 'plate'])]
    if vrm_candidates and 'vrm' not in prepared_df.columns:
        prepared_df['vrm'] = df[vrm_candidates[0]]
        logger.info(f"Mapped VRM column: {vrm_candidates[0]} -> vrm")
    
    # Try to map registration date variations
    reg_date_candidates = [col for col in df.columns if any(x in col.lower() for x in ['registration', 'reg_date', 'first_reg', 'date'])]
    if reg_date_candidates and 'registration_date' not in prepared_df.columns:
        prepared_df['registration_date'] = df[reg_date_candidates[0]]
        logger.info(f"Mapped registration date column: {reg_date_candidates[0]} -> registration_date")
    
    # Try to map mileage variations  
    mileage_candidates = [col for col in df.columns if any(x in col.lower() for x in ['mileage', 'miles', 'odometer'])]
    if mileage_candidates and 'mileage' not in prepared_df.columns:
        prepared_df['mileage'] = df[mileage_candidates[0]]
        logger.info(f"Mapped mileage column: {mileage_candidates[0]} -> mileage")
    
    # If we still don't have the required columns, add empty ones
    for required_col in ['vrm', 'registration_date', 'mileage']:
        if required_col not in prepared_df.columns:
            prepared_df[required_col] = pd.NA
            logger.warning(f"Required column '{required_col}' not found, added empty column")
    
    return prepared_df

# -------- Main Processing Function --------

async def elaborate_task(data: dict):
    """
    Process a single task:
    - mark as pending (upload path already set it, but keep it idempotent)
    - download raw file
    - enrich with AutoTrader and CAP data
    - Enhanced Snowflake enrichment: add Stock, 30-day Sales, 7-day Sales by CAP ID
    - write output and upload
    """
    # Fetch task, mark as pending
    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT t.*, s.supplier_name
                  FROM tasks t
                  LEFT JOIN suppliers s ON s.supplier_id = t.supplier_id
                 WHERE t.task_id = %s
                """,
                (data["task_id"],),
            )
            row = cursor.fetchone()
            if not row:
                return {"message": "Task not found"}
            cols = [d[0] for d in cursor.description]
            task = dict(zip(cols, row))

            cursor.execute("UPDATE tasks SET status = 'pending' WHERE task_id = %s", (data["task_id"],))
            conn.commit()

    env = "prod" if os.getenv("AWS_REGION") == "eu-west-1" else "dev"
    file_ext = task["filename"].split(".")[-1].lower()
    s3_raw_key = f'applications/pricing/uk-vehicle-list-evaluator/{env}/raw/{data["task_id"]}.{file_ext}'
    s3_out_key = f'applications/pricing/uk-vehicle-list-evaluator/{env}/elaborated/{data["task_id"]}.{file_ext}'

    total_rows = None
    execution_time = None

    # Work inside a temp directory and never pre-create target files used by S3 transfer
    with tempfile.TemporaryDirectory() as tmpdir:
        local_in = os.path.join(tmpdir, f'raw_{data["task_id"]}.{file_ext}')
        local_out = os.path.join(tmpdir, f'out_{data["task_id"]}.{file_ext}')

        t0 = time.time()

        try:
            # Download raw (path must not exist before download_file)
            download_file(local_in, s3_bucketname, s3_raw_key)

            # Read and process
            input_df = read_dataframe_file(local_in)
            total_rows = len(input_df)

            autotrader = AutoTraderClient()
            cap_client = CAPClient()
            columns_mapper = get_columns_mapper(task["supplier_name"])

            # Process AutoTrader (uses existing logic)
            logger.info("Starting AutoTrader processing...")
            autotrader_result = autotrader.process_file(input_df, columns_mapper)
            
            # Process CAP client (with enhanced column mapping)
            logger.info("Starting CAP processing...")
            cap_prepared_df = _prepare_df_for_cap_client(input_df, columns_mapper)
            cap_result = cap_client.process_file(cap_prepared_df, columns_mapper)

            # Combine results
            processed_df = pd.concat([input_df, autotrader_result, cap_result], axis=1)
            logger.info(f"Combined DataFrame shape: {processed_df.shape}")

            # ---- ENHANCED Snowflake enrichment with auto-detection ----
            try:
                # Use enhanced version that auto-detects CAPID columns
                processed_df = enrich_with_stock_sales(
                    processed_df,
                    capid_col=None,  # Auto-detect the best CAPID column
                    environment=env,
                    fill_missing_zero=True,
                )
                # Rename to required output headers
                processed_df = processed_df.rename(columns={
                    "sf_stock": "Stock",
                    "sf_sales": "30-day Sales",
                    "sf_sales_7d": "7-day Sales",
                })
                logger.info(f"Snowflake enrichment completed for task {data['task_id']}")
            except Exception as e:
                logger.error(f"Snowflake enrichment failed for task {data['task_id']}: {e}")
                # Ensure columns exist even if enrichment failed
                for c in ["Stock", "30-day Sales", "7-day Sales"]:
                    if c not in processed_df.columns:
                        processed_df[c] = 0

            # Write output (path must not exist beforehand)
            write_dataframe_file(processed_df, local_out)

            # Upload
            upload_file(local_out, s3_bucketname, s3_out_key)

            execution_time = time.time() - t0
            status, message = "success", None
            logger.info(f"Task {data['task_id']} completed in {execution_time:.2f}s")

        except Exception as e:
            status, message = "error", str(e)
            logger.error(
                f"Error processing task {data['task_id']}: {message}\n{traceback.format_exc()}"
            )

        # Update DB
        with get_database_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE tasks SET status = %s, message = %s, total_rows = %s, execution_time = %s WHERE task_id = %s",
                    (status, message, total_rows, execution_time, data["task_id"]),
                )
                conn.commit()

    return {}