from __future__ import annotations

import base64
import tempfile
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter
from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.responses import FileResponse
from fastapi import File, UploadFile
import json
import os
import requests
import time
import hashlib
import logging
import datetime

from utils.lambda_http import LambdaRequestsManager
from utils.s3_tools import upload_file, download_file
from utils.db_utils import (get_database_connection)
from utils.snowflake_client import SnowflakeClient
from utils.sharepoint_client import SharePointClient
from list_processor.autotrader_client import AutoTraderClient
from list_processor.cap_client import CAPClient
from list_processor.columns_mapper import get_columns_mapper, BaseColumnsMapper
from utils.secrets_manager_client import get_secret
from utils.file_handler import read_dataframe_file, write_dataframe_file
import pandas as pd


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

# Generic initialization
s3_bucketname = 'aa-raw-sources-uk'


def get_current_user(request: Request):
    user = request.session.get('user')
    if not user:
        return None
        # raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="Unauthorized", headers={"Location": "/login"})
        # redirect to login page
        # return RedirectResponse(url="/login", status_code=303)
    return user


@router.get("/", response_class=HTMLResponse)
def root(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    #
    return RedirectResponse(url=BASE_URL + "/home", status_code=303)  # CHANGED: was /tasks
    # return templates.TemplateResponse("dashboard.html", {"request": request, "user": user})


@router.get("/login", response_class=HTMLResponse)
def login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, 'BASE_URL': BASE_URL})


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    # calculate sha256 hash of the password
    pwd_hash = hashlib.sha256(password.encode()).hexdigest()
    if username == 'admin' and pwd_hash == "a05ef593239b0ff678c3bb69731e785c470d781a121692302912550aa2459b74":
        request.session['user'] = username
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)  # CHANGED: was /tasks
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": "Invalid username or password",
            'BASE_URL': BASE_URL
        }
    )


@router.get("/logout")
def logout(request: Request):
    request.session.pop('user', None)
    return RedirectResponse(url=BASE_URL + "/login", status_code=303)


