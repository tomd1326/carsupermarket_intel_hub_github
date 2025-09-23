import os
import time
import base64
import hashlib
import logging
import datetime
import tempfile
import threading
import asyncio
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import pandas as pd
import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from starlette.responses import StreamingResponse

from utils.db_utils import get_database_connection
from utils.s3_tools import upload_file, download_file
from utils.file_handler import read_dataframe_file, write_dataframe_file
from list_processor.autotrader_client import AutoTraderClient
from list_processor.cap_client import CAPClient
from list_processor.columns_mapper import get_columns_mapper

# ---------------------------------------------------------------------------

BASE_URL = os.getenv("BASE_URL", "/dev")

router = APIRouter(
    prefix="",
    tags=["Main Router"],
    responses={404: {"description": "Not found"}},
)

cur_dir = os.path.dirname(__file__)
templates_dir = os.path.join(cur_dir, "templates")
templates = Jinja2Templates(directory=templates_dir)

logger = logging.getLogger(__name__)

# S3 bucket
s3_bucketname = "aa-raw-sources-uk"

# ---------------------------------------------------------------------------

def get_current_user(request: Request):
    user = request.session.get("user")
    if not user:
        return None
    return user

# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def root(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    return RedirectResponse(url=BASE_URL + "/tasks", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "BASE_URL": BASE_URL})


@router.post("/login")
def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    pwd_hash = hashlib.sha256(password.encode()).hexdigest()
    if username == "admin" and pwd_hash == "a05ef593239b0ff678c3bb69731e785c470d781a121692302912550aa2459b74":
        request.session["user"] = username
        return RedirectResponse(url=BASE_URL + "/tasks", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Invalid username or password", "BASE_URL": BASE_URL},
    )


@router.get("/logout")
def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse(url=BASE_URL + "/login", status_code=303)

# ---------------------------------------------------------------------------

