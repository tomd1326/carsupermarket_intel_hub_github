import asyncio
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
from typing import Optional
import requests
import time
import hashlib
import logging
import datetime

from botocore.config import Config

from utils.lambda_http import LambdaRequestsManager
from utils.s3_tools import upload_file, download_file
from utils.db_utils import (get_database_connection, get_settings, save_settings)
from utils.snowflake_client import SnowflakeClient
from utils.sharepoint_client import SharePointClient
from list_processor.autotrader_client import AutoTraderClient
from list_processor.cap_client import CAPClient
from list_processor.columns_mapper import get_columns_mapper, BaseColumnsMapper
from list_processor.motability_client import MotabilityFetchClient
from list_processor.onelink_client import OneLinkFetchClient
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
s3_bucket_region = 'eu-west-1'  # bucket is always in eu-west-1 regardless of Lambda region


VALID_USERS = {
    'admin': '401b8fe6c8d8d7e9ecf3e7cfae3fd3ba700693bfb452becce59f08003a55c640',
    'buyer': 'cd44b3a3d1fb9021db64745df31dce588104bf663952dc84fe6e9cd6140aa3cb',
}

# session_id -> {user, login_time} — tracks active sessions server-side
_active_sessions: dict = {}

def get_current_user(request: Request):
    user = request.session.get('user')
    if not user:
        return None
    # Invalidate session if password has changed since login
    if request.session.get('pwd_hash') != VALID_USERS.get(user):
        _active_sessions.pop(request.session.get('session_id'), None)
        request.session.clear()
        return None
    return user


