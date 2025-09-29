# FILE PATH: source_code/router/backend/routes/processing.py
# LAMBDA-OPTIMIZED: PROD Snowflake + minimal logging + REMOVED cap_registration_date

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

# -------- FIXED: Background Processing Function --------

def _run_elaborate_background(task_id: int):
    """Run elaborate_task in a background thread with its own event loop."""
    try:
        asyncio.run(elaborate_task({"task_id": task_id}))
    except Exception as e:
        logger.error(f"Processing failed for task {task_id}: {e}")

# -------- Helper Functions --------

def _prepare_df_for_cap_client(df: pd.DataFrame, columns_mapper) -> pd.DataFrame:
    """Prepare DataFrame for CAP client with minimal logging"""
    prepared_df = df.copy()
    
    # Map VRM column
    vrm_candidates = [col for col in df.columns if any(x in col.lower() for x in ['vrm', 'reg', 'plate'])]
    if vrm_candidates and 'vrm' not in prepared_df.columns:
        prepared_df['vrm'] = df[vrm_candidates[0]]
    
    # Map mileage column
    mileage_candidates = [col for col in df.columns if any(x in col.lower() for x in ['mile', 'odometer'])]
    if mileage_candidates and 'mileage' not in prepared_df.columns:
        prepared_df['mileage'] = df[mileage_candidates[0]]
    
    # Map registration date
    reg_date_candidates = [col for col in df.columns if any(x in col.lower() for x in ['registration', 'reg_date', 'first_reg', 'date'])]
    if reg_date_candidates and 'registration_date' not in prepared_df.columns:
        prepared_df['registration_date'] = df[reg_date_candidates[0]]
    elif 'registration_date' not in prepared_df.columns:
        prepared_df['registration_date'] = ''
    
    return prepared_df

# -------- Main Processing Function --------

async def elaborate_task(data: dict):
    """LAMBDA-OPTIMIZED processing with minimal logging"""
    
    # Fetch task
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

    # Environment setup
    env = "prod" if os.getenv("AWS_REGION") == "eu-west-1" else "dev"
    file_ext = task["filename"].split(".")[-1].lower()
    s3_raw_key = f'applications/pricing/uk-vehicle-list-evaluator/{env}/raw/{data["task_id"]}.{file_ext}'
    s3_out_key = f'applications/pricing/uk-vehicle-list-evaluator/{env}/elaborated/{data["task_id"]}.{file_ext}'

    total_rows = None
    execution_time = None

    # Process in temp directory
    with tempfile.TemporaryDirectory() as tmpdir:
        local_in = os.path.join(tmpdir, f'raw_{data["task_id"]}.{file_ext}')
        local_out = os.path.join(tmpdir, f'out_{data["task_id"]}.{file_ext}')

        t0 = time.time()

        try:
            # Download and read
            download_file(local_in, s3_bucketname, s3_raw_key)
            input_df = read_dataframe_file(local_in)
            total_rows = len(input_df)

            # Initialize clients
            autotrader = AutoTraderClient()
            cap_client = CAPClient()
            columns_mapper = get_columns_mapper(task["supplier_name"])

            # Process AutoTrader and CAP
            autotrader_result = autotrader.process_file(input_df, columns_mapper)
            cap_prepared_df = _prepare_df_for_cap_client(input_df, columns_mapper)
            cap_result = cap_client.process_file(cap_prepared_df, columns_mapper)

            # Combine results
            processed_df = pd.concat([input_df, autotrader_result, cap_result], axis=1)

            # HARDCODED PROD Snowflake enrichment
            processed_df = enrich_with_stock_sales(
                processed_df,
                capid_col=None,
                environment="prod",  # ALWAYS PROD
                fill_missing_zero=True,
            )

            # ⚡ REMOVE cap_registration_date column from final output
            columns_to_remove = ['cap_registration_date']
            processed_df = processed_df.drop(columns=[col for col in columns_to_remove if col in processed_df.columns])

            # Write and upload
            write_dataframe_file(processed_df, local_out)
            upload_file(local_out, s3_bucketname, s3_out_key)

            execution_time = time.time() - t0
            status, message = "success", None

        except Exception as e:
            execution_time = time.time() - t0
            error_message = str(e)
            logger.error(f"Task {data['task_id']} failed: {error_message}")
            status, message = "error", error_message

        # Update database
        with get_database_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE tasks SET status = %s, message = %s, total_rows = %s, execution_time = %s WHERE task_id = %s",
                    (status, message, total_rows, execution_time, data["task_id"]),
                )
                conn.commit()

    return {"message": "Task processing completed"}