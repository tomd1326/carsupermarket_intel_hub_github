# FILE PATH: source_code/router/backend/routes/processing.py
# LAMBDA-OPTIMIZED: async AT/CAP pipeline, minimal logging, fast XLSX, no duplicate merges

import os
import time
import logging
import tempfile
import asyncio
import traceback
from typing import Dict, Any, List, Optional

import pandas as pd

from router.backend.utils.db_utils import get_database_connection
from router.backend.utils.s3_tools import upload_file, download_file
from router.backend.utils.file_handler import read_dataframe_file
from router.backend.list_processor.columns_mapper import get_columns_mapper
from router.backend.list_processor.snowflake_enrichment import enrich_with_stock_sales
from router.backend.list_processor.enhanced_list_processor import (
    process_dataframe_async,
    write_results_to_xlsx,
)

logger = logging.getLogger(__name__)
s3_bucketname = "aa-raw-sources-uk"


def _run_elaborate_background(task_id: int):
    """Run elaborate_task in a background thread with its own event loop."""
    try:
        asyncio.run(elaborate_task({"task_id": task_id}))
    except Exception as e:
        logger.error("Processing failed for task %s: %s", task_id, e)


async def elaborate_task(data: Dict[str, Any]):
    """
    Download, enrich, Snowflake-enrich, write XLSX, upload, update DB.
    
    FIXED: 
    - Uses columns_mapper with supplier_name for proper CAPID/regdate extraction
    - ALL outputs are XLSX regardless of input format
    - Correct column order: [SOURCE] + [API]
    """
    # Fetch task metadata
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

    # Get supplier_name for columns_mapper
    supplier_name = task.get("supplier_name", "Universal")
    logger.info(f"Task {data['task_id']}: Processing with supplier '{supplier_name}'")

    env = "prod" if os.getenv("AWS_REGION") == "eu-west-1" else "dev"
    
    # Input file extension (for S3 download)
    input_ext = task["filename"].split(".")[-1].lower()
    
    # ALWAYS output XLSX regardless of input format
    output_ext = "xlsx"
    
    s3_raw_key = f'applications/pricing/uk-vehicle-list-evaluator/{env}/raw/{data["task_id"]}.{input_ext}'
    s3_out_key = f'applications/pricing/uk-vehicle-list-evaluator/{env}/elaborated/{data["task_id"]}.{output_ext}'

    total_rows = None
    execution_time = None

    with tempfile.TemporaryDirectory() as tmpdir:
        local_in = os.path.join(tmpdir, f'raw_{data["task_id"]}.{input_ext}')
        local_out = os.path.join(tmpdir, f'out_{data["task_id"]}.{output_ext}')

        t0 = time.time()
        try:
            # Download and read input file
            download_file(local_in, s3_bucketname, s3_raw_key)
            input_df = read_dataframe_file(local_in)
            total_rows = len(input_df)
            
            logger.info(f"Task {data['task_id']}: Loaded {total_rows} rows from {local_in}")

            # CRITICAL: Pass supplier_name to use columns_mapper
            results = await process_dataframe_async(input_df, supplier_name)
            
            logger.info(f"Task {data['task_id']}: API enrichment completed")

            # Clean up internal fields
            for r in results:
                r.pop("row_index", None)

            # Convert to dataframe for Snowflake enrichment
            merged_df = pd.DataFrame(results)

            # Snowflake enrichment
            merged_df = enrich_with_stock_sales(
                merged_df,
                capid_col=None,
                environment="prod",
                fill_missing_zero=True,
            )
            
            logger.info(f"Task {data['task_id']}: Snowflake enrichment completed")

            # Remove unwanted column if present
            if "cap_registration_date" in merged_df.columns:
                merged_df = merged_df.drop(columns=["cap_registration_date"])

            # Write XLSX with correct column order
            mapper = get_columns_mapper(supplier_name)
            write_results_to_xlsx(merged_df, local_out, mapper)
            
            logger.info(f"Task {data['task_id']}: Wrote XLSX to {local_out}")

            # Upload to S3
            upload_file(local_out, s3_bucketname, s3_out_key)
            
            logger.info(f"Task {data['task_id']}: Uploaded to S3: {s3_out_key}")

            execution_time = time.time() - t0
            status, message = "success", None

        except Exception as e:
            execution_time = time.time() - t0
            status, message = "error", f"{e}"
            logger.error("Task %s failed: %s", data["task_id"], message)
            logger.debug("Traceback:\n%s", traceback.format_exc())

        # Update database status
        with get_database_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE tasks SET status = %s, message = %s, total_rows = %s, execution_time = %s WHERE task_id = %s",
                    (status, message, total_rows, execution_time, data["task_id"]),
                )
                conn.commit()

    return {"message": "Task processing completed", "status": status, "rows": total_rows}
