# FILE PATH: source_code/router/backend/routes/download_routes.py

import os
import logging

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter
from starlette.responses import StreamingResponse

from utils.db_utils import get_database_connection

logger = logging.getLogger(__name__)

# S3 bucket
s3_bucketname = "aa-raw-sources-uk"

router = APIRouter(prefix="", tags=["File Download"])

# -------- Routes --------

@router.get("/download/{task_id}")
def download_task_file(task_id: str):
    """Download processed file for a completed task"""
    # Get task info from DB
    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM tasks WHERE task_id = %s", (task_id,))
            row = cursor.fetchone()
            if not row:
                return {"message": "Task not found"}
            cols = [d[0] for d in cursor.description]
            task = dict(zip(cols, row))

    file_ext = task["filename"].split(".")[-1].lower()
    env = "prod" if os.getenv("AWS_REGION") == "eu-west-1" else "dev"
    s3_key = f'applications/pricing/uk-vehicle-list-evaluator/{env}/elaborated/{task["task_id"]}.{file_ext}'

    # Nice filename for download
    output_filename = f'{task["filename"].rsplit(".", 1)[0]}_output.{file_ext}'

    # Content type
    if file_ext == "xlsx":
        content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif file_ext == "xls":
        content_type = "application/vnd.ms-excel"
    elif file_ext == "csv":
        content_type = "text/csv"
    else:
        content_type = "application/octet-stream"

    s3 = boto3.client("s3")

    try:
        obj = s3.get_object(Bucket=s3_bucketname, Key=s3_key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in {"NoSuchKey", "404"}:
            return {"message": "Output file not ready yet. Please try again shortly."}
        logger.error(f"Error fetching {s3_key} from S3: {e}")
        return {"message": "Error fetching the file."}

    body = obj["Body"]  # botocore.response.StreamingBody

    def iter_chunks():
        try:
            for chunk in body.iter_chunks(chunk_size=64 * 1024):
                if chunk:
                    yield chunk
        finally:
            try:
                body.close()
            except Exception:
                pass

    headers = {"Content-Disposition": f'attachment; filename="{output_filename}"'}
    return StreamingResponse(iter_chunks(), media_type=content_type, headers=headers)