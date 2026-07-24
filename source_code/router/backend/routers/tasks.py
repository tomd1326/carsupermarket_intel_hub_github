"""Supplier stock evaluator tasks: upload, elaborate, downloads, enterprise email, stock fetch."""
import asyncio
import base64
import tempfile
import traceback

from fastapi import APIRouter, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
import os
from typing import Optional
import time
import logging

from botocore.config import Config
import pandas as pd

from utils.lambda_http import LambdaRequestsManager
from utils.s3_tools import upload_file, download_file
from utils.db_utils import (get_database_connection, get_settings)
from list_processor.autotrader_client import AutoTraderClient
from list_processor.batch_processor import process_file_with_cache
from list_processor.cap_client import CAPClient
from list_processor.columns_mapper import get_columns_mapper
from list_processor.motability_client import MotabilityFetchClient
from list_processor.onelink_client import OneLinkFetchClient
from utils.secrets_manager_client import get_secret
from utils.file_handler import read_dataframe_file, write_dataframe_file

from .core import (BASE_URL, _S3_ENV, templates, s3_bucketname, s3_bucket_region, _track,
                   get_current_user, _uk_now)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/tasks-json")
def tasks_json(page: int = 1, page_size: int = 25, user: str = Depends(get_current_user)):
    """Returns task list as JSON for in-place table updates."""
    if not user:
        raise HTTPException(status_code=401)
    offset = (page - 1) * page_size
    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM tasks")
            total_tasks = cursor.fetchone()[0]
        with conn.cursor() as cursor:
            cursor.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS progress_message TEXT")
            cursor.execute(
                "SELECT t.task_id, t.created, t.status, t.filename, t.total_rows, t.reductions, t.execution_time, t.message, s.supplier_name, t.uploaded_by, t.source, t.error_log_s3, t.aurora_cache_hits, t.progress_message "
                "FROM tasks t LEFT JOIN suppliers s ON t.supplier_id = s.supplier_id "
                "ORDER BY t.task_id DESC LIMIT %s OFFSET %s",
                (page_size, offset)
            )
            cols = [desc[0] for desc in cursor.description]
            tasks = []
            has_pending = False
            for row in cursor.fetchall():
                t = dict(zip(cols, row))
                t['created'] = t['created'].strftime("%Y-%m-%d %H:%M:%S") if t['created'] else None
                t['execution_time'] = round(t['execution_time']) if t['execution_time'] is not None else None
                tasks.append(t)
                if t['status'] == 'pending':
                    has_pending = True
    total_pages = (total_tasks + page_size - 1) // page_size
    return {"tasks": tasks, "has_pending": has_pending, "total_pages": total_pages, "current_page": page}
@router.get("/tasks", response_class=HTMLResponse, name="read_tasks")
def read_tasks(request: Request, page: Optional[int] = 1, page_size: Optional[int] = 25, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)

    at_least_one_pending = False
    offset = (page - 1) * page_size
    with get_database_connection() as conn:
        # Get the list of suppliers
        with conn.cursor() as cursor:
            cursor.execute("SELECT supplier_id, supplier_name FROM suppliers ORDER BY CASE WHEN supplier_name = 'Universal' THEN 1 ELSE 0 END, supplier_name")
            cols = [desc[0] for desc in cursor.description]
            suppliers = [dict(zip(cols, row)) for row in cursor.fetchall()]

        # Get the total number of tasks
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM tasks")
            total_tasks = cursor.fetchone()[0]

        # Get the tasks for the current page
        with conn.cursor() as cursor:
            # Adjust the query to use LIMIT and OFFSET for pagination
            cursor.execute("SELECT * FROM tasks t LEFT JOIN suppliers s ON t.supplier_id = s.supplier_id ORDER BY t.task_id DESC LIMIT %s OFFSET %s", (page_size, offset))
            # get as list of dicts
            cols = [desc[0] for desc in cursor.description]
            tasks = []
            for row in cursor.fetchall():
                task = dict(zip(cols, row))
                tasks.append(task)
                if task['status'] == 'pending':
                    at_least_one_pending = True

    total_pages = (total_tasks + page_size - 1) // page_size  # Calculate the total number of pages

    with get_database_connection() as conn:
        settings = get_settings(conn, user=user)
    _track(request, 'page_view', '/tasks')
    return templates.TemplateResponse("supplier_stock_evaluator.html", {
        "request": request,
        "user": user,
        "is_admin": user == 'tdrayson',
        "tasks": tasks,
        "at_least_one_pending": at_least_one_pending,
        'suppliers': suppliers,
        "total_pages": total_pages,
        "current_page": page,
        'BASE_URL': BASE_URL,
        'is_dev': os.getenv('AWS_REGION') != 'eu-west-1',
        'settings': settings,
    })


