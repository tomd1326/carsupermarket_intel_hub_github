# FILE PATH: source_code/router/backend/routes/upload_routes.py

import os
import base64
import datetime
import tempfile
import threading
import logging
from typing import Optional

from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse

from utils.db_utils import get_database_connection
from utils.s3_tools import upload_file
from .auth_routes import get_current_user
from .processing import _run_elaborate_background

BASE_URL = os.getenv("BASE_URL", "/dev")
logger = logging.getLogger(__name__)

# S3 bucket
s3_bucketname = "aa-raw-sources-uk"

router = APIRouter(prefix="", tags=["File Upload"])

# -------- Helper Functions --------

async def _process_upload(main_content: bytes, supplier_id: str, filename: str, 
                         buying_target: int,
                         prev_content: Optional[bytes], prev_filename: Optional[str]):
    """Common upload processing logic"""
    logger.info(f"Processing upload: filename='{filename}', supplier_id='{supplier_id}', buying_target={buying_target}, size={len(main_content)}")
    
    file_ext = filename.split(".")[-1].lower()
    
    # 1) Create the task as 'created' (NO buying_target in DB)
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
            f.write(main_content)

        env = "prod" if os.getenv("AWS_REGION") == "eu-west-1" else "dev"
        s3_raw_key = f"applications/pricing/uk-vehicle-list-evaluator/{env}/raw/{task_id}.{file_ext}"
        upload_file(temp_input_path, s3_bucketname, s3_raw_key)
        logger.info(f"File uploaded for task {task_id}")
    finally:
        try:
            if os.path.exists(temp_input_path):
                os.remove(temp_input_path)
        except Exception:
            pass

    # 3) Optional PREVIOUS file: store it alongside
    if prev_content and prev_filename:
        try:
            prev_ext = prev_filename.split(".")[-1].lower()
            if prev_ext in ["csv", "xls", "xlsx"]:
                fd_prev, tmp_prev_path = tempfile.mkstemp()
                os.close(fd_prev)
                try:
                    with open(tmp_prev_path, "wb") as pf:
                        pf.write(prev_content)
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

    # 4) Mark 'pending' BEFORE starting processing
    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE tasks SET status = 'pending', message = NULL WHERE task_id = %s", (task_id,))
            conn.commit()

    # 5) Check if running on Lambda
    is_lambda = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
    
    if is_lambda:
        # Lambda: run synchronously (await directly, uses existing event loop)
        logger.info(f"Lambda environment detected - running task {task_id} synchronously")
        await _run_elaborate_background(task_id, buying_target)
    else:
        # Local: use thread for fast UI response
        logger.info(f"Local environment detected - running task {task_id} in background thread")
        
        def _thread_wrapper():
            import asyncio
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                loop.run_until_complete(_run_elaborate_background(task_id, buying_target))
            finally:
                loop.close()
        
        t = threading.Thread(target=_thread_wrapper, daemon=True)
        t.start()

    # 6) Redirect to dashboard
    return RedirectResponse(url=BASE_URL + "/tasks", status_code=303)

# -------- Routes --------

@router.post("/upload-task")
async def upload_task_base64(
    request: Request,
    fileBase64: str = Form(...),
    supplier_id: str = Form(...),
    filename: str = Form(...),
    buying_target: int = Form(500),
    previousFileBase64: Optional[str] = Form(None),
    previousfilename: Optional[str] = Form(None),
    user: str = Depends(get_current_user),
):
    """Handle Base64 encoded file uploads (legacy)"""
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

    return await _process_upload(content, supplier_id, filename, buying_target, None, None)


@router.post("/upload-task-multipart")
async def upload_task_multipart(
    request: Request,
    user: str = Depends(get_current_user),
):
    """Handle multipart file uploads - matches frontend field names exactly"""
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)

    try:
        # Get form data dynamically (no FastAPI Form dependencies to avoid 422)
        form = await request.form()
        
        # Extract fields exactly as frontend sends them
        supplier_id = form.get("supplier_id")
        buying_target = int(form.get("buying_target", 500))
        
        # Frontend sends the actual file as 'filename' field (not 'file')
        main_file = form.get("filename")
        
        # Optional previous file is sent as 'previousfilename'  
        previous_file = form.get("previousfilename") if "previousfilename" in form else None

        # Validate
        if not supplier_id:
            return {"error": "supplier_id is required"}

        if not main_file or not hasattr(main_file, 'filename') or not main_file.filename:
            return {"error": "Main file is required"}
        
        # Validate buying_target
        if buying_target < 1:
            return {"error": "Buying target must be at least 1"}

        # Validate file extension
        file_ext = main_file.filename.split(".")[-1].lower()
        if file_ext not in ["csv", "xls", "xlsx"]:
            return {"error": f"Invalid file format: {file_ext}. Only csv, xls and xlsx allowed."}

        # Read main file content
        main_content = await main_file.read()
        if not main_content:
            return {"error": "Main file is empty"}

        logger.info(f"Processing upload: {main_file.filename}, size: {len(main_content)} bytes, buying_target: {buying_target}")

        # Handle optional previous file
        prev_content = None
        prev_filename = None
        if previous_file and hasattr(previous_file, 'filename') and previous_file.filename:
            prev_ext = previous_file.filename.split(".")[-1].lower()
            if prev_ext in ["csv", "xls", "xlsx"]:
                prev_content = await previous_file.read()
                prev_filename = previous_file.filename
                logger.info(f"Previous file: {prev_filename}, size: {len(prev_content)} bytes")

        # Process the upload
        return await _process_upload(main_content, supplier_id, main_file.filename, buying_target, prev_content, prev_filename)

    except Exception as e:
        logger.exception(f"Error in upload_task_multipart")
        return {"error": f"Server error: {str(e)}"}
