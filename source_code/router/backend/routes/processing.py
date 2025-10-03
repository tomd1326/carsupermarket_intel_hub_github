# FILE PATH: source_code/router/backend/routes/processing.py
# Async AT/CAP pipeline with stable threading, progress logging, fast XLSX
# ENHANCED: Better logging for CloudWatch debugging
# FIXED: Async function for Lambda (no event loop conflict)

import os
import time
import logging
import tempfile
import asyncio
import traceback
from typing import Dict, Any

import pandas as pd

from utils.db_utils import get_database_connection
from utils.s3_tools import upload_file, download_file
from utils.file_handler import read_dataframe_file
from list_processor.columns_mapper import get_columns_mapper
from list_processor.snowflake_enrichment import enrich_with_stock_sales
from list_processor.enhanced_list_processor import (
    process_dataframe_async,
    write_results_to_xlsx,
)
from utils.config import PROGRESS_UPDATE_INTERVAL

logger = logging.getLogger(__name__)
s3_bucketname = "aa-raw-sources-uk"


async def _run_elaborate_background(task_id: int, buying_target: int = 500):
    """
    Run elaborate_task directly (async, no new event loop).
    Works in both Lambda (existing loop) and local threaded contexts.
    """
    try:
        await elaborate_task({"task_id": task_id, "buying_target": buying_target})
    except Exception as e:
        logger.exception("Processing failed for task %s", task_id)