@router.post("/upload-task")
async def upload_task(request: Request, fileBase64: str = Form(...), supplier_id: str = Form(...), filename: str = Form(...), cap_workers: int = Form(20), cap_rate_limit: int = Form(100), user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)

    fd_input, temp_input_file_path = tempfile.mkstemp()
    os.close(fd_input)
    try:
        # Decode the base64 string to binary data
        content = base64.b64decode(fileBase64)
        print(f'File size from the form: {len(content)}')
        if not content:
            return RedirectResponse(url=BASE_URL + "/tasks", status_code=303)

        file_ext = filename.split('.')[-1]
        if file_ext not in ['csv', 'xls', 'xlsx']:
            return {"message": "Invalid file format. Only csv, xls and xlsx files are allowed."}

        # get the supplider_id from the form
        form = await request.form()
        supplier_id = form.get('supplier_id')
        if not supplier_id:
            return RedirectResponse(url=BASE_URL + "/tasks", status_code=303)

        with open(temp_input_file_path, 'wb') as temp_file:
            temp_file.write(content)

        _supplier_prefixes = {'1': 'Enterprise', '2': 'Motability', '3': '1link'}
        _prefix = _supplier_prefixes.get(str(supplier_id), '')
        if _prefix and not filename.startswith(f'{_prefix}_'):
            filename = f'{_prefix}_{filename}'

        # Create a task in the db
        with get_database_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("INSERT INTO tasks (created, status, supplier_id, filename, uploaded_by, source) VALUES (%s, %s, %s, %s, %s, %s) RETURNING task_id",
                               (_uk_now(), 'pending', supplier_id, filename, user, 'Upload'))
                task_id = cursor.fetchone()[0]
            conn.commit()
        _track(request, 'upload_task', '/tasks', {'supplier_id': supplier_id, 'filename': filename, 'task_id': task_id})

        env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
        s3_location = f'applications/pricing/uk-vehicle-list-evaluator/{env}/raw/{task_id}.{file_ext}'

        # upload the file to S3
        upload_file(temp_input_file_path, s3_bucketname, s3_location)

        # Execute the task elaboration
        aws_context = request.scope["aws.context"] if "aws.context" in request.scope else None
        with get_database_connection() as conn:
            _upload_settings = get_settings(conn)
        if aws_context is None:
            import asyncio
            asyncio.create_task(elaborate_task({'task_id': task_id, 'cap_workers': cap_workers, 'cap_rate_limit': cap_rate_limit, 'at_workers': 40, 'at_rate_limit': 60, 'valuation_cache_hours': _upload_settings['valuation_cache_hours']}))
        else:
            LambdaRequestsManager().simulate_http(
                aws_context=aws_context,
                path=BASE_URL + "/elaborate-task",
                method="POST",
                data={'task_id': task_id, 'cap_workers': cap_workers, 'cap_rate_limit': cap_rate_limit, 'at_workers': 40, 'at_rate_limit': 60, 'valuation_cache_hours': _upload_settings['valuation_cache_hours']},
            )
    finally:
        try:
            os.remove(temp_input_file_path)
        except OSError:
            pass

    return RedirectResponse(url=BASE_URL + "/tasks", status_code=303)