@router.get("/", response_class=HTMLResponse)
def root(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    #
    return RedirectResponse(url=BASE_URL + "/home", status_code=303)  # CHANGED: was /tasks
    # return templates.TemplateResponse("supplier_stock_evaluator.html", {"request": request, "user": user})


@router.get("/login", response_class=HTMLResponse)
def login(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, 'BASE_URL': BASE_URL})


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    # calculate sha256 hash of the password
    pwd_hash = hashlib.sha256(password.encode()).hexdigest()
    if pwd_hash == VALID_USERS.get(username):
        session_id = hashlib.sha256(os.urandom(16)).hexdigest()
        request.session['user'] = username
        request.session['pwd_hash'] = pwd_hash
        request.session['session_id'] = session_id
        _active_sessions[session_id] = {'user': username, 'login_time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
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
    _active_sessions.pop(request.session.get('session_id'), None)
    request.session.clear()
    return RedirectResponse(url=BASE_URL + "/login", status_code=303)


@router.get("/settings-page", response_class=HTMLResponse)
def settings_page(request: Request, user: str = Depends(get_current_user)):
    if user != 'admin':
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    with get_database_connection() as conn:
        settings = get_settings(conn)
    sessions = sorted(_active_sessions.values(), key=lambda s: s['login_time'], reverse=True)
    return templates.TemplateResponse("settings.html", {"request": request, "user": user, "BASE_URL": BASE_URL, "settings": settings, "active_sessions": sessions})


@router.post("/settings")
def update_settings(
    request: Request,
    number_to_buy: int = Form(...),
    max_days_sales: int = Form(...),
    max_dts: int = Form(...),
    min_gpu_m: int = Form(...),
    cap_workers: int = Form(...),
    cap_rate_limit: int = Form(...),
    calc_buying_reqs: str = Form(default=""),
    enterprise_email_auto_process: str = Form(default=""),
    user: str = Depends(get_current_user),
):
    if user != 'admin':
        raise HTTPException(status_code=403, detail="Forbidden")
    calc = calc_buying_reqs == "on"
    auto_process = enterprise_email_auto_process == "on"
    with get_database_connection() as conn:
        save_settings(conn, number_to_buy, max_days_sales, max_dts, min_gpu_m, cap_workers, cap_rate_limit, calc, auto_process)
    logger.info(
        "Settings updated by %s: number_to_buy=%d, max_days_sales=%d, max_dts=%d, "
        "min_gpu_m=%d, cap_workers=%d, cap_rate_limit=%d, calc_buying_reqs=%s, enterprise_email_auto_process=%s",
        user, number_to_buy, max_days_sales, max_dts, min_gpu_m, cap_workers, cap_rate_limit, calc, auto_process
    )
    return RedirectResponse(url=BASE_URL + "/settings-page?saved=1", status_code=303)


# NEW
@router.get("/home", response_class=HTMLResponse)
def home(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    def _warm_caches():
        try:
            _get_sf_rows()
        except Exception as e:
            logger.warning(f"Home cache warm (SF) failed: {e}")
        try:
            _get_sp_purchases()
        except Exception as e:
            logger.warning(f"Home cache warm (SP) failed: {e}")
    threading.Thread(target=_warm_caches, daemon=True).start()
    return templates.TemplateResponse("home.html", {"request": request, "user": user, "BASE_URL": BASE_URL})


# NEW
@router.get("/buying-requirements", response_class=HTMLResponse)
def buying_requirements(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    with get_database_connection() as conn:
        settings = get_settings(conn)
    return templates.TemplateResponse("buying_requirements.html", {"request": request, "user": user, "BASE_URL": BASE_URL, "settings": settings})


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


def _safe_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


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
_SF_S3_KEY      = f"applications/pricing/uk-vehicle-list-evaluator/{_S3_ENV}/cache/buying_requirements_v15.json"
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
            rows, col_names = sf.execute("""
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
                    VEHICLE_UPDATED_AT,
                    COALESCE(MAKE_MODEL, '') AS MAKE_MODEL,
                    CAP_LIVE_CLEAN_LATEST
                FROM PROD_CARSUPERMARKET_DB.product_vehicle_journey.buying_requirements
                ORDER BY CAP_ID
            """)
        # Build name->index map so missing columns default to None rather than misaligning data
        # [0:cap_id, 1:label, 2:fuel, 3:price_band, 4:stock, 5:sales90, 6:sales7,
        #  7:dis, 8:dts, 9:gpu_m, 10:gpu_s, 11:gpu_ms,
        #  12:purchase_price, 13:sell_price, 14:at_val, 15:at_pct, 16:at_rating,
        #  17:order_acknowledged_date (ISO string), 18:vrm, 19:supplier_name, 20:vehicle_url,
        #  21:vehicle_status, 22:gpu_m_resolved, 23:vehicle_location,
        #  24:date_first_registered, 25:mileage, 26:autotrader_portal_link,
        #  27:invoice_posted_date, 28:vehicle_updated_at, 29:make_model, 30:cap_live_clean]
        col_map = {name.upper(): i for i, name in enumerate(col_names)}
        def _g(r, name): return r[col_map[name]] if name in col_map else None
        def _s(r, name): v = _g(r, name); return str(v) if v is not None else None
        result = [[
            _g(r, 'CAP_ID'), _g(r, 'LABEL'), _g(r, 'FUEL_TYPE'), _g(r, 'PRICE_BAND'),
            _g(r, 'STOCK'), _g(r, 'SALES_90_DAYS'), _g(r, 'SALES_7_DAYS'),
            _g(r, 'DAYS_IN_STOCK_ACKNOWLEDGED'), _g(r, 'DAYS_ADVERTISED_ACKNOWLEDGED'),
            _g(r, 'GPU_M'), _g(r, 'GPU_S'), _g(r, 'GPU_MS'),
            _g(r, 'PURCHASE_PRICE_INC_VAT'), _g(r, 'SELLING_PRICE_INC_VAT'),
            _g(r, 'AUTOTRADER_LATEST_RETAIL_VALUATION'), _g(r, 'AUTOTRADER_PERCENT'),
            _g(r, 'AUTOTRADER_LATEST_RETAIL_RATING'),
            _s(r, 'ORDER_ACKNOWLEDGED_DATE'),
            _g(r, 'VRM'), _g(r, 'SUPPLIER_NAME'), _g(r, 'VEHICLE_URL'),
            _g(r, 'VEHICLE_STATUS'), _g(r, 'GPU_M_RESOLVED'), _g(r, 'VEHICLE_LOCATION'),
            _s(r, 'DATE_FIRST_REGISTERED'),
            _g(r, 'MILEAGE'), _g(r, 'AUTOTRADER_PORTAL_LINK'),
            _s(r, 'INVOICE_POSTED_DATE'), _s(r, 'VEHICLE_UPDATED_AT'),
            _g(r, 'MAKE_MODEL'), _g(r, 'CAP_LIVE_CLEAN_LATEST'),
        ] for r in rows]
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
    purchases: dict = {}

    # Find the header row — search first 5 rows for the one containing "CAP ID"
    header_row_idx = None
    headers = []
    for i, row in enumerate(values[:5]):
        if row and "CAP ID" in [str(c).strip() for c in row]:
            header_row_idx = i
            headers = [str(c).strip() for c in row]
            break

    if headers:
        try:
            cap_idx  = headers.index("CAP ID")
            rrp_idx  = headers.index("RRP")
            rrp0_idx = headers.index("RRP 0%")
        except ValueError:
            pass
        else:
            for row in values[header_row_idx + 1:]:
                try:
                    raw_cap = str(row[cap_idx]).strip().replace(',', '') if row[cap_idx] not in (None, '') else ''
                    cap_id = int(float(raw_cap)) if raw_cap else None
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

    data_rows = len(values) - (header_row_idx + 1) if header_row_idx is not None else 0
    logger.info(f"SP: header on row {header_row_idx}, {len(purchases)} purchase keys from {data_rows} data rows")
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


@router.get("/tasks/{task_id}/buying-requirements-download")
def task_buying_requirements_download(
    task_id: str,
    number_to_buy: int = 200,
    max_days_sales: int = 90,
    max_dts: int = 45,
    min_gpu_m: int = 0,
    user: str = Depends(get_current_user),
):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)

    # ── 1. Fetch task info ────────────────────────────────────────────────────
    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT t.*, s.supplier_name FROM tasks t "
                "LEFT JOIN suppliers s ON s.supplier_id = t.supplier_id "
                "WHERE t.task_id = %s",
                (task_id,),
            )
            cols = [desc[0] for desc in cursor.description]
            row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    task = dict(zip(cols, row))
    if task["status"] != "success":
        raise HTTPException(status_code=400, detail="Task not complete")

    # ── 2. Download elaborated file from S3 ───────────────────────────────────
    env = "prod" if os.getenv("AWS_REGION") == "eu-west-1" else "dev"
    file_ext = task["filename"].split(".")[-1]
    fd, tmp_path = tempfile.mkstemp(suffix="." + file_ext)
    os.close(fd)
    try:
        s3_key = f"applications/pricing/uk-vehicle-list-evaluator/{env}/elaborated/{task_id}.{file_ext}"
        download_file(tmp_path, s3_bucketname, s3_key)
        elaborated_df = read_dataframe_file(tmp_path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    # ── 3. Build requirements lookup from SF data ────────────────────────────
    # SF row indices:
    #   0:cap_id  3:price_band  4:stock  5:sales90  6:sales7  7:dis  8:dts
    #   9:gpu_m  10:gpu_s  11:gpu_ms  13:sell_price  14:at_val  15:at_pct
    #   16:at_rating  22:gpu_m_resolved
    # DIS/DTS/sell/AT averaged only when sales90==1 (sold vehicles), GPU averaged across all passing.
    # Keyed by (cap_id, price_band). A separate cap_id_at_pct dict holds the
    # CAP-rate-only (non-interest_free) avg AT % per cap_id, used to estimate
    # each supplier vehicle's retail price before its price band is known.
    sf_rows = _get_sf_rows()

    def _empty_bucket():
        return {
            "stock": 0.0, "sales_xxd": 0.0, "sales_7": 0.0,
            "dis_sum": 0.0, "dis_n": 0,
            "dts_sum": 0.0, "dts_n": 0,
            "sell_sum": 0.0, "sell_n": 0,
            "at_val_sum": 0.0, "at_val_n": 0,
            "at_rate_sum": 0.0, "at_rate_n": 0,
            "gpu_m_sum": 0.0, "gpu_m_n": 0,
            "gpu_s_sum": 0.0, "gpu_s_n": 0,
            "gpu_ms_sum": 0.0, "gpu_ms_n": 0,
        }

    cap_data: dict = {}
    # cap_id → {at_pct_sum, at_pct_n} for non-interest_free rows only
    cap_id_at_pct: dict = {}

    for r in sf_rows:
        cap_id     = r[0]
        price_band = r[3] or ""
        stock      = float(r[4]  or 0)
        sales90    = float(r[5]  or 0)
        sales7     = float(r[6]  or 0)
        dis        = float(r[7])  if r[7]  is not None else None
        dts        = float(r[8])  if r[8]  is not None else None
        gpu_m      = float(r[22]) if r[22] is not None else None
        gpu_s      = float(r[10]) if r[10] is not None else None
        gpu_ms     = float(r[11]) if r[11] is not None else None
        sell       = float(r[13]) if r[13] is not None else None
        at_val     = float(r[14]) if r[14] is not None else None
        at_pct_raw = float(r[15]) if r[15] is not None else None

        key = (cap_id, price_band)
        if key not in cap_data:
            cap_data[key] = _empty_bucket()
        g = cap_data[key]

        g["stock"] += stock

        # Accumulate CAP-rate-only AT % for price-band estimation step
        if price_band != "interest_free" and at_pct_raw is not None:
            if cap_id not in cap_id_at_pct:
                cap_id_at_pct[cap_id] = {"sum": 0.0, "n": 0}
            cap_id_at_pct[cap_id]["sum"] += at_pct_raw
            cap_id_at_pct[cap_id]["n"]   += 1

        pass_dts  = dts   is None or dts   <= max_dts
        pass_gpum = gpu_m is None or gpu_m >= min_gpu_m
        if not (pass_dts and pass_gpum):
            continue

        g["sales_xxd"] += sales90 * max_days_sales / 90.0
        g["sales_7"]   += sales7

        sold = sales90 == 1
        if dis    is not None and sold:
            g["dis_sum"]     += dis;    g["dis_n"]     += 1
        if dts    is not None and sold:
            g["dts_sum"]     += dts;    g["dts_n"]     += 1
        if sell   is not None and sell != 0 and sold:
            g["sell_sum"]    += sell;   g["sell_n"]    += 1
        if at_val is not None and sold:
            g["at_val_sum"]  += at_val; g["at_val_n"]  += 1
        if at_pct_raw is not None and sold:
            g["at_rate_sum"] += at_pct_raw; g["at_rate_n"] += 1
        if gpu_m  is not None:
            g["gpu_m_sum"]   += gpu_m;  g["gpu_m_n"]   += 1
        if gpu_s  is not None:
            g["gpu_s_sum"]   += gpu_s;  g["gpu_s_n"]   += 1
        if gpu_ms is not None:
            g["gpu_ms_sum"]  += gpu_ms; g["gpu_ms_n"]  += 1

    # Fleet-wide totals (across all price bands)
    total_current_stock = sum(g["stock"]     for g in cap_data.values())
    total_xxd_sales     = sum(g["sales_xxd"] for g in cap_data.values())
    ideal_total         = total_current_stock + number_to_buy

    def _avg(s, n):
        return round(s / n, 1) if n > 0 else None

    def _avg_gbp(s, n):
        return round(s / n) if n > 0 else None

    # Per-(cap_id, price_band) derived columns
    cap_requirements: dict = {}
    for (cap_id, price_band), g in cap_data.items():
        xxd_sales   = g["sales_xxd"]
        stock       = g["stock"]
        ideal_stock = (ideal_total * xxd_sales / total_xxd_sales) if total_xxd_sales > 0 and xxd_sales > 0 else 0.0
        avg_sell    = _avg_gbp(g["sell_sum"],   g["sell_n"])
        avg_at_val  = _avg_gbp(g["at_val_sum"], g["at_val_n"])
        at_pct      = round(((avg_sell + 199) / avg_at_val) * 100, 1) if avg_sell is not None and avg_at_val and avg_at_val != 0 else None
        cap_requirements[(cap_id, price_band)] = {
            f"{max_days_sales}d Sales":  round(xxd_sales,          1),
            "7d Sales":                  round(g["sales_7"],        1),
            "Stock":                     round(stock,               0),
            "Ideal Stock":               round(ideal_stock,         1),
            "Required":                  round(ideal_stock - stock, 1),
            "Avg Days in Stock":         _avg(g["dis_sum"],     g["dis_n"]),
            "Avg Days to Sell":          _avg(g["dts_sum"],     g["dts_n"]),
            "Avg Sell Price":            avg_sell,
            "Avg AT PTM":                avg_at_val,
            "Avg AT PTM %":              at_pct,
            "Avg AT Rating":             _avg(g["at_rate_sum"], g["at_rate_n"]),
            "Avg GPU-M":                 _avg_gbp(g["gpu_m_sum"],  g["gpu_m_n"]),
            "Avg GPU-S":                 _avg_gbp(g["gpu_s_sum"],  g["gpu_s_n"]),
            "Avg GPU-MS":                _avg_gbp(g["gpu_ms_sum"], g["gpu_ms_n"]),
        }

    # ── 4. Join to elaborated file ────────────────────────────────────────────
    # For each vehicle: use CAP-rate-only avg AT % × AT retail valuation to
    # estimate its retail price, derive its price band, then look up
    # (cap_id, derived_price_band) for all requirement columns.
    req_cols = [
        f"{max_days_sales}d Sales", "7d Sales", "Stock", "Ideal Stock", "Required",
        "Avg Days in Stock", "Avg Days to Sell", "Avg Sell Price",
        "Avg AT PTM", "Avg AT PTM %", "Avg AT Rating",
        "Avg GPU-M", "Avg GPU-S", "Avg GPU-MS",
    ]

    AT_VAL_COL = "at_valuations_retail_valuation"
    cap_col    = "cap_capid" if "cap_capid" in elaborated_df.columns else None
    at_col     = AT_VAL_COL  if AT_VAL_COL  in elaborated_df.columns else None

    def _estimated_retail(cap_id_val, at_val_val):
        cid = _safe_int(cap_id_val)
        at_v = _safe_float(at_val_val)
        if cid is None or at_v is None or at_v <= 0:
            return None
        pct_entry = cap_id_at_pct.get(cid)
        if not pct_entry or pct_entry["n"] == 0:
            return None
        return round(at_v * (pct_entry["sum"] / pct_entry["n"]) / 100.0)

    def _derive_price_band(cap_id_val, at_val_val):
        retail = _estimated_retail(cap_id_val, at_val_val)
        return _rrp_to_price_band(retail) if retail is not None else None

    if cap_col and at_col:
        elaborated_df["Estimated Retail Price"] = elaborated_df.apply(
            lambda row: _estimated_retail(row[cap_col], row[at_col]), axis=1
        )
        elaborated_df["Derived Price Band"] = elaborated_df.apply(
            lambda row: _derive_price_band(row[cap_col], row[at_col]), axis=1
        )
    else:
        elaborated_df["Estimated Retail Price"] = None
        elaborated_df["Derived Price Band"]     = None

    def lookup(cap_id_val, price_band_val, col):
        try:
            cid = int(float(cap_id_val))
        except (TypeError, ValueError):
            return None
        return cap_requirements.get((cid, price_band_val or ""), {}).get(col)

    for col in req_cols:
        if cap_col:
            elaborated_df[col] = elaborated_df.apply(
                lambda row, c=col: lookup(row[cap_col], row.get("Derived Price Band"), c), axis=1
            )
        else:
            elaborated_df[col] = None

    # ── 5. Write and return ───────────────────────────────────────────────────
    fd2, tmp_out = tempfile.mkstemp(suffix="." + file_ext)
    os.close(fd2)
    try:
        write_dataframe_file(elaborated_df, tmp_out)
        base_name = task["filename"].rsplit(".", 1)[0]
        out_filename = f"{base_name}_requirements.{file_ext}"
        return FileResponse(tmp_out, filename=out_filename, media_type="application/octet-stream")
    except Exception:
        try:
            os.remove(tmp_out)
        except OSError:
            pass
        raise


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
            cursor.execute(
                "SELECT t.task_id, t.created, t.status, t.filename, t.total_rows, t.execution_time, t.message, s.supplier_name "
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
                t['execution_time'] = round(t['execution_time'], 1) if t['execution_time'] is not None else None
                tasks.append(t)
                if t['status'] in ('pending', 'created'):
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
            cursor.execute("SELECT supplier_id, supplier_name FROM suppliers ORDER BY CASE WHEN supplier_name = 'Generic' THEN 1 ELSE 0 END, supplier_name")
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
                if task['status'] in ('pending', 'created'):
                    at_least_one_pending = True

    total_pages = (total_tasks + page_size - 1) // page_size  # Calculate the total number of pages

    with get_database_connection() as conn:
        settings = get_settings(conn)
    return templates.TemplateResponse("supplier_stock_evaluator.html", {
        "request": request,
        "user": user,
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
            asyncio.create_task(elaborate_task({'task_id': task_id, 'cap_workers': cap_workers, 'cap_rate_limit': cap_rate_limit}))
        else:
            LambdaRequestsManager().simulate_http(
                aws_context=aws_context,
                path=BASE_URL + "/elaborate-task",
                method="POST",
                data={'task_id': task_id, 'cap_workers': cap_workers, 'cap_rate_limit': cap_rate_limit},
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

    file_ext = task['filename'].split('.')[-1]
    fd, tmp_path = tempfile.mkstemp(suffix='.' + file_ext)
    os.close(fd)
    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    s3_location = f'applications/pricing/uk-vehicle-list-evaluator/{env}/elaborated/{task["task_id"]}.{file_ext}'
    download_file(tmp_path, s3_bucketname, s3_location)
    output_filename = prefix + task['filename'].split('.')[0] + '_output.' + file_ext
    return FileResponse(tmp_path, filename=output_filename, media_type='application/octet-stream')

@router.get("/delete-task/{task_id}")
def delete_task(task_id: str, user: str = Depends(get_current_user)):
    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM tasks WHERE task_id = %s AND status IN ('pending', 'created')", (task_id,))
        conn.commit()
    return {"ok": True}

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
    with get_database_connection() as conn:
        settings = get_settings(conn)
    if not settings.get('enterprise_email_auto_process'):
        raise HTTPException(status_code=503, detail="Enterprise email auto-processing is disabled")
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
                "INSERT INTO tasks (created, status, supplier_id, filename) VALUES (%s, %s, %s, %s) RETURNING task_id",
                (datetime.datetime.now(), 'created', supplier_id, filename)
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
        asyncio.create_task(elaborate_task({'task_id': task_id, 'cap_workers': settings['cap_workers'], 'cap_rate_limit': settings['cap_rate_limit']}))
    else:
        LambdaRequestsManager().simulate_http(
            aws_context=aws_context,
            path=BASE_URL + "/elaborate-task",
            method="POST",
            data={'task_id': task_id, 'cap_workers': settings['cap_workers'], 'cap_rate_limit': settings['cap_rate_limit']},
        )
    logger.info(f"Enterprise email: started elaborate for task_id={task_id}")
    return {"status": "started", "task_id": task_id}

_SUPPLIER_FETCH_ROW_LIMIT = 50
_supplier_fetch_lock = asyncio.Lock()


@router.post("/api/fetch-supplier-stock")
async def fetch_supplier_stock(
    request: Request,
    supplier: str = Form(...),
    include_preview: Optional[str] = Form(None),
    user: str = Depends(get_current_user),
):
    if user != 'admin':
        raise HTTPException(status_code=403, detail="Forbidden")

    supplier = supplier.lower()
    if supplier not in ('motability', '1link'):
        raise HTTPException(status_code=400, detail="supplier must be 'motability' or '1link'")

    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    # Supplier credentials only exist in prod secret
    secrets = get_secret('prod/carsupermarket/car_pricer/api_access')

    if _supplier_fetch_lock.locked():
        raise HTTPException(status_code=409, detail="A stock retrieval is already in progress. Please wait for it to finish.")

    now = datetime.datetime.now()
    supplier_label = 'Motability' if supplier == 'motability' else '1link'
    filename = f"{supplier_label}_{now.strftime('%Y%m%d_%H%M%S')}.csv"

    # Insert task immediately so it appears in the list while fetching
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
                "INSERT INTO tasks (created, status, supplier_id, filename) VALUES (%s, %s, %s, %s) RETURNING task_id",
                (now, 'created', supplier_id, filename)
            )
            task_id = cursor.fetchone()[0]
            conn.commit()

    _FETCH_MAX_ATTEMPTS = 3
    _FETCH_RETRY_DELAY = 5  # seconds between attempts

    async with _supplier_fetch_lock:
        last_exc = None
        input_df = None
        for attempt in range(1, _FETCH_MAX_ATTEMPTS + 1):
            try:
                if supplier == 'motability':
                    client = MotabilityFetchClient(
                        username=secrets['MOTABILITY_ID'],
                        password=secrets['MOTABILITY_PASSWORD'],
                    )
                    input_df = client.fetch_dataframe(row_limit=_SUPPLIER_FETCH_ROW_LIMIT)
                    if not include_preview:
                        type_col = input_df.columns[1] if len(input_df.columns) > 1 else None
                        if type_col is not None:
                            input_df = input_df[input_df[type_col] != 'Preview']
                else:
                    client = OneLinkFetchClient(
                        username=secrets['1LINK_ID'],
                        password=secrets['1LINK_PASSWORD'],
                    )
                    input_df = client.fetch_dataframe(row_limit=_SUPPLIER_FETCH_ROW_LIMIT)
                break  # success
            except Exception as e:
                last_exc = e
                logger.warning(f"fetch_supplier_stock attempt {attempt}/{_FETCH_MAX_ATTEMPTS} failed for {supplier}: {e}")
                if attempt < _FETCH_MAX_ATTEMPTS:
                    await asyncio.sleep(_FETCH_RETRY_DELAY)

        if input_df is None:
            with get_database_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "UPDATE tasks SET status = 'error', message = %s WHERE task_id = %s",
                        (str(last_exc)[:500], task_id)
                    )
                conn.commit()
            logger.error(f"fetch_supplier_stock failed for {supplier} after {_FETCH_MAX_ATTEMPTS} attempts: {last_exc}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(last_exc))

    # Write DataFrame to a temp CSV and upload to S3 as the raw file
    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE tasks SET status = 'created' WHERE task_id = %s",
                (task_id,)
            )
        conn.commit()

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

    with get_database_connection() as conn:
        settings = get_settings(conn)
    aws_context = request.scope["aws.context"] if "aws.context" in request.scope else None
    if aws_context is None:
        asyncio.create_task(elaborate_task({'task_id': task_id, 'cap_workers': settings['cap_workers'], 'cap_rate_limit': settings['cap_rate_limit']}))
    else:
        LambdaRequestsManager().simulate_http(
            aws_context=aws_context,
            path=BASE_URL + "/elaborate-task",
            method="POST",
            data={'task_id': task_id, 'cap_workers': settings['cap_workers'], 'cap_rate_limit': settings['cap_rate_limit']},
        )

    logger.info(f"fetch_supplier_stock: created task_id={task_id} for {supplier} ({len(input_df)} rows)")
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

            cap_workers = int(data.get('cap_workers', 20))
            cap_rate_limit = int(data.get('cap_rate_limit', 100))
            logger.info(f"CAP settings: workers={cap_workers}, rate_limit={cap_rate_limit}")

            autotrader = AutoTraderClient()
            cap_client = CAPClient()
            columns_mapper = get_columns_mapper(task['supplier_name'])

            def process_df(args):
                processor_obj, input_df, columns_mapper, *kwargs = args
                return processor_obj.process_file(input_df, columns_mapper, *kwargs)

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(process_df, (autotrader, input_df, columns_mapper)),
                           executor.submit(process_df, (cap_client, input_df, columns_mapper, cap_workers, cap_rate_limit))]
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