async def elaborate_task(data: Dict[str, Any]):
    """
    Download input, enrich with AT+CAP, Snowflake-enrich, write XLSX, upload, update DB.
    """
    task_id = data["task_id"]
    buying_target = int(data.get("buying_target", 500))
    
    logger.info(f"=" * 80)
    logger.info(f"Task {task_id}: START PROCESSING")
    logger.info(f"Task {task_id}: buying_target={buying_target}")
    logger.info(f"=" * 80)
    
    # 1) Read task metadata and mark pending
    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT t.*, s.supplier_name
                  FROM tasks t
                  LEFT JOIN suppliers s ON s.supplier_id = t.supplier_id
                 WHERE t.task_id = %s
                """,
                (task_id,),
            )
            row = cursor.fetchone()
            if not row:
                logger.error(f"Task {task_id}: NOT FOUND in database")
                return {"message": "Task not found"}
            cols = [d[0] for d in cursor.description]
            task = dict(zip(cols, row))

            cursor.execute(
                "UPDATE tasks SET status = 'pending', message = NULL WHERE task_id = %s",
                (task_id,),
            )
            conn.commit()

    supplier_name = task.get("supplier_name", "Universal")
    logger.info(f"Task {task_id}: Supplier='{supplier_name}'")

    env = "prod" if os.getenv("AWS_REGION") == "eu-west-1" else "dev"
    logger.info(f"Task {task_id}: Environment={env}")

    # Input and output
    input_ext = task["filename"].split(".")[-1].lower()
    output_ext = "xlsx"

    s3_raw_key = f"applications/pricing/uk-vehicle-list-evaluator/{env}/raw/{task_id}.{input_ext}"
    s3_out_key = f"applications/pricing/uk-vehicle-list-evaluator/{env}/elaborated/{task_id}.{output_ext}"

    total_rows = None
    execution_time = None

    with tempfile.TemporaryDirectory() as tmpdir:
        local_in = os.path.join(tmpdir, f"raw_{task_id}.{input_ext}")
        local_out = os.path.join(tmpdir, f"out_{task_id}.{output_ext}")

        t0 = time.time()
        try:
            # 2) Download and read input
            logger.info(f"Task {task_id}: Downloading from S3: {s3_raw_key}")
            download_file(local_in, s3_bucketname, s3_raw_key)
            logger.info(f"Task {task_id}: Downloaded to {local_in}")
            
            logger.info(f"Task {task_id}: Reading input file...")
            input_df = read_dataframe_file(local_in)
            total_rows = len(input_df)
            logger.info(f"Task {task_id}: Loaded {total_rows} rows from {local_in}")

            # 3) Enrichment (AT + CAP) with progress logging
            logger.info(f"Task {task_id}: Starting API enrichment (AutoTrader + CAP)...")
            
            def _progress(n: int):
                if n == total_rows or n % PROGRESS_UPDATE_INTERVAL == 0:
                    logger.info(f"Task {task_id}: API Progress {n}/{total_rows}")

            api_start = time.time()
            results = await process_dataframe_async(
                input_df, supplier_name, progress_callback=_progress
            )
            api_duration = time.time() - api_start

            logger.info(f"Task {task_id}: API enrichment completed in {api_duration:.2f}s")
            logger.info(f"Task {task_id}: Processed {len(results)} vehicles")

            # Clean internal fields
            for r in results:
                r.pop("row_index", None)

            merged_df = pd.DataFrame(results)

            # 4) Snowflake enrichment
            logger.info(f"Task {task_id}: Starting Snowflake enrichment...")
            sf_start = time.time()
            merged_df = enrich_with_stock_sales(
                merged_df,
                capid_col=None,
                environment="prod",
                fill_missing_zero=True,
            )
            sf_duration = time.time() - sf_start
            logger.info(f"Task {task_id}: Snowflake enrichment completed in {sf_duration:.2f}s")

            # Normalize numeric types for calculations (if present)
            numeric_cols = [
                "capid_stock",
                "capid_sales",
                "capid_sales_7_days",
                "capid_avg_days_in_stock_acknowledged",
                "capid_sales_percentage",
                "total_sales",
                "total_stock",
            ]
            for c in numeric_cols:
                if c in merged_df.columns:
                    merged_df[c] = pd.to_numeric(merged_df[c], errors="coerce").fillna(0)

            # 5) Calculated columns (far right)
            logger.info(f"Task {task_id}: Calculating derived columns...")
            merged_df["buying_target"] = int(buying_target)
            merged_df["total_stock_target"] = merged_df["total_stock"] + buying_target
            merged_df["capid_ideal_stock"] = (
                merged_df["capid_sales_percentage"] * merged_df["total_stock_target"]
            )
            merged_df["capid_requirement"] = (
                merged_df["capid_ideal_stock"] - merged_df["capid_stock"]
            )

            # Drop any unwanted columns if present
            if "cap_registration_date" in merged_df.columns:
                merged_df = merged_df.drop(columns=["cap_registration_date"])

            # 6) Write XLSX with correct order
            logger.info(f"Task {task_id}: Writing XLSX output...")
            xlsx_start = time.time()
            mapper = get_columns_mapper(supplier_name)
            write_results_to_xlsx(merged_df, local_out, mapper)
            xlsx_duration = time.time() - xlsx_start
            logger.info(f"Task {task_id}: Wrote XLSX in {xlsx_duration:.2f}s to {local_out}")

            # 7) Upload
            logger.info(f"Task {task_id}: Uploading to S3: {s3_out_key}")
            upload_start = time.time()
            upload_file(local_out, s3_bucketname, s3_out_key)
            upload_duration = time.time() - upload_start
            logger.info(f"Task {task_id}: Uploaded in {upload_duration:.2f}s")

            execution_time = time.time() - t0
            status, message = "success", None
            
            logger.info(f"=" * 80)
            logger.info(f"Task {task_id}: SUCCESS")
            logger.info(f"Task {task_id}: Total time: {execution_time:.2f}s")
            logger.info(f"Task {task_id}: Breakdown - API: {api_duration:.2f}s, Snowflake: {sf_duration:.2f}s, XLSX: {xlsx_duration:.2f}s, Upload: {upload_duration:.2f}s")
            logger.info(f"=" * 80)

        except Exception as e:
            execution_time = time.time() - t0
            status, message = "error", f"{e}"
            logger.exception(f"Task {task_id} FAILED after {execution_time:.2f}s")
            logger.error(f"=" * 80)
            logger.error(f"Task {task_id}: ERROR - {message}")
            logger.error(f"=" * 80)

        # 8) Update DB
        with get_database_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE tasks SET status = %s, message = %s, total_rows = %s, execution_time = %s WHERE task_id = %s",
                    (status, message, total_rows, execution_time, task_id),
                )
                conn.commit()

    return {"message": "Task processing completed", "status": status, "rows": total_rows}