# NEW
@router.get("/home", response_class=HTMLResponse)
def home(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    return templates.TemplateResponse("home.html", {"request": request, "user": user, "BASE_URL": BASE_URL})


# NEW
@router.get("/buying-requirements", response_class=HTMLResponse)
def buying_requirements(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    return templates.TemplateResponse("buying_requirements.html", {"request": request, "user": user, "BASE_URL": BASE_URL})


# NEW
@router.get("/buying-requirements/test-snowflake")
def test_snowflake(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)

    TARGET_DB = "PROD_CARSUPERMARKET_DB"
    TARGET_SCHEMA = "product_vehicle_journey"
    TARGET_TABLE_NAME = "buying_requirements"
    TARGET_TABLE = f"{TARGET_DB}.{TARGET_SCHEMA}.{TARGET_TABLE_NAME}"

    try:
        with SnowflakeClient() as sf:
            rows, _ = sf.execute("SELECT CURRENT_WAREHOUSE()")
            warehouse = rows[0][0]

            # Use metadata — no table scan
            meta_rows, _ = sf.execute(
                "SELECT ROW_COUNT FROM information_schema.tables "
                "WHERE table_catalog = %s AND table_schema = %s AND table_name = %s",
                [TARGET_DB, TARGET_SCHEMA.upper(), TARGET_TABLE_NAME.upper()]
            )
            row_count = meta_rows[0][0] if meta_rows else None

        return {
            "status": "ok",
            "warehouse": warehouse,
            "target_table": TARGET_TABLE,
            "row_count": row_count,
        }
    except Exception as e:
        logger.error(f"Snowflake test failed: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/buying-requirements/sf-data")
def buying_requirements_sf_data(request: Request, force: bool = False, user: str = Depends(get_current_user)):
    """Snowflake rows — served from cache on page load, forced live on Refresh Data."""
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    try:
        sf_rows = _get_sf_rows(force=force)
        return {"status": "ok", "rows": sf_rows}
    except Exception as e:
        logger.error(f"Buying requirements SF data failed: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/buying-requirements/sp-purchases")
def buying_requirements_sp_purchases(request: Request, user: str = Depends(get_current_user)):
    """SharePoint purchases — always fetches live (called by Refresh Data)."""
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    try:
        purchases = _get_sp_purchases(force=True)
        out = {f"{k[0]}|{k[1]}": v for k, v in purchases.items()}
        return {"status": "ok", "purchases": out}
    except Exception as e:
        logger.error(f"Buying requirements SP purchases failed: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/buying-requirements/sp-purchases-cached")
def buying_requirements_sp_purchases_cached(request: Request, user: str = Depends(get_current_user)):
    """SP purchases from cache only — never triggers a live SharePoint fetch."""
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    try:
        purchases = _get_sp_purchases_cached()
        if purchases is None:
            return {"status": "ok", "purchases": {}}
        # S3 cache already has string keys; in-process cache has tuple keys
        if purchases and isinstance(next(iter(purchases)), tuple):
            purchases = {f"{k[0]}|{k[1]}": v for k, v in purchases.items()}
        return {"status": "ok", "purchases": purchases}
    except Exception as e:
        logger.error(f"Buying requirements SP cached failed: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/buying-requirements/data")
def buying_requirements_data(request: Request, user: str = Depends(get_current_user)):
    """Combined endpoint (kept for compatibility). Merges SF + SP in one call."""
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    try:
        sf_rows   = _get_sf_rows()
        purchases = _get_sp_purchases()
        out = []
        for r in sf_rows:
            cap_id = r[0]
            price_band = r[5]
            today = purchases.get((cap_id, price_band), 0)
            out.append([cap_id, r[1], r[2], r[3], r[4], None, today,
                        r[6], r[7], r[8], r[9], r[10], r[11], r[12], r[13], r[14], r[15], r[16]])
        return {"status": "ok", "rows": out}
    except Exception as e:
        logger.error(f"Buying requirements data failed: {e}")
        return {"status": "error", "message": str(e)}


_SP_SUBFOLDER = "Department/Buying/B2B"
_SP_FILENAME  = "NEW DAILY BUYERS SHEET.xlsx"
_SP_SHEET     = "Today"
_SP_COLUMNS   = ["CAP ID", "RRP", "RRP 0%"]

_SF_DB     = "PROD_CARSUPERMARKET_DB"
_SF_SCHEMA = "product_vehicle_journey"
_SF_TABLE  = "buying_requirements"

# Price band boundaries (upper-inclusive), matching the UI bands
_PRICE_BANDS = [
    (10_000,   "<10k"),
    (12_500,   "10-12.5k"),
    (15_000,   "12.5-15k"),
    (20_000,   "15-20k"),
    (25_000,   "20-25k"),
    (30_000,   "25-30k"),
]
_PRICE_BAND_DEFAULT = "30k+"


def _rrp_to_price_band(rrp) -> str:
    """Map a numeric RRP (number or '£14,698' string) to a price band label."""
    try:
        v = float(str(rrp).replace('£', '').replace(',', '').strip())
    except (TypeError, ValueError):
        return _PRICE_BAND_DEFAULT
    for ceiling, label in _PRICE_BANDS:
        if v < ceiling:
            return label
    return _PRICE_BAND_DEFAULT


# ── Caches ───────────────────────────────────────────────────────────────────
# Snowflake: data changes at fixed times (03:20, 08:20, 11:20, 14:20, 17:20).
#   - Persisted to S3 so Lambda cold starts are instant.
#   - In-process copy avoids repeated S3 reads within the same Lambda instance.
#   - TTL = 3h (safely shorter than the minimum gap between refreshes).
# SharePoint: live buyer activity — 60-second TTL, in-process only.
_sf_cache: dict = {"rows": None, "expires": 0.0}
_sp_cache: dict = {"purchases": None, "expires": 0.0}
_cache_lock = threading.Lock()

_SF_TTL_SECONDS = 3 * 3600
_SP_TTL_SECONDS = 60          # in-process TTL
_SP_S3_TTL_SECONDS = 24 * 3600  # S3 TTL — survives cold starts across the day
_S3_BUCKET      = "aa-raw-sources-uk"
_S3_ENV         = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
_SF_S3_KEY      = f"applications/pricing/uk-vehicle-list-evaluator/{_S3_ENV}/cache/buying_requirements_v13.json"
_SP_S3_KEY      = f"applications/pricing/uk-vehicle-list-evaluator/{_S3_ENV}/cache/sp_purchases_v4.json"


def _s3_load(key: str) -> dict | None:
    """Read a JSON payload from S3. Returns None if missing or expired."""
    try:
        import boto3
        obj = boto3.client("s3").get_object(Bucket=_S3_BUCKET, Key=key)
        payload = json.loads(obj["Body"].read())
        if time.time() < payload["expires_at"]:
            return payload
    except Exception:
        pass
    return None


def _s3_save(key: str, data: dict, ttl: int) -> None:
    """Write a JSON payload to S3 with an absolute expiry timestamp."""
    try:
        import boto3
        data["expires_at"] = time.time() + ttl
        boto3.client("s3").put_object(
            Bucket=_S3_BUCKET, Key=key,
            Body=json.dumps(data, default=_json_default).encode(),
            ContentType="application/json",
        )
    except Exception as e:
        logger.warning(f"Failed to write S3 cache {key}: {e}")


def _load_sf_cache_from_s3() -> list | None:
    payload = _s3_load(_SF_S3_KEY)
    return payload["rows"] if payload else None


def _json_default(obj):
    from decimal import Decimal
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serialisable")


def _save_sf_cache_to_s3(rows: list) -> None:
    _s3_save(_SF_S3_KEY, {"rows": rows}, _SF_TTL_SECONDS)


def _load_sp_cache_from_s3() -> dict | None:
    payload = _s3_load(_SP_S3_KEY)
    return payload["purchases"] if payload else None


def _save_sp_cache_to_s3(purchases: dict, ttl: int = _SP_TTL_SECONDS) -> None:
    _s3_save(_SP_S3_KEY, {"purchases": {f"{k[0]}|{k[1]}": v for k, v in purchases.items()}}, ttl)


def _get_sf_rows(force: bool = False) -> list:
    """Return Snowflake rows — from in-process cache, S3 cache, or live query."""
    now = time.monotonic()
    with _cache_lock:
        if not force and _sf_cache["rows"] is not None and now < _sf_cache["expires"]:
            return _sf_cache["rows"]

    # Try S3 cache before hitting Snowflake (skip on force)
    result = None if force else _load_sf_cache_from_s3()

    if result is None:
        # S3 miss — query Snowflake and repopulate both caches
        with SnowflakeClient() as sf:
            rows, _ = sf.execute("""
                SELECT
                    CAP_ID,
                    COALESCE(MAKE_MODEL, '') || ' ' || COALESCE(CAP_DERIVATIVE, '') || ' / ' || COALESCE(PRICE_BAND, '') AS LABEL,
                    COALESCE(FUEL_TYPE, '')          AS FUEL_TYPE,
                    COALESCE(PRICE_BAND, '')         AS PRICE_BAND,
                    STOCK,
                    SALES_90_DAYS,
                    SALES_7_DAYS,
                    DAYS_IN_STOCK_ACKNOWLEDGED,
                    DAYS_ADVERTISED_ACKNOWLEDGED,
                    GPU_M,
                    GPU_S,
                    GPU_MS,
                    PURCHASE_PRICE_INC_VAT,
                    SELLING_PRICE_INC_VAT,
                    AUTOTRADER_LATEST_RETAIL_VALUATION,
                    AUTOTRADER_PERCENT,
                    AUTOTRADER_LATEST_RETAIL_RATING,
                    ORDER_ACKNOWLEDGED_DATE,
                    COALESCE(VRM, '') AS VRM,
                    COALESCE(SUPPLIER_NAME, '') AS SUPPLIER_NAME,
                    VEHICLE_URL,
                    COALESCE(VEHICLE_STATUS, '') AS VEHICLE_STATUS,
                    COALESCE(GPU_M, STOCK_EVOLUTION_GPU_M) AS GPU_M_RESOLVED,
                    COALESCE(VEHICLE_LOCATION, '') AS VEHICLE_LOCATION,
                    DATE_FIRST_REGISTERED,
                    MILEAGE,
                    AUTOTRADER_PORTAL_LINK,
                    INVOICE_POSTED_DATE,
                    VEHICLE_UPDATED_AT
                FROM PROD_CARSUPERMARKET_DB.product_vehicle_journey.buying_requirements
                ORDER BY CAP_ID
            """)
        # One row per vehicle:
        # [0:cap_id, 1:label, 2:fuel, 3:price_band, 4:stock, 5:sales90, 6:sales7,
        #  7:dis, 8:dts, 9:gpu_m, 10:gpu_s, 11:gpu_ms,
        #  12:purchase_price, 13:sell_price, 14:at_val, 15:at_pct, 16:at_rating,
        #  17:order_acknowledged_date (ISO string), 18:vrm, 19:supplier_name, 20:vehicle_url,
        #  21:vehicle_status, 22:gpu_m_resolved, 23:vehicle_location,
        #  24:date_first_registered, 25:mileage, 26:autotrader_portal_link,
        #  27:invoice_posted_date]
        result = [[r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11], r[12], r[13], r[14], r[15], r[16],
                   str(r[17]) if r[17] is not None else None, r[18], r[19], r[20], r[21], r[22], r[23],
                   str(r[24]) if r[24] is not None else None, r[25], r[26],
                   str(r[27]) if r[27] is not None else None,
                   str(r[28]) if r[28] is not None else None] for r in rows]
        _save_sf_cache_to_s3(result)

    with _cache_lock:
        _sf_cache["rows"] = result
        _sf_cache["expires"] = now + _SF_TTL_SECONDS
    return result


def _get_sp_purchases(force: bool = False) -> dict:
    """Return cached SharePoint purchase counts per (cap_id, price_band), fetching if stale."""
    now = time.monotonic()
    with _cache_lock:
        if not force and _sp_cache["purchases"] is not None and now < _sp_cache["expires"]:
            return _sp_cache["purchases"]

    values = SharePointClient().get_worksheet_values(_SP_SUBFOLDER, _SP_FILENAME, _SP_SHEET)
    headers = values[0] if values else []
    purchases: dict = {}

    if headers:
        try:
            cap_idx  = headers.index("CAP ID")
            rrp_idx  = headers.index("RRP")
            rrp0_idx = headers.index("RRP 0%")
        except ValueError:
            pass
        else:
            for row in values[1:]:
                try:
                    cap_id = int(row[cap_idx]) if row[cap_idx] not in (None, '') else None
                except (TypeError, ValueError):
                    cap_id = None
                if cap_id is None:
                    continue

                rrp0 = row[rrp0_idx] if rrp0_idx < len(row) else None
                try:
                    rrp0_val = float(str(rrp0).replace('£', '').replace(',', '').strip()) if rrp0 not in (None, '') else 0
                    is_if = rrp0_val > 0
                except (TypeError, ValueError):
                    is_if = False
                if is_if:
                    band = "interest_free"
                else:
                    rrp = row[rrp_idx] if rrp_idx < len(row) else None
                    band = _rrp_to_price_band(rrp)

                key = (cap_id, band)
                purchases[key] = purchases.get(key, 0) + 1

    logger.info(f"SP: {len(purchases)} purchase keys from {len(values)-1 if values else 0} data rows")
    with _cache_lock:
        _sp_cache["purchases"] = purchases
        _sp_cache["expires"] = time.monotonic() + _SP_TTL_SECONDS
    _save_sp_cache_to_s3(purchases, _SP_S3_TTL_SECONDS)
    return purchases


def _get_sp_purchases_cached() -> dict | None:
    """Return SP purchases from in-process or S3 cache only — never fetches live."""
    with _cache_lock:
        if _sp_cache["purchases"] is not None and time.monotonic() < _sp_cache["expires"]:
            return _sp_cache["purchases"]
    return _load_sp_cache_from_s3()


def _sse(message: str, kind: str = "log") -> str:
    return f"data: {json.dumps({'type': kind, 'message': message})}\n\n"


@router.get("/buying-requirements/stream-snowflake")
def stream_snowflake(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)

    def generate():
        try:
            yield _sse("Connecting to Snowflake...")
            with SnowflakeClient() as sf:
                yield _sse("Connected. Getting warehouse...")
                rows, _ = sf.execute("SELECT CURRENT_WAREHOUSE()")
                warehouse = rows[0][0]
                yield _sse(f"Warehouse: {warehouse}")

                yield _sse(f"Querying {_SF_DB}.{_SF_SCHEMA}.{_SF_TABLE}...")
                meta_rows, _ = sf.execute(
                    "SELECT ROW_COUNT FROM information_schema.tables "
                    "WHERE table_catalog = %s AND table_schema = %s AND table_name = %s",
                    [_SF_DB, _SF_SCHEMA.upper(), _SF_TABLE.upper()]
                )
                row_count = meta_rows[0][0] if meta_rows else None
                yield _sse(f"Row count: {row_count:,}" if row_count is not None else "Row count: unknown", "done")
        except Exception as e:
            logger.error(f"Snowflake stream failed: {e}")
            yield _sse(str(e), "error")

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.get("/buying-requirements/stream-sharepoint")
def stream_sharepoint(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)

    def generate():
        try:
            yield _sse("Authenticating with SharePoint...")
            client = SharePointClient()
            yield _sse("Auth OK. Resolving site...")

            site_id = client._get_site_id()
            yield _sse("Got site. Resolving drive...")

            drive_id = client._get_drive_id(site_id)
            yield _sse("Got drive. Resolving file...")

            item_id = client._get_item_id(drive_id, _SP_SUBFOLDER, _SP_FILENAME)
            yield _sse(f"Got file. Reading '{_SP_SHEET}' sheet...")

            encoded_sheet = requests.utils.quote(_SP_SHEET)
            url = (
                f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}"
                f"/workbook/worksheets/{encoded_sheet}/usedRange?$select=values"
            )
            resp = requests.get(url, headers=client._headers)
            resp.raise_for_status()
            values = resp.json().get("values", [])

            headers = values[0] if values else []
            missing = [c for c in _SP_COLUMNS if c not in headers]
            if missing:
                yield _sse(f"Missing columns: {missing}", "error")
                return

            indices = [headers.index(c) for c in _SP_COLUMNS]
            rows = [[row[i] for i in indices] for row in values[1:]]
            yield _sse(f"{len(rows):,} rows — columns: {', '.join(_SP_COLUMNS)}", "done")
        except Exception as e:
            logger.error(f"SharePoint stream failed: {e}")
            yield _sse(str(e), "error")

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.get("/tasks", response_class=HTMLResponse, name="read_tasks")
def read_tasks(request: Request, page: Optional[int] = 1, page_size: Optional[int] = 25, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)

    at_least_one_pending = False
    offset = (page - 1) * page_size
    with get_database_connection() as conn:
        # Get the list of suppliers
        with conn.cursor() as cursor:
            cursor.execute("SELECT supplier_id, supplier_name FROM suppliers ORDER BY supplier_name")
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

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "tasks": tasks,
        "at_least_one_pending": at_least_one_pending,
        'suppliers': suppliers,
        "total_pages": total_pages,
        "current_page": page,
        'BASE_URL': BASE_URL
    })