@router.get("/download/{task_id}", response_class=FileResponse)
def download_task_file(task_id: str):
    # Get task info from the db
    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT t.*, s.supplier_name FROM tasks t LEFT JOIN suppliers s ON s.supplier_id = t.supplier_id WHERE t.task_id = %s", (task_id,))
            cols = [desc[0] for desc in cursor.description]
            task = dict(zip(cols, cursor.fetchone()))

    if not task:
        return {"message": "Task not found"}

    _supplier_prefixes = {'enterprise': 'Enterprise', '1link': '1link', 'motability': 'Motability'}
    supplier_name = (task.get('supplier_name') or '').strip()
    prefix = _supplier_prefixes.get(supplier_name.lower(), '')
    prefix = f'{prefix}_' if prefix and not task['filename'].startswith(f'{prefix}_') else ''

    orig_ext = task['filename'].rsplit('.', 1)[-1].lower()
    _exts_to_try = ['xlsx'] if orig_ext == 'xlsx' else ['xlsx', orig_ext]

    _stored = task.get('s3_location')
    _candidate_keys = []
    if _stored:
        _candidate_keys.append((_stored, _stored.rsplit('.', 1)[-1]))
    for _ext in _exts_to_try:
        for _env in ('prod', 'dev'):
            _key = f'applications/pricing/uk-vehicle-list-evaluator/{_env}/elaborated/{task["task_id"]}.{_ext}'
            if not _stored or _key != _stored:
                _candidate_keys.append((_key, _ext))

    tmp_path = None
    _used_ext = 'xlsx'
    for _s3_key, _ext in _candidate_keys:
        fd, tmp_path = tempfile.mkstemp(suffix='.' + _ext)
        os.close(fd)
        try:
            download_file(tmp_path, s3_bucketname, _s3_key)
            _used_ext = _ext
            break
        except Exception:
            tmp_path = None
    if tmp_path is None:
        raise HTTPException(status_code=404, detail="Elaborated file not found on S3")
    base_name = task['filename'].rsplit('.', 1)[0]
    output_filename = prefix + base_name + '_output.' + _used_ext
    return FileResponse(
        tmp_path,
        filename=output_filename,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{output_filename}"'},
    )

@router.get("/download-error-log/{task_id}")
def download_error_log(task_id: str, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT error_log_s3 FROM tasks WHERE task_id = %s", (task_id,))
            row = cursor.fetchone()
    if not row or not row[0]:
        raise HTTPException(status_code=404, detail="No error log for this task")
    import boto3, tempfile
    s3 = boto3.client('s3', region_name='eu-west-1')
    tmp_fd, tmp_path = tempfile.mkstemp(suffix='.csv')
    os.close(tmp_fd)
    try:
        s3.download_file(s3_bucketname, row[0], tmp_path)
        return FileResponse(
            tmp_path,
            media_type='text/csv',
            headers={"Content-Disposition": f'attachment; filename="api_errors_{task_id}.csv"'},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/error-log/{task_id}")
def view_error_log(task_id: str, user: str = Depends(get_current_user)):
    """Returns the task's API error log as JSON rows for the popup table."""
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT error_log_s3 FROM tasks WHERE task_id = %s", (task_id,))
            row = cursor.fetchone()
    if not row or not row[0]:
        raise HTTPException(status_code=404, detail="No error log for this task")

    import boto3, csv, io as _io
    s3 = boto3.client('s3', region_name='eu-west-1')
    try:
        body = s3.get_object(Bucket=s3_bucketname, Key=row[0])['Body'].read().decode('utf-8', errors='replace')
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    reader = csv.DictReader(_io.StringIO(body))
    return {"columns": reader.fieldnames or [], "rows": list(reader)}


@router.get("/delete-task/{task_id}")
def delete_task(task_id: str, user: str = Depends(get_current_user)):
    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM tasks WHERE task_id = %s AND status = 'pending'", (task_id,))
        conn.commit()
    return {"ok": True}


@router.get("/tasks/{task_id}/vehicles-json")
def task_vehicles_json(task_id: str, user: str = Depends(get_current_user)):
    """Return the uploaded CSV's per-vehicle rows (VRM, CAP ID, Make, Model,
    Variant, Mileage) for the View popup. Currently used by Motability tasks so
    each vehicle can link out to the mfldirect search filtered by CAP ID."""
    if not user:
        raise HTTPException(status_code=401)

    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT t.filename, s.supplier_name FROM tasks t "
                "LEFT JOIN suppliers s ON s.supplier_id = t.supplier_id WHERE t.task_id = %s",
                (task_id,)
            )
            row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    filename, supplier_name = row

    orig_ext = filename.rsplit('.', 1)[-1].lower()
    # Motability Get Stock always writes the raw file as .csv regardless of the
    # stored filename; try that first, then the filename's own extension, across
    # both env folders so historical tasks (dev or prod) still resolve.
    exts_to_try = list(dict.fromkeys(['csv', orig_ext]))
    candidate_keys = [
        f'applications/pricing/uk-vehicle-list-evaluator/{env}/raw/{task_id}.{ext}'
        for env in ('prod', 'dev')
        for ext in exts_to_try
    ]

    input_df = None
    last_exc = None
    for s3_key in candidate_keys:
        ext = s3_key.rsplit('.', 1)[-1]
        fd, tmp_path = tempfile.mkstemp(suffix='.' + ext)
        os.close(fd)
        try:
            download_file(tmp_path, s3_bucketname, s3_key)
            input_df = read_dataframe_file(tmp_path)
            break
        except Exception as e:
            last_exc = e
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    if input_df is None:
        raise HTTPException(status_code=404, detail=f"Raw file not found on S3: {last_exc}")

    # Match the columns we want case-insensitively so header variations still work.
    wanted = {
        'vrm': ['reg no.', 'reg no', 'vrm', 'reg'],
        'cap_id': ['cap id', 'capid', 'cap_id'],
        'make': ['make'],
        'model': ['model'],
        'variant': ['variant', 'derivative'],
        'mileage': ['mileage', 'miles', 'odometer', 'odo'],
    }
    lower_cols = {str(c).strip().lower(): c for c in input_df.columns}
    resolved = {}
    for key, candidates in wanted.items():
        for cand in candidates:
            if cand in lower_cols:
                resolved[key] = lower_cols[cand]
                break

    rows = []
    for _, r in input_df.iterrows():
        item = {}
        for key, src_col in resolved.items():
            val = r.get(src_col)
            if pd.isnull(val):
                item[key] = None
            elif key in ('cap_id', 'mileage'):
                try:
                    item[key] = int(float(val))
                except (ValueError, TypeError):
                    item[key] = str(val).strip()
            else:
                item[key] = str(val).strip()
        rows.append(item)

    return {"supplier_name": supplier_name, "count": len(rows), "rows": rows}

@router.get("/trigger-elaborate-task")
async def trigger_elaborate_task(request: Request, user: str = Depends(get_current_user)):

    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)

    # Execute the task elaboration
    LambdaRequestsManager().simulate_http(
        aws_context=request.scope["aws.context"] if "aws.context" in request.scope else None,
        path=BASE_URL + "/elaborate-task",
        method="POST",
        data={
            'task_id': request.query_params['task_id'],
        }
    )

    return RedirectResponse(url=BASE_URL + "/tasks", status_code=303)

