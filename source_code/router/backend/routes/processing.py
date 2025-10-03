# FILE PATH: source_code/router/backend/routes/processing.py
# Async AT/CAP pipeline with stable threading, progress logging, fast XLSX

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


def _run_elaborate_background(task_id: int, buying_target: int = 500):
    """
    Run elaborate_task in a background thread with its own event loop.
    """
    loop = None
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(
            elaborate_task({"task_id": task_id, "buying_target": buying_target})
        )
    except Exception as e:
        logger.error("Processing failed for task %s: %s", task_id, e)
        logger.error("Traceback: %s", traceback.format_exc())
    finally:
        if loop:
            try:
                # Cancel pending tasks (don't wait on executors)
                for t in asyncio.all_tasks(loop):
                    t.cancel()
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass


async def elaborate_task(data: Dict[str, Any]):
    """
    Download input, enrich with AT+CAP, Snowflake-enrich, write XLSX, upload, update DB.
    """
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
                (data["task_id"],),
            )
            row = cursor.fetchone()
            if not row:
                return {"message": "Task not found"}
            cols = [d[0] for d in cursor.description]
            task = dict(zip(cols, row))

            cursor.execute(
                "UPDATE tasks SET status = 'pending', message = NULL WHERE task_id = %s",
                (data["task_id"],),
            )
            conn.commit()

    supplier_name = task.get("supplier_name", "Universal")
    buying_target = int(data.get("buying_target", 500))
    logger.info(
        f"Task {data['task_id']}: Processing with supplier '{supplier_name}', buying_target={buying_target}"
    )

    env = "prod" if os.getenv("AWS_REGION") == "eu-west-1" else "dev"

    # Input and output
    input_ext = task["filename"].split(".")[-1].lower()
    output_ext = "xlsx"

    s3_raw_key = f"applications/pricing/uk-vehicle-list-evaluator/{env}/raw/{data['task_id']}.{input_ext}"
    s3_out_key = f"applications/pricing/uk-vehicle-list-evaluator/{env}/elaborated/{data['task_id']}.{output_ext}"

    total_rows = None
    execution_time = None

    with tempfile.TemporaryDirectory() as tmpdir:
        local_in = os.path.join(tmpdir, f"raw_{data['task_id']}.{input_ext}")
        local_out = os.path.join(tmpdir, f"out_{data['task_id']}.{output_ext}")

        t0 = time.time()
        try:
            # 2) Download and read input
            download_file(local_in, s3_bucketname, s3_raw_key)
            input_df = read_dataframe_file(local_in)
            total_rows = len(input_df)
            logger.info(f"Task {data['task_id']}: Loaded {total_rows} rows from {local_in}")

            # 3) Enrichment (AT + CAP) with progress logging
            def _progress(n: int):
                if n == total_rows or n % PROGRESS_UPDATE_INTERVAL == 0:
                    logger.info(f"Task {data['task_id']}: Progress {n}/{total_rows}")

            results = await process_dataframe_async(
                input_df, supplier_name, progress_callback=_progress
            )

            logger.info(f"Task {data['task_id']}: API enrichment completed")

            # Clean internal fields
            for r in results:
                r.pop("row_index", None)

            merged_df = pd.DataFrame(results)

            # 4) Snowflake enrichment
            merged_df = enrich_with_stock_sales(
                merged_df,
                capid_col=None,
                environment="prod",
                fill_missing_zero=True,
            )
            logger.info(f"Task {data['task_id']}: Snowflake enrichment completed")

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
            mapper = get_columns_mapper(supplier_name)
            write_results_to_xlsx(merged_df, local_out, mapper)
            logger.info(f"Task {data['task_id']}: Wrote XLSX to {local_out}")

            # 7) Upload
            upload_file(local_out, s3_bucketname, s3_out_key)
            logger.info(f"Task {data['task_id']}: Uploaded to S3: {s3_out_key}")

            execution_time = time.time() - t0
            status, message = "success", None

        except Exception as e:
            execution_time = time.time() - t0
            status, message = "error", f"{e}"
            logger.error("Task %s failed: %s", data["task_id"], message)
            logger.debug("Traceback:\n%s", traceback.format_exc())

        # 8) Update DB
        with get_database_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE tasks SET status = %s, message = %s, total_rows = %s, execution_time = %s WHERE task_id = %s",
                    (status, message, total_rows, execution_time, data["task_id"]),
                )
                conn.commit()

    return {"message": "Task processing completed", "status": status, "rows": total_rows}