@router.post("/upload-task")
async def upload_task(request: Request, fileBase64: str = Form(...), supplier_id: str = Form(...), filename: str = Form(...), user: str = Depends(get_current_user)):
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

        # Create a task in the db
        with get_database_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("INSERT INTO tasks (created, status, supplier_id, filename) VALUES (%s, %s, %s, %s) RETURNING task_id",
                               (datetime.datetime.now(), 'created', supplier_id, filename))
                task_id = cursor.fetchone()[0]
                conn.commit()

        env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
        s3_location = f'applications/pricing/uk-vehicle-list-evaluator/{env}/raw/{task_id}.{file_ext}'

        # upload the file to S3
        upload_file(temp_input_file_path, s3_bucketname, s3_location)

        # Execute the task elaboration
        aws_context = request.scope["aws.context"] if "aws.context" in request.scope else None
        if aws_context is None:
            import asyncio
            asyncio.create_task(elaborate_task({'task_id': task_id}))
        else:
            LambdaRequestsManager().simulate_http(
                aws_context=aws_context,
                path=BASE_URL + "/elaborate-task",
                method="POST",
                data={'task_id': task_id},
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
            cursor.execute("SELECT * FROM tasks WHERE task_id = %s", (task_id,))
            cols = [desc[0] for desc in cursor.description]
            task = dict(zip(cols, cursor.fetchone()))

    if not task:
        return {"message": "Task not found"}

    file_ext = task['filename'].split('.')[-1]
    fd, tmp_path = tempfile.mkstemp(suffix='.' + file_ext)
    os.close(fd)
    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    s3_location = f'applications/pricing/uk-vehicle-list-evaluator/{env}/elaborated/{task["task_id"]}.{file_ext}'
    download_file(tmp_path, s3_bucketname, s3_location)
    output_filename = task['filename'].split('.')[0] + '_output.' + file_ext
    return FileResponse(tmp_path, filename=output_filename, media_type='application/octet-stream')

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
                cursor.execute("UPDATE tasks SET status = 'pending' WHERE task_id = %s", (data['task_id'],))
                conn.commit()

        env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
        file_ext = task['filename'].split('.')[-1]
        s3_location = f'applications/pricing/uk-vehicle-list-evaluator/{env}/raw/{data["task_id"]}.{file_ext}'
        total_rows = None
        execution_time = None

        fd_in,  tmp_in  = tempfile.mkstemp(suffix='.' + file_ext)
        fd_out, tmp_out = tempfile.mkstemp(suffix='.' + file_ext)
        os.close(fd_in)
        os.close(fd_out)

        try:
            t = time.time()

            download_file(tmp_in, s3_bucketname, s3_location)

            input_df = read_dataframe_file(tmp_in)
            total_rows = len(input_df)

            autotrader = AutoTraderClient()
            cap_client = CAPClient()
            columns_mapper = get_columns_mapper(task['supplier_name'])

            def process_df(args):
                processor_obj, input_df, columns_mapper = args
                return processor_obj.process_file(input_df, columns_mapper)

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(process_df, (autotrader, input_df, columns_mapper)),
                           executor.submit(process_df, (cap_client, input_df, columns_mapper))]
                results = [future.result() for future in futures]

            processed_df = pd.concat([input_df, *results], axis=1)
            write_dataframe_file(processed_df, tmp_out)

            s3_location = f'applications/pricing/uk-vehicle-list-evaluator/{env}/elaborated/{data["task_id"]}.{file_ext}'
            upload_file(tmp_out, s3_bucketname, s3_location)

            execution_time = time.time() - t
            status = 'success'
            message = None
        except Exception as e:
            logger.error(f"Error processing file: {e}, traceback: {traceback.format_exc()}")
            status = 'error'
            message = str(e)

        with get_database_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE tasks SET status = %s, message = %s, total_rows = %s, execution_time = %s WHERE task_id = %s", (status, message, total_rows, execution_time, data['task_id'],))
                conn.commit()

    except Exception as e:
        logger.error(f"Unhandled error in elaborate_task: {e}, traceback: {traceback.format_exc()}")

    finally:
        for p in (tmp_in, tmp_out):
            if p is None:
                continue
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass

    return {}