# ── Enterprise email inbox ────────────────────────────────────────────────────
@router.post("/api/enterprise-email/create-task")
def enterprise_email_create_task(
    filename: str = Form(...),
):
    """Called by Power Automate step 1: creates a DB task and returns a presigned S3 PUT URL."""
    file_ext = filename.split('.')[-1].lower()
    if file_ext not in ['xlsx', 'xls']:
        raise HTTPException(status_code=400, detail="Expected xlsx or xls")
    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT supplier_id FROM suppliers WHERE supplier_name = 'Enterprise' LIMIT 1")
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=500, detail="Enterprise supplier not found in DB")
            supplier_id = row[0]
            cursor.execute(
                "INSERT INTO tasks (created, status, supplier_id, filename, uploaded_by, source) VALUES (%s, %s, %s, %s, %s, %s) RETURNING task_id",
                (_uk_now(), 'pending', supplier_id, filename, 'email', 'Upload')
            )
            task_id = cursor.fetchone()[0]
            conn.commit()
    import boto3
    s3_key = f"applications/pricing/uk-vehicle-list-evaluator/{_S3_ENV}/raw/{task_id}.{file_ext}"
    presigned_url = boto3.client(
        's3',
        region_name=s3_bucket_region,
        config=Config(signature_version='s3v4'),
    ).generate_presigned_url(
        'put_object',
        Params={'Bucket': s3_bucketname, 'Key': s3_key},
        ExpiresIn=300
    )
    logger.info(f"Enterprise email: created task_id={task_id}, file={filename}")
    return {"task_id": task_id, "upload_url": presigned_url}