@router.get("/tasks", response_class=HTMLResponse, name="read_tasks")
def read_tasks(
    request: Request,
    page: Optional[int] = 1,
    page_size: Optional[int] = 25,
    user: str = Depends(get_current_user),
):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)

    at_least_one_pending = False
    offset = (page - 1) * page_size

    with get_database_connection() as conn:
        # Suppliers
        with conn.cursor() as cursor:
            cursor.execute("SELECT supplier_id, supplier_name FROM suppliers ORDER BY supplier_name")
            cols = [d[0] for d in cursor.description]
            suppliers = [dict(zip(cols, row)) for row in cursor.fetchall()]

        # Count
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM tasks")
            total_tasks = cursor.fetchone()[0]

        # Page
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT *
                FROM tasks t
                LEFT JOIN suppliers s ON t.supplier_id = s.supplier_id
                ORDER BY t.task_id DESC
                LIMIT %s OFFSET %s
                """,
                (page_size, offset),
            )
            cols = [d[0] for d in cursor.description]
            tasks = []
            for row in cursor.fetchall():
                task = dict(zip(cols, row))
                tasks.append(task)
                if task["status"] == "pending":
                    at_least_one_pending = True

    total_pages = (total_tasks + page_size - 1) // page_size

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "tasks": tasks,
            "at_least_one_pending": at_least_one_pending,
            "suppliers": suppliers,
            "total_pages": total_pages,
            "current_page": page,
            "BASE_URL": BASE_URL,
        },
    )

# ---------------------------------------------------------------------------

def _run_elaborate_background(task_id: int):
    """Run elaborate_task in a background thread with its own event loop."""
    try:
        asyncio.run(elaborate_task({"task_id": task_id}))
    except Exception as e:
        logger.error(f"Background processing failed for task {task_id}: {e}")

# ---------------------------------------------------------------------------

@router.post("/upload-task")
async def upload_task(
    request: Request,
    fileBase64: str = Form(...),
    supplier_id: str = Form(...),
    filename: str = Form(...),
    previousFileBase64: Optional[str] = Form(None),
    previousfilename: Optional[str] = Form(None),
    user: str = Depends(get_current_user),
):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)

    # Read supplier_id from form (keeps existing behavior)
    form = await request.form()
    supplier_id = form.get("supplier_id")
    if not supplier_id:
        return RedirectResponse(url=BASE_URL + "/tasks", status_code=303)

    # Decode and basic validation
    content = base64.b64decode(fileBase64 or "")
    if not content:
        return RedirectResponse(url=BASE_URL + "/tasks", status_code=303)

    file_ext = filename.split(".")[-1].lower()
    if file_ext not in ["csv", "xls", "xlsx"]:
        return {"message": "Invalid file format. Only csv, xls and xlsx files are allowed."}

    # 1) Create the task as 'created'
    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO tasks (created, status, supplier_id, filename) "
                "VALUES (%s, %s, %s, %s) RETURNING task_id",
                (datetime.datetime.now(), "created", supplier_id, filename),
            )
            task_id = cursor.fetchone()[0]
            conn.commit()

    # 2) Upload main file to S3
    fd_input, temp_input_path = tempfile.mkstemp()
    os.close(fd_input)
    try:
        with open(temp_input_path, "wb") as f:
            f.write(content)

        env = "prod" if os.getenv("AWS_REGION") == "eu-west-1" else "dev"
        s3_raw_key = f"applications/pricing/uk-vehicle-list-evaluator/{env}/raw/{task_id}.{file_ext}"
        upload_file(temp_input_path, s3_bucketname, s3_raw_key)
        logger.info(f"Main file uploaded for task {task_id}")
    finally:
        try:
            if os.path.exists(temp_input_path):
                os.remove(temp_input_path)
        except Exception:
            pass

    # 3) Optional PREVIOUS file: store it alongside (no separate task for stage 1)
    if previousFileBase64 and previousfilename and previousFileBase64.strip() and previousfilename.strip():
        try:
            prev_bytes = base64.b64decode(previousFileBase64)
            if prev_bytes:
                prev_ext = previousfilename.split(".")[-1].lower()
                if prev_ext in ["csv", "xls", "xlsx"]:
                    fd_prev, tmp_prev_path = tempfile.mkstemp()
                    os.close(fd_prev)
                    try:
                        with open(tmp_prev_path, "wb") as pf:
                            pf.write(prev_bytes)
                        env = "prod" if os.getenv("AWS_REGION") == "eu-west-1" else "dev"
                        s3_prev_key = f"applications/pricing/uk-vehicle-list-evaluator/{env}/raw/{task_id}_previous.{prev_ext}"
                        upload_file(tmp_prev_path, s3_bucketname, s3_prev_key)
                        logger.info(f"Previous file uploaded for task {task_id}")
                    finally:
                        try:
                            if os.path.exists(tmp_prev_path):
                                os.remove(tmp_prev_path)
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"Previous file provided but failed to store: {e}")

    # 4) Mark 'pending' BEFORE returning (so UI shows countdown immediately)
    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE tasks SET status = 'pending', message = NULL WHERE task_id = %s", (task_id,))
            conn.commit()

    # 5) Kick off processing in background thread (non-blocking)
    t = threading.Thread(target=_run_elaborate_background, args=(task_id,), daemon=True)
    t.start()

    # 6) Redirect immediately — dashboard will show 'pending' and auto-refresh
    return RedirectResponse(url=BASE_URL + "/tasks", status_code=303)

# ---------------------------------------------------------------------------

@router.get("/download/{task_id}")
def download_task_file(task_id: str):
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

# ---------------------------------------------------------------------------

@router.get("/trigger-elaborate-task")
async def trigger_elaborate_task(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    task_id = int(request.query_params["task_id"])
    t = threading.Thread(target=_run_elaborate_background, args=(task_id,), daemon=True)
    t.start()
    return RedirectResponse(url=BASE_URL + "/tasks", status_code=303)

# ---------------------------------------------------------------------------

@router.post("/elaborate-task")
async def elaborate_task(data: dict):
    """
    Process a single task:
    - mark as pending (upload path already set it, but keep it idempotent)
    - download raw file
    - enrich with AutoTrader and CAP data
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

            def process_df(args):
                processor_obj, df, mapper = args
                return processor_obj.process_file(df, mapper)

            with ThreadPoolExecutor(max_workers=2) as ex:
                f1 = ex.submit(process_df, (autotrader, input_df, columns_mapper))
                f2 = ex.submit(process_df, (cap_client, input_df, columns_mapper))
                res1 = f1.result()
                res2 = f2.result()

            processed_df = pd.concat([input_df, res1, res2], axis=1)

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