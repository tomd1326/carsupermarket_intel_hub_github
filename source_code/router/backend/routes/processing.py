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
    results_to_excel_fast,
)

logger = logging.getLogger(__name__)
s3_bucketname = "aa-raw-sources-uk"


def _run_elaborate_background(task_id: int):
    """Run elaborate_task in a background thread with its own event loop."""
    try:
        asyncio.run(elaborate_task({"task_id": task_id}))
    except Exception as e:
        logger.error("Processing failed for task %s: %s", task_id, e)


def _normalize_columns_for_pipeline(df: pd.DataFrame, supplier_name: Optional[str]) -> pd.DataFrame:
    """
    Ensure canonical columns expected by the async pipeline:
      - REGNO (VRM)
      - MILES (odometer miles)
    """
    out = df.copy()
    try:
        mapper = get_columns_mapper(supplier_name) if supplier_name else {}
    except Exception:
        mapper = {}

    if "REGNO" not in out.columns:
        src = None
        for key in ("vrm", "regno", "registration", "reg", "plate"):
            if isinstance(mapper, dict) and key in mapper and mapper[key] in out.columns:
                src = mapper[key]; break
        if not src:
            for c in out.columns:
                cl = c.lower()
                if any(k in cl for k in ("vrm", "regno", "registration", "reg", "plate")):
                    src = c; break
        if src:
            out["REGNO"] = out[src].astype(str).str.strip()

    if "MILES" not in out.columns:
        src = None
        for key in ("miles", "mileage", "odometer"):
            if isinstance(mapper, dict) and key in mapper and mapper[key] in out.columns:
                src = mapper[key]; break
        if not src:
            for c in out.columns:
                cl = c.lower()
                if any(k in cl for k in ("miles", "mileage", "odometer")):
                    src = c; break
        if src:
            out["MILES"] = pd.to_numeric(out[src], errors="coerce").fillna(0).astype(int)

    return out


async def elaborate_task(data: Dict[str, Any]):
    """Download, enrich, Snowflake-enrich, write XLSX, upload, update DB."""
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

    env = "prod" if os.getenv("AWS_REGION") == "eu-west-1" else "dev"
    file_ext = task["filename"].split(".")[-1].lower()
    s3_raw_key = f'applications/pricing/uk-vehicle-list-evaluator/{env}/raw/{data["task_id"]}.{file_ext}'
    s3_out_key = f'applications/pricing/uk-vehicle-list-evaluator/{env}/elaborated/{data["task_id"]}.{file_ext}'

    total_rows = None
    execution_time = None

    with tempfile.TemporaryDirectory() as tmpdir:
        local_in = os.path.join(tmpdir, f'raw_{data["task_id"]}.{file_ext}')
        local_out = os.path.join(tmpdir, f'out_{data["task_id"]}.{file_ext}')

        t0 = time.time()
        try:
            # I/O
            download_file(local_in, s3_bucketname, s3_raw_key)
            input_df = read_dataframe_file(local_in)
            total_rows = len(input_df)

            # Normalize for pipeline
            input_df = _normalize_columns_for_pipeline(input_df, task.get("supplier_name"))

            # Enrich with async pipeline (includes original columns; preserves order)
            results = await process_dataframe_async(input_df)
            for r in results:
                r.pop("row_index", None)
            merged_df = pd.DataFrame(results)

            # Snowflake enrichment (prod as per original)
            merged_df = enrich_with_stock_sales(
                merged_df,
                capid_col=None,
                environment="prod",
                fill_missing_zero=True,
            )

            # Remove unwanted column if present
            if "cap_registration_date" in merged_df.columns:
                merged_df = merged_df.drop(columns=["cap_registration_date"])

            # Fast XLSX write
            results_to_excel_fast(merged_df.to_dict(orient="records"), local_out)

            # Upload
            upload_file(local_out, s3_bucketname, s3_out_key)

            execution_time = time.time() - t0
            status, message = "success", None

        except Exception as e:
            execution_time = time.time() - t0
            status, message = "error", f"{e}"
            logger.error("Task %s failed: %s", data["task_id"], message)
            logger.debug("Traceback:\n%s", traceback.format_exc())

        # DB status
        with get_database_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE tasks SET status = %s, message = %s, total_rows = %s, execution_time = %s WHERE task_id = %s",
                    (status, message, total_rows, execution_time, data["task_id"]),
                )
                conn.commit()

    return {"message": "Task processing completed", "status": status, "rows": total_rows}