@router.post("/api/enterprise-email/start-task")
async def enterprise_email_start_task(
    request: Request,
    task_id: int = Form(...),
):
    """Called by Power Automate step 3: fires async elaboration after file is uploaded to S3."""
    with get_database_connection() as conn:
        settings = get_settings(conn)
    aws_context = request.scope["aws.context"] if "aws.context" in request.scope else None
    if aws_context is None:
        import asyncio
        asyncio.create_task(elaborate_task({'task_id': task_id, 'cap_workers': settings['cap_workers'], 'cap_rate_limit': settings['cap_rate_limit'], 'at_workers': settings['at_workers'], 'at_rate_limit': settings['at_rate_limit'], 'valuation_cache_hours': settings['valuation_cache_hours']}))
    else:
        LambdaRequestsManager().simulate_http(
            aws_context=aws_context,
            path=BASE_URL + "/elaborate-task",
            method="POST",
            data={'task_id': task_id, 'cap_workers': settings['cap_workers'], 'cap_rate_limit': settings['cap_rate_limit'], 'at_workers': settings['at_workers'], 'at_rate_limit': settings['at_rate_limit'], 'valuation_cache_hours': settings['valuation_cache_hours']},
        )
    logger.info(f"Enterprise email: started elaborate for task_id={task_id}")
    return {"status": "started", "task_id": task_id}

_supplier_fetch_lock = asyncio.Lock()


def _set_progress(task_id, message):
    """Write a live progress line to the task so the UI can show each step of a
    Get Stock run as it happens. Best-effort — never let logging break the fetch."""
    try:
        with get_database_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS progress_message TEXT")
                cursor.execute(
                    "UPDATE tasks SET progress_message = %s WHERE task_id = %s",
                    (str(message)[:500], task_id)
                )
            conn.commit()
    except Exception as e:
        logger.warning(f"_set_progress failed for task {task_id}: {e}")


@router.post("/api/fetch-supplier-stock")
async def fetch_supplier_stock(
    request: Request,
    supplier: str = Form(...),
    include_preview: Optional[str] = Form(None),
    user: str = Depends(get_current_user),
):
    if user != 'tdrayson':
        raise HTTPException(status_code=403, detail="Forbidden")

    supplier = supplier.lower()
    if supplier not in ('motability', '1link'):
        raise HTTPException(status_code=400, detail="supplier must be 'motability' or '1link'")

    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    secrets = get_secret(f'{env}/carsupermarket/car_pricer/api_access')
    logger.info('fetch_supplier_stock: starting %s fetch for %s', supplier, user)

    if _supplier_fetch_lock.locked():
        raise HTTPException(status_code=409, detail="A stock retrieval is already in progress. Please wait for it to finish.")

    now = _uk_now()
    supplier_label = 'Motability' if supplier == 'motability' else '1link'
    filename = f"{supplier_label}_{now.strftime('%Y%m%d_%H%M%S')}.csv"

    # Insert task immediately so it appears in the list while fetching
    try:
        with get_database_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT supplier_id FROM suppliers WHERE LOWER(supplier_name) = %s LIMIT 1",
                    (supplier,)
                )
                row = cursor.fetchone()
                if not row:
                    raise HTTPException(status_code=500, detail=f"Supplier '{supplier}' not found in DB")
                supplier_id = row[0]
                cursor.execute(
                    "INSERT INTO tasks (created, status, supplier_id, filename, uploaded_by, source) VALUES (%s, %s, %s, %s, %s, %s) RETURNING task_id",
                    (now, 'pending', supplier_id, filename, user, 'Get Stock')
                )
                task_id = cursor.fetchone()[0]
                conn.commit()
        _set_progress(task_id, 'Queued — waiting to start')
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("fetch_supplier_stock DB error: %s", e)
        raise HTTPException(status_code=503, detail=f"Database connection failed — check VPN/network connectivity. Detail: {e}")

    _FETCH_MAX_ATTEMPTS = 3
    _FETCH_RETRY_DELAY = 5  # seconds between attempts

    with get_database_connection() as conn:
        settings = get_settings(conn, user=user)
    # Fire the actual fetch in a background Lambda and return immediately
    aws_context = request.scope["aws.context"] if "aws.context" in request.scope else None
    fetch_data = {
        'task_id': task_id,
        'supplier': supplier,
        'include_preview': bool(include_preview),
        'cap_workers': settings['cap_workers'],
        'cap_rate_limit': settings['cap_rate_limit'],
        'at_workers': settings['at_workers'],
        'at_rate_limit': settings['at_rate_limit'],
        'valuation_cache_hours': settings['valuation_cache_hours'],
        'fetch_start_time': time.time(),
    }
    if aws_context is None:
        asyncio.create_task(_fetch_supplier_stock_internal(fetch_data))
    else:
        LambdaRequestsManager().simulate_http(
            aws_context=aws_context,
            path=BASE_URL + "/api/fetch-supplier-stock-internal",
            method="POST",
            data=fetch_data,
        )

    logger.info(f"fetch_supplier_stock: queued task_id={task_id} for {supplier}")
    return RedirectResponse(url=BASE_URL + "/tasks", status_code=303)


@router.post("/api/fetch-supplier-stock-internal")
async def fetch_supplier_stock_internal(data: dict):
    await _fetch_supplier_stock_internal(data)


async def _fetch_supplier_stock_internal(data: dict):
    task_id = data['task_id']
    supplier = data['supplier']
    include_preview = data.get('include_preview', False)

    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    secrets = get_secret(f'{env}/carsupermarket/car_pricer/api_access')

    _FETCH_MAX_ATTEMPTS = 3
    _FETCH_RETRY_DELAY = 5

    supplier_label = 'Motability' if supplier == 'motability' else '1link'

    async with _supplier_fetch_lock:
        last_exc = None
        input_df = None
        for attempt in range(1, _FETCH_MAX_ATTEMPTS + 1):
            try:
                attempt_note = '' if attempt == 1 else f' (attempt {attempt}/{_FETCH_MAX_ATTEMPTS})'
                _set_progress(task_id, f'Logging in to {supplier_label} and fetching stock list…{attempt_note}')
                if supplier == 'motability':
                    client = MotabilityFetchClient(
                        username=secrets['MOTABILITY_ID'],
                        password=secrets['MOTABILITY_PASSWORD'],
                    )
                    input_df = client.fetch_dataframe()
                    if not include_preview:
                        type_col = input_df.columns[1] if len(input_df.columns) > 1 else None
                        if type_col is not None:
                            input_df = input_df[input_df[type_col] != 'Preview']
                else:
                    client = OneLinkFetchClient(
                        username=secrets['1LINK_ID'],
                        password=secrets['1LINK_PASSWORD'],
                    )
                    input_df = client.fetch_dataframe()
                    input_df = _drop_1link_lcv_rows(input_df)
                _set_progress(task_id, f'Fetched {len(input_df)} vehicles from {supplier_label}')
                break
            except Exception as e:
                last_exc = e
                logger.warning(f"fetch_supplier_stock attempt {attempt}/{_FETCH_MAX_ATTEMPTS} failed for {supplier}: {e}")
                _set_progress(task_id, f'Fetch attempt {attempt}/{_FETCH_MAX_ATTEMPTS} failed: {e}')
                if attempt < _FETCH_MAX_ATTEMPTS:
                    await asyncio.sleep(_FETCH_RETRY_DELAY)

        if input_df is None:
            _set_progress(task_id, f'Failed to fetch stock after {_FETCH_MAX_ATTEMPTS} attempts: {last_exc}')
            with get_database_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE tasks SET status = 'error', message = %s WHERE task_id = %s",
                        (str(last_exc)[:500], task_id)
                    )
                conn.commit()
            logger.error(f"fetch_supplier_stock failed for {supplier} after {_FETCH_MAX_ATTEMPTS} attempts: {last_exc}", exc_info=True)
            return

    _set_progress(task_id, 'Uploading stock list to storage…')
    fd, tmp_path = tempfile.mkstemp(suffix='.csv')
    os.close(fd)
    try:
        input_df.to_csv(tmp_path, index=False)
        s3_key = f'applications/pricing/uk-vehicle-list-evaluator/{env}/raw/{task_id}.csv'
        upload_file(tmp_path, s3_bucketname, s3_key)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    _set_progress(task_id, 'Enriching with CAP & Autotrader data…')
    logger.info(f"fetch_supplier_stock: fetched task_id={task_id} for {supplier} ({len(input_df)} rows)")
    await elaborate_task({'task_id': task_id, 'cap_workers': data['cap_workers'], 'cap_rate_limit': data['cap_rate_limit'], 'at_workers': data['at_workers'], 'at_rate_limit': data['at_rate_limit'], 'valuation_cache_hours': data.get('valuation_cache_hours'), 'start_time': data.get('fetch_start_time')})


def _drop_1link_lcv_rows(input_df):
    """LCV rows carry incorrect CAP IDs so CAP/AT lookups fail — drop them entirely.

    Applied on elaboration (not just on fetch) so it covers manually uploaded
    1link files as well as those pulled via Get Stock.
    """
    make_col = next((c for c in input_df.columns if str(c).strip().lower() == 'make'), None)
    if make_col is None:
        logger.warning(f"1link: no Make column found for LCV filter. Columns: {list(input_df.columns)[:20]}")
        return input_df
    # Require a separator after LCV so a make merely starting with those
    # letters is not dropped — 1link prefixes the real make, e.g. "LCV Ford".
    lcv_mask = input_df[make_col].astype(str).str.strip().str.upper().str.match(r'LCV[\s\-_]')
    if lcv_mask.any():
        logger.info(f"1link: excluding {int(lcv_mask.sum())} rows with Make beginning 'LCV'")
        return input_df[~lcv_mask]
    return input_df


@router.post("/elaborate-task")
async def elaborate_task(data: dict):
    tmp_in = tmp_out = None
    try:
        # Fetch the task data from the db
        with get_database_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT t.*, s.supplier_name FROM tasks t LEFT JOIN suppliers s ON s.supplier_id = t.supplier_id WHERE t.task_id = %s", (data['task_id'],))
                cols = [desc[0] for desc in cursor.description]
                row = cursor.fetchone()
                task = dict(zip(cols, row)) if row else None
                if not task:
                    return {"message": "Task not found"}

        env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
        file_ext = task['filename'].split('.')[-1]
        s3_location = f'applications/pricing/uk-vehicle-list-evaluator/{env}/raw/{data["task_id"]}.{file_ext}'
        total_rows = None
        execution_time = None
        error_log_s3 = None
        aurora_cache_hits = None
        api_error_log = []

        fd_in,  tmp_in  = tempfile.mkstemp(suffix='.' + file_ext)
        fd_out, tmp_out = tempfile.mkstemp(suffix='.xlsx')
        os.close(fd_in)
        os.close(fd_out)

        try:
            t = data.get('start_time') or time.time()

            download_file(tmp_in, s3_bucketname, s3_location)

            input_df = read_dataframe_file(tmp_in)
            if task['supplier_name'] == '1link':
                input_df = _drop_1link_lcv_rows(input_df)
            total_rows = len(input_df)

            cap_workers = int(data.get('cap_workers', 20))
            cap_rate_limit = int(data.get('cap_rate_limit', 100))
            at_workers = int(data.get('at_workers', 40))
            at_rate_limit = int(data.get('at_rate_limit', 60))
            valuation_cache_hours = int(data['valuation_cache_hours']) if data.get('valuation_cache_hours') is not None else None
            logger.info(f"CAP settings: workers={cap_workers}, rate_limit={cap_rate_limit} | AT settings: workers={at_workers}, rate_limit={at_rate_limit} | valuation_cache_hours={valuation_cache_hours}")

            autotrader = AutoTraderClient()
            cap_client = CAPClient()
            columns_mapper = get_columns_mapper(task['supplier_name'])

            snapshot_suppliers = {'Enterprise', 'Motability', '1link', 'Purchase Pro'}
            is_snapshot = task['supplier_name'] in snapshot_suppliers

            logger.info('Starting elaboration for task %s: %s, %d rows', data['task_id'], task['supplier_name'], total_rows)

            with get_database_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT created FROM tasks WHERE supplier_id = %s AND status = 'success' AND task_id != %s ORDER BY task_id DESC LIMIT 1",
                        (task['supplier_id'], data['task_id'])
                    )
                    prev_row = cursor.fetchone()
            prev_task_created = prev_row[0] if prev_row else None

            processed_df, reductions, api_error_log, aurora_cache_hits = process_file_with_cache(
                input_df=input_df,
                columns_mapper=columns_mapper,
                supplier_id=task['supplier_id'],
                supplier_name=task['supplier_name'],
                is_snapshot=is_snapshot,
                at_client=autotrader,
                cap_client=cap_client,
                cap_workers=cap_workers,
                cap_rate_limit=cap_rate_limit,
                at_workers=at_workers,
                at_rate_limit=at_rate_limit,
                prev_task_created=prev_task_created,
                valuation_cache_hours=valuation_cache_hours,
            )
            write_dataframe_file(processed_df, tmp_out)

            s3_location = f'applications/pricing/uk-vehicle-list-evaluator/{env}/elaborated/{data["task_id"]}.xlsx'
            upload_file(tmp_out, s3_bucketname, s3_location)

            error_log_s3 = None
            if api_error_log:
                import csv, io as _io
                buf = _io.StringIO()
                writer = csv.DictWriter(buf, fieldnames=['timestamp', 'api', 'vrm', 'mileage', 'cap_id', 'status_code', 'error_type', 'detail'])
                writer.writeheader()
                writer.writerows(sorted(api_error_log, key=lambda r: r.get('timestamp', '')))
                error_log_key = f'applications/pricing/uk-vehicle-list-evaluator/{env}/error-logs/{data["task_id"]}.csv'
                try:
                    import boto3
                    boto3.client('s3', region_name='eu-west-1').put_object(
                        Bucket=s3_bucketname, Key=error_log_key,
                        Body=buf.getvalue().encode(), ContentType='text/csv',
                    )
                    error_log_s3 = error_log_key
                except Exception as _e:
                    logger.warning(f"Failed to write API error log to S3: {_e}")

            execution_time = time.time() - t
            status = 'success'
            message = None
        except Exception as e:
            logger.error(f"Error processing file: {e}, traceback: {traceback.format_exc()}")
            status = 'error'
            message = str(e)[:250]
            # Write the traceback to S3 so it's downloadable from the UI
            try:
                import csv, io as _io, boto3 as _boto3
                buf = _io.StringIO()
                writer = csv.DictWriter(buf, fieldnames=['timestamp', 'api', 'vrm', 'mileage', 'cap_id', 'status_code', 'error_type', 'detail'])
                writer.writeheader()
                rows_to_write = sorted(api_error_log, key=lambda r: r.get('timestamp', '')) if api_error_log else []
                rows_to_write.append({
                    'timestamp': _uk_now().strftime('%Y-%m-%d %H:%M:%S'),
                    'api': 'TASK',
                    'vrm': None,
                    'mileage': None,
                    'cap_id': None,
                    'status_code': None,
                    'error_type': 'EXCEPTION',
                    'detail': traceback.format_exc()[:2000],
                })
                writer.writerows(rows_to_write)
                _env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
                error_log_key = f'applications/pricing/uk-vehicle-list-evaluator/{_env}/error-logs/{data["task_id"]}.csv'
                _boto3.client('s3', region_name='eu-west-1').put_object(
                    Bucket=s3_bucketname, Key=error_log_key,
                    Body=buf.getvalue().encode(), ContentType='text/csv',
                )
                error_log_s3 = error_log_key
            except Exception as _e:
                logger.warning(f"Failed to write error log to S3: {_e}")

        progress_final = None if status == 'success' else f'Error: {message}'
        with get_database_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS error_log_s3 TEXT")
                cursor.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS aurora_cache_hits INTEGER")
                cursor.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS progress_message TEXT")
                cursor.execute(
                    "UPDATE tasks SET status = %s, message = %s, total_rows = %s, execution_time = %s, s3_location = %s, reductions = %s, error_log_s3 = %s, aurora_cache_hits = %s, progress_message = %s WHERE task_id = %s",
                    (status, message, total_rows, execution_time, s3_location if status == 'success' else None, reductions if status == 'success' else None, error_log_s3, aurora_cache_hits if status == 'success' else None, progress_final, data['task_id']),
                )
                conn.commit()

    except Exception as e:
        logger.error(f"Unhandled error in elaborate_task: {e}, traceback: {traceback.format_exc()}")
        # Last-resort: mark the task as error so it never stays stuck at pending
        try:
            with get_database_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE tasks SET status = 'error', message = %s WHERE task_id = %s AND status = 'pending'",
                        (str(e)[:250], data['task_id']),
                    )
                conn.commit()
        except Exception as _e:
            logger.error(f"Failed to mark task as error after unhandled exception: {_e}")

    finally:
        for p in (tmp_in, tmp_out):
            if p is None:
                continue
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
