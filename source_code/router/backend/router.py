import asyncio
import base64
import re
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
from zoneinfo import ZoneInfo

def _uk_now() -> datetime.datetime:
    return datetime.datetime.now(tz=ZoneInfo('Europe/London')).replace(tzinfo=None)

from botocore.config import Config

from utils.lambda_http import LambdaRequestsManager
from utils.s3_tools import upload_file, download_file
from utils.db_utils import (get_database_connection, get_settings, save_settings,
                            record_login, record_logout, record_event,
                            get_last_login_per_user, get_active_sessions_all_envs,
                            load_all_users, upsert_user, remove_user)
from utils.snowflake_client import SnowflakeClient
from utils.sharepoint_client import SharePointClient
from list_processor.autotrader_client import AutoTraderClient
from list_processor.batch_processor import process_file_with_cache
from list_processor.cap_client import CAPClient
from list_processor.columns_mapper import get_columns_mapper, BaseColumnsMapper
from list_processor.motability_client import MotabilityFetchClient
from list_processor.onelink_client import OneLinkFetchClient
from utils.secrets_manager_client import get_secret
from utils.file_handler import read_dataframe_file, write_dataframe_file
import pandas as pd


BASE_URL = os.getenv("BASE_URL", "/dev")
_ENV = 'local' if os.getenv('LOCAL_ENV') == '1' else ('prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev')
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
    'tdrayson': 'e026581a53dc97e3381566ad0f74e99ae65e0297f9eb41bb76596627885bf8a0',
}

def _load_users_from_aurora():
    try:
        with get_database_connection() as conn:
            users = load_all_users(conn)
        VALID_USERS.update(users)
        logger.info("Loaded %d users from Aurora", len(users))
    except Exception as e:
        logger.warning("Could not load users from Aurora — falling back to hardcoded admin only: %s", e)

_load_users_from_aurora()

# session_id -> {user, login_time} — tracks active sessions server-side
_active_sessions: dict = {}
_sessions_invalidated_at: float = 0.0  # epoch — sessions logged in before this are force-logged out

def _track(request: Request, event_type: str, page: str, metadata: dict | None = None) -> None:
    session_id = request.session.get('session_id')
    username = request.session.get('user')
    if not session_id or not username:
        return
    try:
        with get_database_connection() as conn:
            record_event(conn, session_id, username, _ENV, event_type, page, metadata)
    except Exception as e:
        logger.warning("Failed to record event %s for %s: %s", event_type, username, e)


def get_current_user(request: Request):
    user = request.session.get('user')
    if not user:
        return None
    # Invalidate session if password has changed since login
    if request.session.get('pwd_hash') != VALID_USERS.get(user):
        _active_sessions.pop(request.session.get('session_id'), None)
        request.session.clear()
        return None
    # Invalidate session if admin triggered a force re-login after this session logged in
    if request.session.get('login_time_epoch', 0) < _sessions_invalidated_at:
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
        login_epoch = time.time()
        request.session['user'] = username
        request.session['pwd_hash'] = pwd_hash
        request.session['session_id'] = session_id
        request.session['login_time_epoch'] = login_epoch
        _active_sessions[session_id] = {'user': username, 'login_time': _uk_now().strftime('%Y-%m-%d %H:%M:%S')}
        try:
            with get_database_connection() as conn:
                record_login(conn, session_id, username, _ENV, _uk_now())
        except Exception as e:
            logger.warning("Failed to record login for %s: %s", username, e)
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
    session_id = request.session.get('session_id')
    _active_sessions.pop(session_id, None)
    if session_id:
        try:
            with get_database_connection() as conn:
                record_logout(conn, session_id, _uk_now())
        except Exception as e:
            logger.warning("Failed to record logout for session %s: %s", session_id, e)
    request.session.clear()
    return RedirectResponse(url=BASE_URL + "/login", status_code=303)


@router.get("/settings-page", response_class=HTMLResponse)
def settings_page(request: Request, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    with get_database_connection() as conn:
        settings = get_settings(conn, user='buyer')
    sessions = sorted(_active_sessions.values(), key=lambda s: s['login_time'], reverse=True)
    non_admin_users = sorted(u for u in VALID_USERS if u != 'tdrayson')
    return templates.TemplateResponse("settings.html", {"request": request, "user": user, "BASE_URL": BASE_URL, "settings": settings, "active_sessions": sessions, "non_admin_users": non_admin_users})


@router.get("/settings/users", response_class=HTMLResponse)
def users_page(request: Request, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    sessions = sorted(_active_sessions.values(), key=lambda s: s['login_time'], reverse=True)
    non_admin_users = sorted(u for u in VALID_USERS if u != 'tdrayson')
    last_logins = {}
    db_active_sessions = []
    try:
        with get_database_connection() as conn:
            last_logins = get_last_login_per_user(conn)
            db_active_sessions = get_active_sessions_all_envs(conn)
    except Exception as e:
        logger.warning("Failed to load user activity data: %s", e)
    return templates.TemplateResponse("users.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "active_sessions": sessions, "non_admin_users": non_admin_users,
        "last_logins": last_logins, "db_active_sessions": db_active_sessions,
    })


@router.post("/settings/force-relogin")
def force_relogin(request: Request, user: str = Depends(get_current_user)):
    global _sessions_invalidated_at, _active_sessions
    if user != 'tdrayson':
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    _sessions_invalidated_at = time.time()
    _active_sessions.clear()
    return RedirectResponse(url=BASE_URL + "/settings-page?relogin=1", status_code=303)


def _user_redirect(redirect_to: str, **params):
    base = BASE_URL + ("/settings/users" if redirect_to == "users" else "/settings-page")
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return RedirectResponse(url=f"{base}?{qs}", status_code=303)


@router.post("/settings/delete-user")
def delete_user(
    request: Request,
    target_user: str = Form(...),
    redirect_to: str = Form("settings"),
    user: str = Depends(get_current_user)
):
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    if target_user == 'tdrayson':
        raise HTTPException(status_code=400, detail="Cannot delete admin")
    if target_user not in VALID_USERS:
        return _user_redirect(redirect_to, pwderr="User+not+found")
    del VALID_USERS[target_user]
    with get_database_connection() as conn:
        remove_user(conn, target_user)
    logger.info("User %s deleted by admin", target_user)
    return _user_redirect(redirect_to, userdeleted=target_user)


@router.post("/settings/add-user")
def add_user(
    request: Request,
    new_username: str = Form(...),
    new_password: str = Form(...),
    redirect_to: str = Form("settings"),
    user: str = Depends(get_current_user)
):
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    username = new_username.strip().lower()
    if not username:
        return _user_redirect(redirect_to, pwderr="Username+cannot+be+empty")
    if username in VALID_USERS:
        return _user_redirect(redirect_to, pwderr="User+already+exists")
    if not new_password or len(new_password) < 6:
        return _user_redirect(redirect_to, pwderr="Password+must+be+at+least+6+characters")
    pwd_hash = hashlib.sha256(new_password.encode()).hexdigest()
    VALID_USERS[username] = pwd_hash
    with get_database_connection() as conn:
        upsert_user(conn, username, pwd_hash)
    logger.info("User %s created by admin", username)
    return _user_redirect(redirect_to, useradded=username)


@router.post("/settings/set-password")
def set_password(
    request: Request,
    target_user: str = Form(...),
    new_password: str = Form(...),
    redirect_to: str = Form("settings"),
    user: str = Depends(get_current_user)
):
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    if target_user == 'tdrayson':
        raise HTTPException(status_code=400, detail="Cannot change admin password here")
    if target_user not in VALID_USERS:
        raise HTTPException(status_code=404, detail="User not found")
    if not new_password or len(new_password) < 6:
        return _user_redirect(redirect_to, pwderr="Password+must+be+at+least+6+characters")
    pwd_hash = hashlib.sha256(new_password.encode()).hexdigest()
    VALID_USERS[target_user] = pwd_hash
    with get_database_connection() as conn:
        upsert_user(conn, target_user, pwd_hash)
    logger.info("Password updated for %s by admin", target_user)
    return _user_redirect(redirect_to, pwdok=target_user)


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
    get_stock: str = Form(default=""),
    enable_cars_bought: str = Form(default=""),
    mot_workers: int = Form(default=5),
    at_workers: int = Form(default=40),
    at_rate_limit: int = Form(default=60),
    valuation_cache_hours: int = Form(default=36),
    user: str = Depends(get_current_user),
):
    if user != 'tdrayson':
        raise HTTPException(status_code=403, detail="Forbidden")
    calc = calc_buying_reqs == "on"
    gs = get_stock == "on"
    ecb = enable_cars_bought == "on"
    with get_database_connection() as conn:
        save_settings(conn, number_to_buy, max_days_sales, max_dts, min_gpu_m, cap_workers, cap_rate_limit, calc, gs, enable_cars_bought=ecb, mot_workers=mot_workers, at_workers=at_workers, at_rate_limit=at_rate_limit, valuation_cache_hours=valuation_cache_hours, user='buyer')
    logger.info(
        "Settings updated by %s: number_to_buy=%d, max_days_sales=%d, max_dts=%d, "
        "min_gpu_m=%d, cap_workers=%d, cap_rate_limit=%d, at_workers=%d, at_rate_limit=%d, calc_buying_reqs=%s, get_stock=%s, enable_cars_bought=%s, mot_workers=%d",
        user, number_to_buy, max_days_sales, max_dts, min_gpu_m, cap_workers, cap_rate_limit, at_workers, at_rate_limit, calc, gs, ecb, mot_workers
    )
    return RedirectResponse(url=BASE_URL + "/settings-page?saved=1", status_code=303)


# NEW
@router.get("/home", response_class=HTMLResponse)
def home(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    def _warm_caches():
        def _sf():
            try:
                _get_sf_rows()
            except Exception as e:
                logger.warning(f"Home cache warm (SF) failed: {e}")
        def _dbs():
            try:
                _get_dbs_purchases()
            except Exception as e:
                logger.warning(f"Home cache warm (DBS) failed: {e}")
        def _cb():
            try:
                _get_cb_purchases()
            except Exception as e:
                logger.warning(f"Home cache warm (CB) failed: {e}")
        with ThreadPoolExecutor(max_workers=3) as ex:
            ex.submit(_sf); ex.submit(_dbs); ex.submit(_cb)
    threading.Thread(target=_warm_caches, daemon=True).start()
    _track(request, 'page_view', '/home')
    return templates.TemplateResponse("home.html", {"request": request, "user": user, "BASE_URL": BASE_URL})


# NEW
@router.get("/stock-list", response_class=HTMLResponse)
def stock_list(request: Request, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    _track(request, 'page_view', '/stock-list')
    return templates.TemplateResponse("stock_list.html", {"request": request, "user": user, "BASE_URL": BASE_URL})


@router.get("/buying-requirements", response_class=HTMLResponse)
def buying_requirements(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    with get_database_connection() as conn:
        settings = get_settings(conn, user=user)
    _track(request, 'page_view', '/buying-requirements')
    enable_cars_bought = user == 'tdrayson' or settings.get('enable_cars_bought', False)
    return templates.TemplateResponse("buying_requirements.html", {"request": request, "user": user, "BASE_URL": BASE_URL, "settings": settings, "enable_cars_bought": enable_cars_bought})


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
        with _cache_lock:
            cached_at = _sf_cache.get("cached_at", 0.0)
        return {"status": "ok", "rows": sf_rows, "cached_at": cached_at}
    except Exception as e:
        logger.error(f"Buying requirements SF data failed: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/buying-requirements/dbs-purchases")
def buying_requirements_dbs_purchases(request: Request, user: str = Depends(get_current_user)):
    """Daily Buyers Sheet purchases — always fetches live (called by Refresh Data)."""
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    try:
        purchases = _get_dbs_purchases(force=True)
        out = {f"{k[0]}|{k[1]}": v for k, v in purchases.items()}
        with _cache_lock:
            fetched_at  = _dbs_cache.get("fetched_at")
            vehicles    = _dbs_cache.get("vehicles") or []
            total_vrms  = _dbs_cache.get("total_vrms", len(vehicles))
        return {"status": "ok", "purchases": out, "vehicles": vehicles, "total": total_vrms, "fetched_at": fetched_at}
    except Exception as e:
        logger.error(f"Buying requirements DBS purchases failed: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/buying-requirements/dbs-purchases-cached")
def buying_requirements_dbs_purchases_cached(request: Request, user: str = Depends(get_current_user)):
    """DBS purchases from cache only — never triggers a live SharePoint fetch."""
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    try:
        result = _get_dbs_purchases_cached()
        if result is None:
            return {"status": "ok", "purchases": {}, "vehicles": [], "total": None, "fetched_at": None}
        purchases, vehicles = result
        if purchases and isinstance(next(iter(purchases)), tuple):
            purchases = {f"{k[0]}|{k[1]}": v for k, v in purchases.items()}
        with _cache_lock:
            fetched_at = _dbs_cache.get("fetched_at")
            total_vrms = _dbs_cache.get("total_vrms", len(vehicles))
        if fetched_at is None:
            fetched_at = _load_dbs_fetched_at_from_s3()
        return {"status": "ok", "purchases": purchases, "vehicles": vehicles, "total": total_vrms, "fetched_at": fetched_at}
    except Exception as e:
        logger.error(f"Buying requirements DBS cached failed: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/buying-requirements/cb-purchases")
def buying_requirements_cb_purchases(request: Request, user: str = Depends(get_current_user)):
    """Cars Bought not on Apex — always fetches live (called by Refresh Data)."""
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    try:
        purchases = _get_cb_purchases(force=True)
        out = {f"{k[0]}|{k[1]}": v for k, v in purchases.items()}
        with _cache_lock:
            fetched_at  = _cb_cache.get("fetched_at")
            vehicles    = _cb_cache.get("vehicles") or []
            total_vrms  = _cb_cache.get("total_vrms", sum(purchases.values()))
        return {"status": "ok", "purchases": out, "vehicles": vehicles, "total": total_vrms, "fetched_at": fetched_at}
    except Exception as e:
        logger.error(f"Buying requirements CB purchases failed: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/buying-requirements/cb-purchases-cached")
def buying_requirements_cb_purchases_cached(request: Request, user: str = Depends(get_current_user)):
    """Cars Bought not on Apex from cache only — never triggers a live fetch."""
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    try:
        result = _get_cb_purchases_cached()
        if result is None:
            return {"status": "ok", "purchases": {}, "vehicles": [], "total": None, "fetched_at": None}
        purchases, vehicles = result
        if purchases and isinstance(next(iter(purchases)), tuple):
            purchases = {f"{k[0]}|{k[1]}": v for k, v in purchases.items()}
        with _cache_lock:
            fetched_at = _cb_cache.get("fetched_at")
            total_vrms = _cb_cache.get("total_vrms", sum(purchases.values()) if purchases else 0)
        return {"status": "ok", "purchases": purchases, "vehicles": vehicles, "total": total_vrms, "fetched_at": fetched_at}
    except Exception as e:
        logger.error(f"Buying requirements CB cached failed: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/buying-requirements/data")
def buying_requirements_data(request: Request, user: str = Depends(get_current_user)):
    """Combined endpoint (kept for compatibility). Merges SF + SP in one call."""
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    try:
        sf_rows   = _get_sf_rows()
        purchases = _get_dbs_purchases()
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


_DBS_SUBFOLDER = "Department/Buying/B2B"
_DBS_FILENAME  = "NEW DAILY BUYERS SHEET.xlsx"
_DBS_SHEET     = "Today"
_DBS_COLUMNS   = ["CAP ID", "RRP", "RRP 0%"]

_CB_SUBFOLDER  = "Cars Bought"
_CB_FILENAME   = "CARS BOUGHT 2026.xlsx"
_CB_SHEET      = "TO GO ON"

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


_EXCEL_EPOCH = datetime.date(1899, 12, 30)

def _parse_excel_date(v) -> str | None:
    """Convert an Excel serial number or ISO/string date to dd/mm/yyyy. Returns None if unparseable."""
    if v is None or v == '' or v == 0:
        return None
    s = str(v).strip()
    # Excel serial number (integer or float with no non-numeric chars except '.')
    try:
        serial = int(float(s))
        if 30000 < serial < 60000:  # plausible Excel date range (1982–2064)
            d = _EXCEL_EPOCH + datetime.timedelta(days=serial)
            return d.strftime('%d/%m/%Y')
    except (TypeError, ValueError):
        pass
    # ISO date string yyyy-mm-dd (possibly with time)
    if len(s) >= 10 and s[4] == '-':
        try:
            d = datetime.date.fromisoformat(s[:10])
            return d.strftime('%d/%m/%Y')
        except ValueError:
            pass
    # Already dd/mm/yyyy
    if len(s) == 10 and s[2] == '/' and s[5] == '/':
        return s
    return s[:10] if len(s) >= 10 else s


def _safe_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _safe_float(v):
    try:
        f = float(v)
        return None if f != f else f  # NaN check
    except (TypeError, ValueError):
        return None


# ── Caches ───────────────────────────────────────────────────────────────────
# Snowflake: dbt model runs at 03:00, 07:00, 10:00, 13:00, 16:00 UK time.
#   Data is ready by +15 min. Cache is valid if it was populated after the most
#   recent scheduled window. Persisted to S3 so Lambda cold starts are instant.
# SharePoint: live buyer activity — 30-min TTL, in-process + S3.
_sf_cache: dict = {"rows": None, "cached_at": 0.0}   # cached_at = unix epoch (wall clock)
_dbs_cache: dict = {"purchases": None, "vehicles": None, "expires": 0.0, "fetched_at": None}
_cb_cache: dict  = {"purchases": None, "vehicles": None, "expires": 0.0, "fetched_at": None}
_cache_lock = threading.Lock()

# dbt run hours (UK local time) — data available 15 min after each
_SF_SCHEDULE_HOURS_UK = [3, 7, 10, 13, 16]
_SF_READY_OFFSET_MIN  = 15

_DBS_TTL_SECONDS    = 30 * 60
_DBS_S3_TTL_SECONDS = 30 * 60
_CB_TTL_SECONDS     = 30 * 60
_CB_S3_TTL_SECONDS  = 30 * 60
_S3_BUCKET         = "aa-raw-sources-uk"
_S3_ENV            = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
_SF_S3_KEY         = f"applications/pricing/uk-vehicle-list-evaluator/{_S3_ENV}/cache/buying_requirements_v20.json"
_DBS_S3_KEY        = f"applications/pricing/uk-vehicle-list-evaluator/{_S3_ENV}/cache/dbs_purchases_v17.json"
_CB_S3_KEY         = f"applications/pricing/uk-vehicle-list-evaluator/{_S3_ENV}/cache/cb_purchases_v13.json"
_VRM_CAP_S3_KEY    = f"applications/pricing/uk-vehicle-list-evaluator/shared/vrm_cap_id_cache.json"


def _sf_last_valid_window_utc() -> float:
    """Return the UTC unix timestamp of the most recent SF schedule window that has passed."""
    import zoneinfo
    uk_tz = zoneinfo.ZoneInfo("Europe/London")
    now_uk = datetime.datetime.now(uk_tz)
    # Walk schedule hours in descending order to find the most recent one
    for h in sorted(_SF_SCHEDULE_HOURS_UK, reverse=True):
        candidate = now_uk.replace(hour=h, minute=_SF_READY_OFFSET_MIN, second=0, microsecond=0)
        if candidate <= now_uk:
            return candidate.astimezone(datetime.timezone.utc).timestamp()
    # All today's windows are in the future — use yesterday's last window
    yesterday_uk = now_uk - datetime.timedelta(days=1)
    last_h = max(_SF_SCHEDULE_HOURS_UK)
    candidate = yesterday_uk.replace(hour=last_h, minute=_SF_READY_OFFSET_MIN, second=0, microsecond=0)
    return candidate.astimezone(datetime.timezone.utc).timestamp()


def _sf_cache_is_fresh(cached_at: float) -> bool:
    """True if cached_at is after the most recent valid schedule window."""
    return cached_at >= _sf_last_valid_window_utc()


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


def _load_sf_cache_from_s3() -> tuple[list, float] | None:
    """Returns (rows, cached_at) if the S3 payload exists and is schedule-fresh, else None."""
    try:
        import boto3
        obj = boto3.client("s3").get_object(Bucket=_S3_BUCKET, Key=_SF_S3_KEY)
        payload = json.loads(obj["Body"].read())
        cached_at = payload.get("cached_at", 0.0)
        if _sf_cache_is_fresh(cached_at):
            rows = payload["rows"]
            for r in rows:
                if r[3] == '0%':
                    r[3] = 'interest_free'
            return rows, cached_at
    except Exception:
        pass
    return None


def _json_default(obj):
    from decimal import Decimal
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serialisable")


def _save_sf_cache_to_s3(rows: list, cached_at: float) -> None:
    try:
        import boto3
        boto3.client("s3").put_object(
            Bucket=_S3_BUCKET, Key=_SF_S3_KEY,
            Body=json.dumps({"rows": rows, "cached_at": cached_at}, default=_json_default).encode(),
            ContentType="application/json",
        )
    except Exception as e:
        logger.warning(f"Failed to write SF S3 cache: {e}")


def _load_dbs_cache_from_s3() -> tuple[dict, list] | None:
    payload = _s3_load(_DBS_S3_KEY)
    if not payload:
        return None
    purchases = payload.get("purchases", {})
    vehicles  = payload.get("vehicles", [])
    with _cache_lock:
        _dbs_cache["total_vrms"] = payload.get("total_vrms", len(vehicles))
    return purchases, vehicles


def _save_dbs_cache_to_s3(purchases: dict, vehicles: list, ttl: int = _DBS_TTL_SECONDS, total_vrms: int = None) -> None:
    _s3_save(_DBS_S3_KEY, {"purchases": {f"{k[0]}|{k[1]}": v for k, v in purchases.items()}, "vehicles": vehicles, "total_vrms": total_vrms, "fetched_at": time.time()}, ttl)


def _load_dbs_fetched_at_from_s3() -> float | None:
    try:
        import boto3
        obj = boto3.client("s3").get_object(Bucket=_S3_BUCKET, Key=_DBS_S3_KEY)
        payload = json.loads(obj["Body"].read())
        if time.time() < payload.get("expires_at", 0):
            return payload.get("fetched_at")
    except Exception:
        pass
    return None


def _get_sf_rows(force: bool = False) -> list:
    """Return Snowflake rows — from in-process cache, S3 cache, or live query."""
    with _cache_lock:
        if not force and _sf_cache["rows"] is not None and _sf_cache_is_fresh(_sf_cache["cached_at"]):
            return _sf_cache["rows"]

    # Try S3 cache before hitting Snowflake (skip on force)
    s3_result = None if force else _load_sf_cache_from_s3()
    if s3_result is not None:
        rows, cached_at = s3_result
        with _cache_lock:
            _sf_cache["rows"] = rows
            _sf_cache["cached_at"] = cached_at
        return rows

    cached_at = time.time()
    # S3 miss or stale — query Snowflake and repopulate both caches
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
                CAP_LIVE_CLEAN_LATEST,
                VEHICLE_COSTS,
                GPU_M_COSTS,
                COALESCE(MILEAGE_BAND, '') AS MILEAGE_BAND
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
    def _gbp(r, name): v = _g(r, name); return round(v, 2) if v is not None else None
    result = [[
        _g(r, 'CAP_ID'), _g(r, 'LABEL'), _g(r, 'FUEL_TYPE'), ('interest_free' if _g(r, 'PRICE_BAND') == '0%' else _g(r, 'PRICE_BAND')),
        _g(r, 'STOCK'), _g(r, 'SALES_90_DAYS'), _g(r, 'SALES_7_DAYS'),
        _g(r, 'DAYS_IN_STOCK_ACKNOWLEDGED'), _g(r, 'DAYS_ADVERTISED_ACKNOWLEDGED'),
        _gbp(r, 'GPU_M'), _gbp(r, 'GPU_S'), _gbp(r, 'GPU_MS'),
        _gbp(r, 'PURCHASE_PRICE_INC_VAT'), _gbp(r, 'SELLING_PRICE_INC_VAT'),
        _gbp(r, 'AUTOTRADER_LATEST_RETAIL_VALUATION'), _g(r, 'AUTOTRADER_PERCENT'),
        _g(r, 'AUTOTRADER_LATEST_RETAIL_RATING'),
        _s(r, 'ORDER_ACKNOWLEDGED_DATE'),
        _g(r, 'VRM'), _g(r, 'SUPPLIER_NAME'), _g(r, 'VEHICLE_URL'),
        _g(r, 'VEHICLE_STATUS'), _gbp(r, 'GPU_M_RESOLVED'), _g(r, 'VEHICLE_LOCATION'),
        _s(r, 'DATE_FIRST_REGISTERED'),
        _g(r, 'MILEAGE'), _g(r, 'AUTOTRADER_PORTAL_LINK'),
        _s(r, 'INVOICE_POSTED_DATE'), _s(r, 'VEHICLE_UPDATED_AT'),
        _g(r, 'MAKE_MODEL'), _g(r, 'CAP_LIVE_CLEAN_LATEST'),
        _gbp(r, 'VEHICLE_COSTS'), _gbp(r, 'GPU_M_COSTS'), _g(r, 'MILEAGE_BAND'),
    ] for r in rows]
    _save_sf_cache_to_s3(result, cached_at)

    with _cache_lock:
        _sf_cache["rows"] = result
        _sf_cache["cached_at"] = cached_at
    return result


# ── Shared VRM → CAP ID cache ─────────────────────────────────────────────────
# Keyed by VRM, no expiry — VRM→CAP ID is permanent. Shared across DBS and CB.

_vrm_cap_cache: dict = {}   # in-process: {vrm: cap_id or None}
_vrm_cap_lock = threading.Lock()


def _load_vrm_cap_cache_from_s3() -> dict:
    try:
        import boto3
        obj = boto3.client("s3").get_object(Bucket=_S3_BUCKET, Key=_VRM_CAP_S3_KEY)
        return json.loads(obj["Body"].read())
    except Exception:
        return {}


def _save_vrm_cap_cache_to_s3(cache: dict) -> None:
    try:
        import boto3
        boto3.client("s3").put_object(
            Bucket=_S3_BUCKET, Key=_VRM_CAP_S3_KEY,
            Body=json.dumps(cache).encode(), ContentType="application/json"
        )
    except Exception as e:
        logger.warning(f"VRM CAP cache S3 save failed: {e}")


def _ensure_vrm_cap_cache_loaded() -> None:
    """Load S3 cache into in-process dict if not already done."""
    with _vrm_cap_lock:
        if not _vrm_cap_cache:
            loaded = _load_vrm_cap_cache_from_s3()
            _vrm_cap_cache.update(loaded)
            if loaded:
                logger.info(f"VRM CAP cache: loaded {len(loaded)} entries from S3")


def _enrich_cap_ids(rows: list[dict], workers: int = 25) -> list[dict]:
    """
    For each row dict with keys {vrm, cap_id, ...}, fill in missing cap_ids.
    Checks in-process cache first, then CAP API for any remaining misses.
    Writes new lookups back to S3.
    rows must have 'vrm' and 'cap_id' keys.
    """
    _ensure_vrm_cap_cache_loaded()

    need_api = []
    for r in rows:
        if r['cap_id'] is not None:
            continue
        vrm = r['vrm']
        with _vrm_cap_lock:
            if vrm in _vrm_cap_cache:
                r['cap_id'] = _vrm_cap_cache[vrm]
            else:
                need_api.append(r)

    logger.info(f"VRM CAP cache: {len(need_api)} VRMs need CAP API lookup")
    if not need_api:
        return rows
    cap = CAPClient()

    def _lookup_one(r):
        try:
            from list_processor.structs import VehicleRow
            resp = cap.vrm_valuation(VehicleRow(vrm=r['vrm'], mileage=1000))
            raw = resp.get('capid')
            cap_id = int(raw) if raw and raw != 'Not Found' else None
        except Exception as e:
            logger.warning(f"CAP lookup failed for {r['vrm']}: {e}")
            cap_id = None
        return r['vrm'], cap_id

    new_entries = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for vrm, cap_id in ex.map(_lookup_one, need_api):
            new_entries[vrm] = cap_id

    with _vrm_cap_lock:
        _vrm_cap_cache.update(new_entries)

    for r in need_api:
        r['cap_id'] = new_entries.get(r['vrm'])

    _save_vrm_cap_cache_to_s3(dict(_vrm_cap_cache))
    logger.info(f"VRM CAP cache: {sum(1 for v in new_entries.values() if v)} resolved, {sum(1 for v in new_entries.values() if not v)} not found. Cache now {len(_vrm_cap_cache)} entries.")
    return rows


def _get_sf_cap_price_bands() -> dict:
    """Return {cap_id: price_band} from SF cache — the band with the most stock, then most sales."""
    with _cache_lock:
        rows = _sf_cache.get("rows") or []
    # rows: [0:cap_id, 3:price_band, 4:stock, 5:sales90, ...]
    best: dict = {}  # cap_id -> (price_band, stock, sales90)
    for r in rows:
        cap_id     = r[0]
        price_band = r[3]
        stock      = r[4] or 0
        sales90    = r[5] or 0
        if cap_id is None or not price_band:
            continue
        prev = best.get(cap_id)
        if prev is None or stock > prev[1] or (stock == prev[1] and sales90 > prev[2]):
            best[cap_id] = (price_band, stock, sales90)
    return {cap_id: v[0] for cap_id, v in best.items()}


def _get_dbs_purchases(force: bool = False) -> dict:
    """Return cached Daily Buyers Sheet purchase counts per (cap_id, price_band), fetching if stale."""
    now = time.monotonic()
    with _cache_lock:
        if not force and _dbs_cache["purchases"] is not None and now < _dbs_cache["expires"]:
            return _dbs_cache["purchases"]

    for _attempt in range(1, 4):
        try:
            values = SharePointClient().get_worksheet_values(_DBS_SUBFOLDER, _DBS_FILENAME, _DBS_SHEET)
            break
        except Exception as _exc:
            logger.warning(f"Daily Buyers Sheet fetch attempt {_attempt}/3 failed: {_exc}")
            if _attempt == 3:
                raise
            time.sleep(3)
    purchases: dict = {}

    # Find the header row — search first 5 rows for a row containing a REG/VRM and CAP ID variant
    def _norm_hdr(v): return re.sub(r'[\s_]+', '', str(v).strip().upper())

    def _col(hdrs, *aliases):
        for a in aliases:
            a = re.sub(r'[\s_]+', '', a.upper())
            for i, h in enumerate(hdrs):
                if h == a:
                    return i
        return None

    header_row_idx = None
    headers_norm = []
    for i in ([1] + [j for j in range(min(5, len(values))) if j != 1]):
        row = values[i] if i < len(values) else []
        norm = [_norm_hdr(c) for c in row]
        has_reg = any(h in ('REG', 'VRM', 'REGISTRATION') for h in norm)
        has_cap = any(h in ('CAPID',) for h in norm)
        if row and has_reg and has_cap:
            header_row_idx = i
            headers_norm = norm
            break

    if header_row_idx is None:
        logger.warning(f"Daily Buyers Sheet: no header row found in first {min(5, len(values))} rows — sheet may have changed structure")

    raw_rows = []
    if headers_norm:
        reg_idx  = _col(headers_norm, 'REG', 'VRM', 'REGISTRATION')
        cap_idx  = _col(headers_norm, 'CAP ID', 'CAPID')
        rrp_idx  = _col(headers_norm, 'RRP')
        rrp0_idx = _col(headers_norm, 'RRP 0%')

        if None in (reg_idx, cap_idx, rrp_idx, rrp0_idx):
            logger.warning(f"Daily Buyers Sheet: missing required column(s) — REG={reg_idx} CAP ID={cap_idx} RRP={rrp_idx} RRP 0%={rrp0_idx}. Headers: {headers_norm}")
        else:
            make_idx     = _col(headers_norm, 'MAKE')
            model_idx    = _col(headers_norm, 'MODEL')
            deriv_idx    = _col(headers_norm, 'DERIVATIVE', 'VARIANT')
            supplier_idx = _col(headers_norm, 'SUPPLIER')
            mileage_idx  = _col(headers_norm, 'MILEAGE')
            siv_idx      = _col(headers_norm, 'SIV', 'PURCHASE PRICE', 'PURCHASE_PRICE')
            dor_idx      = _col(headers_norm, 'DOR')
            cap_live_idx = _col(headers_norm, 'CAP CLEAN LIVE', 'CAP LIVE CLEAN', 'CAP CLEAN')
            at_ptm_idx   = _col(headers_norm, 'AUTOTRADER RRP', 'AT RRP', 'AT PTM')
            at_rating_idx = _col(headers_norm, 'RR')
            for row in values[header_row_idx + 1:]:
                vrm = str(row[reg_idx]).strip().replace(' ', '').upper() if reg_idx < len(row) and row[reg_idx] not in (None, '') else ''
                if not vrm or vrm in ('NONE', 'N/A'):
                    continue
                try:
                    raw_cap = str(row[cap_idx]).strip().replace(',', '') if cap_idx < len(row) and row[cap_idx] not in (None, '') else ''
                    cap_id = int(float(raw_cap)) if raw_cap else None
                except (TypeError, ValueError):
                    cap_id = None
                rrp      = row[rrp_idx]  if rrp_idx  < len(row) else None
                rrp0     = row[rrp0_idx] if rrp0_idx < len(row) else None
                make     = str(row[make_idx]).strip()     if make_idx     is not None and make_idx     < len(row) and row[make_idx]     not in (None, '') else ''
                model    = str(row[model_idx]).strip()    if model_idx    is not None and model_idx    < len(row) and row[model_idx]    not in (None, '') else ''
                deriv    = str(row[deriv_idx]).strip()    if deriv_idx    is not None and deriv_idx    < len(row) and row[deriv_idx]    not in (None, '') else ''
                supplier = str(row[supplier_idx]).strip() if supplier_idx is not None and supplier_idx < len(row) and row[supplier_idx] not in (None, '') else ''
                def _parse_num(idx):
                    if idx is None or idx >= len(row) or row[idx] in (None, ''):
                        return None
                    try:
                        return float(str(row[idx]).replace('£', '').replace(',', '').strip())
                    except (TypeError, ValueError):
                        return None
                mileage  = _parse_num(mileage_idx)
                siv      = _parse_num(siv_idx)
                dor_raw  = row[dor_idx] if dor_idx is not None and dor_idx < len(row) and row[dor_idx] not in (None, '') else None
                dor      = _parse_excel_date(dor_raw)
                cap_live  = _parse_num(cap_live_idx)
                at_ptm    = _parse_num(at_ptm_idx)
                at_rating = _parse_num(at_rating_idx)
                raw_rows.append({'vrm': vrm, 'cap_id': cap_id, 'rrp': rrp, 'rrp0': rrp0, 'make': make, 'model': model, 'deriv': deriv, 'supplier': supplier, 'mileage': mileage, 'siv': siv, 'dor': dor, 'cap_live': cap_live, 'at_ptm': at_ptm, 'at_rating': at_rating})

    raw_rows = _enrich_cap_ids(raw_rows)

    total_dbs_vrms = len(raw_rows)

    vehicles = []
    sf_bands = _get_sf_cap_price_bands()
    with _cache_lock:
        _sf_rows_dbs = _sf_cache.get("rows") or []
    sf_cap_band_pairs_dbs = {(r[0], r[3]) for r in _sf_rows_dbs if r[0] is not None and r[3]}

    def _to_float(v):
        if v is None or str(v).strip() in ('', 'None'): return None
        try: return float(str(v).replace('£','').replace(',','').strip())
        except (TypeError, ValueError): return None

    skipped_no_cap = skipped_no_rrp = skipped_no_sf = 0
    for r in raw_rows:
        if r['cap_id'] is None:
            skipped_no_cap += 1
            continue
        try:
            _r0 = r['rrp0']
            rrp0_val = float(str(_r0).replace('£', '').replace(',', '').strip()) if _r0 not in (None, '', 0, 0.0) and str(_r0).strip() not in ('0', '0.0', '0.00') else 0
            is_if = rrp0_val > 0
        except (TypeError, ValueError):
            is_if = False
        if not is_if:
            rrp_float = _to_float(r.get('rrp'))
            if not rrp_float:
                skipped_no_rrp += 1
                continue
        band = "interest_free" if is_if else _rrp_to_price_band(r['rrp'])
        if r['cap_id'] not in sf_bands:
            skipped_no_sf += 1
            continue
        if (r['cap_id'], band) not in sf_cap_band_pairs_dbs:
            skipped_no_sf += 1
            continue
        key = (r['cap_id'], band)
        purchases[key] = purchases.get(key, 0) + 1
        vehicles.append({'vrm': r['vrm'], 'cap_id': r['cap_id'], 'band': band, 'make': r.get('make', ''), 'model': r.get('model', ''), 'deriv': r.get('deriv', ''), 'supplier': r.get('supplier', ''), 'mileage': r.get('mileage'), 'siv': r.get('siv'), 'rrp': _to_float(r.get('rrp')), 'rrp0': _to_float(r.get('rrp0')), 'dor': r.get('dor'), 'cap_live': r.get('cap_live'), 'at_ptm': r.get('at_ptm'), 'at_rating': r.get('at_rating'), 'source': 'dbs'})

    data_rows = len(values) - (header_row_idx + 1) if header_row_idx is not None else 0
    logger.info(f"Daily Buyers Sheet: {sum(purchases.values())} purchases in {len(purchases)} cap/band keys (from {data_rows} rows, {total_dbs_vrms} unique VRMs, skipped: {skipped_no_cap} no CAP ID, {skipped_no_rrp} no RRP, {skipped_no_sf} not in SF)")
    fetched_at = time.time()
    with _cache_lock:
        _dbs_cache["purchases"]  = purchases
        _dbs_cache["vehicles"]   = vehicles
        _dbs_cache["total_vrms"] = total_dbs_vrms
        _dbs_cache["expires"]    = time.monotonic() + _DBS_TTL_SECONDS
        _dbs_cache["fetched_at"] = fetched_at
    _save_dbs_cache_to_s3(purchases, vehicles, _DBS_S3_TTL_SECONDS, total_dbs_vrms)
    return purchases


def _get_dbs_purchases_cached() -> tuple[dict, list] | None:
    """Return (purchases, vehicles) from in-process or S3 cache only — never fetches live."""
    with _cache_lock:
        if _dbs_cache["purchases"] is not None and time.monotonic() < _dbs_cache["expires"]:
            return _dbs_cache["purchases"], _dbs_cache.get("vehicles") or []
    return _load_dbs_cache_from_s3()


# ── Cars Bought cache helpers ─────────────────────────────────────────────────

def _load_cb_cache_from_s3() -> tuple[dict, list] | None:
    payload = _s3_load(_CB_S3_KEY)
    if not payload:
        return None
    return payload.get("purchases", {}), payload.get("vehicles", [])


def _save_cb_cache_to_s3(purchases: dict, vehicles: list, fetched_at: float, ttl: int = _CB_S3_TTL_SECONDS) -> None:
    _s3_save(_CB_S3_KEY, {"purchases": {f"{k[0]}|{k[1]}": v for k, v in purchases.items()}, "vehicles": vehicles, "fetched_at": fetched_at}, ttl)


class _CarsBoughtSharePointClient(SharePointClient):
    TEAM_SITE = "/sites/CarSupermarketOperationalData"


def _get_cb_purchases(force: bool = False) -> dict:
    """Return purchases dict keyed by (cap_id, price_band) for Cars Bought not on Apex."""
    now = time.monotonic()
    with _cache_lock:
        if not force and _cb_cache["purchases"] is not None and now < _cb_cache["expires"]:
            return _cb_cache["purchases"]

    def _fetch_cb_values():
        for _attempt in range(1, 4):
            try:
                return _CarsBoughtSharePointClient().get_worksheet_values(_CB_SUBFOLDER, _CB_FILENAME, _CB_SHEET)
            except Exception as _exc:
                logger.warning(f"Cars Bought fetch attempt {_attempt}/3 failed: {_exc}")
                if _attempt == 3:
                    raise
                time.sleep(3)

    def _fetch_apex_vrms():
        try:
            with SnowflakeClient() as sf:
                rows, _ = sf.execute("""
                    SELECT DISTINCT VRM
                    FROM PROD_CARSUPERMARKET_DB.product_vehicle_journey.buying_requirements
                    WHERE VRM IS NOT NULL AND VRM != ''
                """)
            result = {str(r[0]).strip().upper() for r in rows if r[0]}
            logger.info(f"Cars Bought: fetched {len(result)} Apex VRMs from Snowflake for dedup")
            return result
        except Exception as e:
            logger.warning(f"CB fetch: could not fetch Apex VRMs from Snowflake: {e}")
            # Fallback: extract non-empty VRMs from the in-process SF cache
            with _cache_lock:
                sf_rows = _sf_cache.get("rows") or []
            result = {str(r[18]).strip().upper() for r in sf_rows if len(r) > 18 and r[18]}
            logger.info(f"Cars Bought: using SF cache fallback — {len(result)} Apex VRMs for dedup")
            return result

    def _fetch_dbs_vrms():
        try:
            values = SharePointClient().get_worksheet_values(_DBS_SUBFOLDER, _DBS_FILENAME, _DBS_SHEET)
            for row in values[:5]:
                strs = [str(c).strip() for c in row]
                if "REG" in strs:
                    reg_idx = strs.index("REG")
                    return {
                        str(r[reg_idx]).strip().replace(' ', '').upper()
                        for r in values[values.index(row) + 1:]
                        if reg_idx < len(r) and r[reg_idx] not in (None, '')
                        and str(r[reg_idx]).strip().upper() not in ('NONE', 'N/A', '')
                    }
        except Exception as e:
            logger.warning(f"CB fetch: could not load DBS VRMs for dedup: {e}")
        return set()

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_cb   = ex.submit(_fetch_cb_values)
        f_apex = ex.submit(_fetch_apex_vrms)
        f_dbs  = ex.submit(_fetch_dbs_vrms)
        cb_values = f_cb.result()
        apex_vrms = f_apex.result()
        dbs_vrms  = f_dbs.result()

    header_row_idx = None
    headers = []
    # Check row 2 (index 1) first — sheet may have a title row above headers
    for i in ([1] + [j for j in range(min(5, len(cb_values))) if j != 1]):
        if i >= len(cb_values):
            continue
        strs = [str(c).strip() for c in cb_values[i]]
        if "REG" in strs and "CAP ID" in strs:
            header_row_idx = i
            headers = strs
            break

    total_cb_rows = len(cb_values) - (header_row_idx + 1) if header_row_idx is not None else 0
    logger.info(f"Cars Bought: loaded {total_cb_rows} data rows (header on row {header_row_idx})")

    raw_rows = []
    purchases: dict = {}
    if headers:
        try:
            reg_idx  = headers.index("REG")
            cap_idx  = headers.index("CAP ID")
            rrp_idx  = headers.index("RRP")
            rrp0_idx = headers.index("0%RRP")
        except ValueError:
            pass
        else:
            make_idx     = headers.index("MAKE")           if "MAKE"           in headers else None
            model_idx    = headers.index("MODEL")          if "MODEL"          in headers else None
            deriv_idx    = headers.index("VARIANT")        if "VARIANT"        in headers else None
            supplier_idx = headers.index("SUPPLIER")       if "SUPPLIER"       in headers else None
            mileage_idx  = headers.index("MILEAGE")        if "MILEAGE"        in headers else None
            siv_idx      = headers.index("SIV")            if "SIV"            in headers else (
                           headers.index("PURCHASE PRICE") if "PURCHASE PRICE" in headers else (
                           headers.index("PURCHASE_PRICE") if "PURCHASE_PRICE" in headers else None))
            dor_idx      = headers.index("DOR")              if "DOR"              in headers else None
            cap_live_idx = headers.index("CAP CLEAN LIVE")  if "CAP CLEAN LIVE"  in headers else (
                           headers.index("CAP LIVE CLEAN")  if "CAP LIVE CLEAN"  in headers else (
                           headers.index("CAP CLEAN")       if "CAP CLEAN"       in headers else None))
            at_ptm_idx    = headers.index("AUTOTRADER RRP")  if "AUTOTRADER RRP"  in headers else (
                            headers.index("AT RRP")          if "AT RRP"          in headers else (
                            headers.index("AT PTM")          if "AT PTM"          in headers else None))
            at_rating_idx = headers.index("AT RATING")       if "AT RATING"       in headers else None
            seen_vrms = set()
            for row in cb_values[header_row_idx + 1:]:
                vrm = str(row[reg_idx]).strip().replace(' ', '').upper() if reg_idx < len(row) and row[reg_idx] not in (None, '') else ''
                if not vrm or vrm in ('NONE', 'N/A'):
                    continue
                if vrm in apex_vrms or vrm in dbs_vrms or vrm in seen_vrms:
                    continue
                seen_vrms.add(vrm)
                raw_cap = str(row[cap_idx]).strip().replace(',', '') if cap_idx < len(row) and row[cap_idx] not in (None, '') else ''
                try:
                    cap_id = int(float(raw_cap)) if raw_cap else None
                except (TypeError, ValueError):
                    cap_id = None
                rrp      = row[rrp_idx]  if rrp_idx  < len(row) else None
                rrp0     = row[rrp0_idx] if rrp0_idx < len(row) else None
                make     = str(row[make_idx]).strip()     if make_idx     is not None and make_idx     < len(row) and row[make_idx]     not in (None, '') else ''
                model    = str(row[model_idx]).strip()    if model_idx    is not None and model_idx    < len(row) and row[model_idx]    not in (None, '') else ''
                deriv    = str(row[deriv_idx]).strip()    if deriv_idx    is not None and deriv_idx    < len(row) and row[deriv_idx]    not in (None, '') else ''
                supplier = str(row[supplier_idx]).strip() if supplier_idx is not None and supplier_idx < len(row) and row[supplier_idx] not in (None, '') else ''
                def _parse_num(idx):
                    if idx is None or idx >= len(row) or row[idx] in (None, ''):
                        return None
                    try:
                        return float(str(row[idx]).replace('£', '').replace(',', '').strip())
                    except (TypeError, ValueError):
                        return None
                mileage  = _parse_num(mileage_idx)
                siv      = _parse_num(siv_idx)
                dor_raw  = row[dor_idx] if dor_idx is not None and dor_idx < len(row) and row[dor_idx] not in (None, '') else None
                dor      = _parse_excel_date(dor_raw)
                cap_live  = _parse_num(cap_live_idx)
                at_ptm    = _parse_num(at_ptm_idx)
                at_rating = _parse_num(at_rating_idx)
                raw_rows.append({'vrm': vrm, 'cap_id': cap_id, 'rrp': rrp, 'rrp0': rrp0, 'make': make, 'model': model, 'deriv': deriv, 'supplier': supplier, 'mileage': mileage, 'siv': siv, 'dor': dor, 'cap_live': cap_live, 'at_ptm': at_ptm, 'at_rating': at_rating})

    raw_rows = _enrich_cap_ids(raw_rows)

    sf_bands = _get_sf_cap_price_bands()
    # Build exact (cap_id, band) pairs that exist as rows in the SF table
    with _cache_lock:
        _sf_rows = _sf_cache.get("rows") or []
    sf_cap_band_pairs = {(r[0], r[3]) for r in _sf_rows if r[0] is not None and r[3]}

    def _to_float(v):
        if v is None or str(v).strip() in ('', 'None'): return None
        try: return float(str(v).replace('£','').replace(',','').strip())
        except (TypeError, ValueError): return None

    total_vrms = len(raw_rows)  # all unique VRMs not on Apex/DBS — used for badge
    vehicles: list = []
    skipped_no_cap = skipped_no_rrp = skipped_no_sf = 0
    for r in raw_rows:
        vrm = r['vrm']
        make_model = f"{r.get('make', '')} {r.get('model', '')}".strip()
        if r['cap_id'] is None:
            skipped_no_cap += 1
            continue
        try:
            _r0 = r['rrp0']
            rrp0_val = float(str(_r0).replace('£', '').replace(',', '').strip()) if _r0 not in (None, '', 0, 0.0) and str(_r0).strip() not in ('0', '0.0', '0.00') else 0
            is_if = rrp0_val > 0
        except (TypeError, ValueError):
            is_if = False
        if not is_if:
            rrp_float = _to_float(r.get('rrp'))
            if not rrp_float:
                skipped_no_rrp += 1
                continue
        band = "interest_free" if is_if else _rrp_to_price_band(r['rrp'])
        if r['cap_id'] not in sf_bands:
            skipped_no_sf += 1
            continue
        if (r['cap_id'], band) not in sf_cap_band_pairs:
            skipped_no_sf += 1
            continue
        key = (r['cap_id'], band)
        purchases[key] = purchases.get(key, 0) + 1
        vehicles.append({'vrm': r['vrm'], 'cap_id': r['cap_id'], 'band': band, 'make': r.get('make', ''), 'model': r.get('model', ''), 'deriv': r.get('deriv', ''), 'supplier': r.get('supplier', ''), 'mileage': r.get('mileage'), 'siv': r.get('siv'), 'rrp': _to_float(r.get('rrp')), 'rrp0': _to_float(r.get('rrp0')), 'dor': r.get('dor'), 'cap_live': r.get('cap_live'), 'at_ptm': r.get('at_ptm'), 'at_rating': r.get('at_rating'), 'source': 'cb'})

    fetched_at = time.time()
    with _cache_lock:
        _cb_cache["purchases"]   = purchases
        _cb_cache["vehicles"]    = vehicles
        _cb_cache["total_vrms"]  = total_vrms
        _cb_cache["expires"]     = time.monotonic() + _CB_TTL_SECONDS
        _cb_cache["fetched_at"]  = fetched_at
    _save_cb_cache_to_s3(purchases, vehicles, fetched_at, _CB_S3_TTL_SECONDS)
    logger.info(f"Cars Bought: {total_vrms} unique VRMs, {sum(purchases.values())} in table ({len(purchases)} cap/band keys), skipped: {skipped_no_cap} no CAP ID, {skipped_no_rrp} no RRP, {skipped_no_sf} not in SF")
    return purchases



def _get_cb_purchases_cached() -> tuple[dict, list] | None:
    """Return (purchases, vehicles) from in-process or S3 cache only."""
    with _cache_lock:
        if _cb_cache["purchases"] is not None and time.monotonic() < _cb_cache["expires"]:
            return _cb_cache["purchases"], _cb_cache.get("vehicles") or []
    return _load_cb_cache_from_s3()


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


@router.get("/buying-requirements/stream-dbs")
def stream_dbs(request: Request, user: str = Depends(get_current_user)):
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

            item_id = client._get_item_id(drive_id, _DBS_SUBFOLDER, _DBS_FILENAME)
            yield _sse(f"Got file. Reading '{_DBS_SHEET}' sheet...")

            encoded_sheet = requests.utils.quote(_DBS_SHEET)
            url = (
                f"https://graph.microsoft.com/v1.0/drives/{drive_id}/items/{item_id}"
                f"/workbook/worksheets/{encoded_sheet}/usedRange?$select=values"
            )
            resp = requests.get(url, headers=client._headers)
            resp.raise_for_status()
            values = resp.json().get("values", [])

            headers = values[0] if values else []
            missing = [c for c in _DBS_COLUMNS if c not in headers]
            if missing:
                yield _sse(f"Missing columns: {missing}", "error")
                return

            indices = [headers.index(c) for c in _DBS_COLUMNS]
            rows = [[row[i] for i in indices] for row in values[1:]]
            yield _sse(f"{len(rows):,} rows — columns: {', '.join(_DBS_COLUMNS)}", "done")
        except Exception as e:
            logger.error(f"Daily Buyers Sheet stream failed: {e}")
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
    orig_ext = task["filename"].rsplit(".", 1)[-1].lower()
    _exts_to_try = ["xlsx"] if orig_ext == "xlsx" else ["xlsx", orig_ext]

    # Build candidate S3 keys: stored location first, then both env prefixes as fallback
    # for tasks created before s3_location was recorded.
    _stored = task.get("s3_location")
    _candidate_keys = []
    if _stored:
        _candidate_keys.append((_stored, _stored.rsplit(".", 1)[-1]))
    for _ext in _exts_to_try:
        for _env in ("prod", "dev"):
            _key = f"applications/pricing/uk-vehicle-list-evaluator/{_env}/elaborated/{task_id}.{_ext}"
            if not _stored or _key != _stored:
                _candidate_keys.append((_key, _ext))

    logger.info("download: task=%s candidates=%s", task_id, [k for k, _ in _candidate_keys])
    elaborated_df = None
    _used_ext = "xlsx"
    for _s3_key, _ext in _candidate_keys:
        fd, tmp_path = tempfile.mkstemp(suffix="." + _ext)
        os.close(fd)
        try:
            download_file(tmp_path, s3_bucketname, _s3_key)
            elaborated_df = read_dataframe_file(tmp_path)
            _used_ext = _ext
            logger.info("download: success with s3_key=%s", _s3_key)
            break
        except Exception as e:
            logger.info("download: failed s3_key=%s error=%s", _s3_key, e)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    if elaborated_df is None:
        raise HTTPException(status_code=404, detail="Elaborated file not found on S3")

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
            "gpu_m_costs_sum": 0.0, "gpu_m_costs_n": 0,
            "gpu_m_sum": 0.0, "gpu_m_n": 0,
            "gpu_s_sum": 0.0, "gpu_s_n": 0,
            "gpu_ms_sum": 0.0, "gpu_ms_n": 0,
        }

    cap_data: dict = {}
    # cap_id → {at_pct_sum, at_pct_n} for non-interest_free rows only
    cap_id_at_pct: dict = {}
    # (cap_id, mileage_band) → {sum, n} for vehicle_costs
    cap_id_vehicle_costs: dict = {}
    # mileage_band → {sum, n} across all cap_ids — fallback when no per-cap entry exists
    band_vehicle_costs: dict = {}

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
        at_pct_raw     = float(r[15]) if r[15] is not None else None
        at_rating      = float(r[16]) if r[16] is not None else None
        vehicle_costs  = _safe_float(r[31]) if len(r) > 31 else None
        _gpu_m_costs_raw = _safe_float(r[32]) if len(r) > 32 else None
        gpu_m_costs    = -_gpu_m_costs_raw if _gpu_m_costs_raw is not None else None
        mileage_band   = r[33] if len(r) > 33 else None

        key = cap_id
        if key not in cap_data:
            cap_data[key] = _empty_bucket()
        g = cap_data[key]

        g["stock"] += stock

        # Accumulate CAP-rate-only sell/AT-val ratio for price-band estimation.
        # Use raw sell price / AT val ratio to estimate retail price for supplier vehicles.
        sell_f  = float(r[13]) if r[13] is not None else None
        at_val_f = float(r[14]) if r[14] is not None else None
        if sell_f is not None: sell_f = None if sell_f != sell_f else sell_f
        if at_val_f is not None: at_val_f = None if at_val_f != at_val_f else at_val_f
        if (price_band != "interest_free" and sales90 == 1
                and sell_f is not None and at_val_f is not None and at_val_f > 0):
            ratio = sell_f / at_val_f * 100.0
            if cap_id not in cap_id_at_pct:
                cap_id_at_pct[cap_id] = {"sum": 0.0, "n": 0}
            cap_id_at_pct[cap_id]["sum"] += ratio
            cap_id_at_pct[cap_id]["n"]   += 1

        pass_dts  = dts   is None or dts   <= max_dts
        pass_gpum = gpu_m is None or gpu_m >= min_gpu_m
        if not (pass_dts and pass_gpum):
            continue

        g["sales_xxd"] += sales90 * max_days_sales / 90.0
        g["sales_7"]   += sales7

        sold = sales90 == 1
        sold_rate = sold and price_band != "interest_free"
        if dis       is not None and sold:
            g["dis_sum"]     += dis;        g["dis_n"]     += 1
        if dts       is not None and sold:
            g["dts_sum"]     += dts;        g["dts_n"]     += 1
        if sell      is not None and sell != 0 and sold_rate:
            g["sell_sum"]    += sell;       g["sell_n"]    += 1
        if at_val    is not None and sold_rate:
            g["at_val_sum"]  += at_val;     g["at_val_n"]  += 1
        if at_rating  is not None and sold:
            g["at_rate_sum"] += at_rating;  g["at_rate_n"] += 1
        if vehicle_costs is not None and sold and mileage_band:
            vc_key = (cap_id, mileage_band)
            if vc_key not in cap_id_vehicle_costs:
                cap_id_vehicle_costs[vc_key] = {"sum": 0.0, "n": 0}
            cap_id_vehicle_costs[vc_key]["sum"] += vehicle_costs
            cap_id_vehicle_costs[vc_key]["n"]   += 1
            if mileage_band not in band_vehicle_costs:
                band_vehicle_costs[mileage_band] = {"sum": 0.0, "n": 0}
            band_vehicle_costs[mileage_band]["sum"] += vehicle_costs
            band_vehicle_costs[mileage_band]["n"]   += 1
        if gpu_m_costs   is not None and sold:
            g["gpu_m_costs_sum"]   += gpu_m_costs;   g["gpu_m_costs_n"]   += 1
        if gpu_m     is not None and sold:
            g["gpu_m_sum"]   += gpu_m;      g["gpu_m_n"]   += 1
        if gpu_s     is not None and sold:
            g["gpu_s_sum"]   += gpu_s;      g["gpu_s_n"]   += 1
        if gpu_ms    is not None and sold:
            g["gpu_ms_sum"]  += gpu_ms;     g["gpu_ms_n"]  += 1

    # Fleet-wide totals (across all price bands)
    total_current_stock = sum(g["stock"]     for g in cap_data.values())
    total_xxd_sales     = sum(g["sales_xxd"] for g in cap_data.values())
    ideal_total         = total_current_stock + number_to_buy

    def _avg(s, n):
        return round(s / n, 1) if n > 0 else None

    def _avg_gbp(s, n):
        return round(s / n) if n > 0 else None

    # Per-cap_id derived columns (collapsed across all price bands)
    cap_requirements: dict = {}
    for cap_id, g in cap_data.items():
        xxd_sales   = g["sales_xxd"]
        stock       = g["stock"]
        ideal_stock = (ideal_total * xxd_sales / total_xxd_sales) if total_xxd_sales > 0 and xxd_sales > 0 else 0.0
        avg_sell    = _avg_gbp(g["sell_sum"],   g["sell_n"])
        avg_at_val  = round(g["at_val_sum"] / g["at_val_n"]) if g["at_val_n"] > 0 else None
        at_pct      = round((avg_sell / avg_at_val) * 100, 2) if avg_sell is not None and avg_at_val and avg_at_val != 0 else None
        cap_requirements[cap_id] = {
            "90d Sales":                 round(xxd_sales,          1),
            "7d Sales":                  round(g["sales_7"],        1),
            "Stock":                     round(stock,               0),
            "Ideal Stock":               round(ideal_stock,         1),
            "Required":                  round(ideal_stock - stock, 1),
            "Avg Days in Stock":         _avg(g["dis_sum"],     g["dis_n"]),
            "Avg Days to Sell":          _avg(g["dts_sum"],     g["dts_n"]),
            "Avg Sell Price":            avg_sell,
            "Avg AT PTM":                avg_at_val,
            "Avg AT PTM %":              at_pct,
            "Avg AT Rating":             round(g["at_rate_sum"] / g["at_rate_n"], 2) if g["at_rate_n"] > 0 else None,
            "Avg GPU-M Costs":           _avg_gbp(g["gpu_m_costs_sum"],   g["gpu_m_costs_n"]),
            "Avg GPU-M":                 _avg_gbp(g["gpu_m_sum"],  g["gpu_m_n"]),
            "Avg GPU-S":                 _avg_gbp(g["gpu_s_sum"],  g["gpu_s_n"]),
            "Avg GPU-MS":                _avg_gbp(g["gpu_ms_sum"], g["gpu_ms_n"]),
        }

    # ── 4. Join to elaborated file ────────────────────────────────────────────
    # For each vehicle: use CAP-rate-only avg AT % × AT retail valuation to
    # estimate its retail price, derive its price band, then look up
    # (cap_id, derived_price_band) for all requirement columns.
    req_cols = [
        "90d Sales", "7d Sales", "Stock", "Ideal Stock", "Required",
        "Avg Days in Stock", "Avg Days to Sell", "Avg Sell Price",
        "Avg AT PTM", "Avg AT PTM %", "Avg AT Rating",
        "Avg GPU-M Costs",
        "Avg GPU-M",
    ]

    AT_VAL_COL = "at_valuations_retail_valuation"
    cap_col    = "cap_capid" if "cap_capid" in elaborated_df.columns else None
    at_col     = AT_VAL_COL  if AT_VAL_COL  in elaborated_df.columns else None
    _mileage_candidates = ["mileage", "miles", "odometer", "odo", "odom"]
    mileage_col = next((c for c in elaborated_df.columns if any(cand in c.lower() for cand in _mileage_candidates)), None)

    def _mileage_to_band(m):
        try:
            m = int(float(m))
        except (TypeError, ValueError):
            return None
        if m <= 0:      return None
        if m < 10000:   return '<10k'
        if m < 20000:   return '10-20k'
        if m < 30000:   return '20-30k'
        if m < 40000:   return '30-40k'
        if m < 50000:   return '40-50k'
        if m < 60000:   return '50-60k'
        if m < 70000:   return '60-70k'
        return '70k+'

    def _lookup_vehicle_costs(cap_id_val, mileage_val):
        cid = _safe_int(cap_id_val)
        band = _mileage_to_band(mileage_val)
        if cid is None or not band:
            return None
        _bands = ['<10k', '10-20k', '20-30k', '30-40k', '40-50k', '50-60k', '60-70k', '70k+']
        idx = _bands.index(band) if band in _bands else -1
        for b in _bands[idx:]:
            entry = cap_id_vehicle_costs.get((cid, b))
            if entry and entry["n"] > 0:
                return round(entry["sum"] / entry["n"])
        # Fallback: average vehicle cost for this mileage band across all cap_ids
        entry = band_vehicle_costs.get(band)
        if entry and entry["n"] > 0:
            return round(entry["sum"] / entry["n"])
        return None

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

    elaborated_df["Mileage Band"] = elaborated_df.apply(
        lambda row: _mileage_to_band(row[mileage_col]) if mileage_col else None, axis=1
    )

    def lookup(cap_id_val, col):
        try:
            cid = int(float(cap_id_val))
        except (TypeError, ValueError):
            return None
        return cap_requirements.get(cid, {}).get(col)

    for col in req_cols:
        if cap_col:
            elaborated_df[col] = elaborated_df.apply(
                lambda row, c=col: lookup(row[cap_col], c), axis=1
            )
        else:
            elaborated_df[col] = None

    if cap_col and mileage_col:
        elaborated_df["Avg Vehicle Costs"] = elaborated_df.apply(
            lambda row: _lookup_vehicle_costs(row[cap_col], row[mileage_col]), axis=1
        )
    else:
        elaborated_df["Avg Vehicle Costs"] = None

    # ── 5. Write and return ───────────────────────────────────────────────────
    fd2, tmp_out = tempfile.mkstemp(suffix="." + _used_ext)
    os.close(fd2)
    try:
        write_dataframe_file(elaborated_df, tmp_out)
        base_name = task["filename"].rsplit(".", 1)[0]
        out_filename = f"{base_name}_requirements.{_used_ext}"
        logger.info("download: serving out_filename=%s tmp_out=%s", out_filename, tmp_out)
        return FileResponse(
            tmp_out,
            filename=out_filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{out_filename}"'},
        )
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
                "SELECT t.task_id, t.created, t.status, t.filename, t.total_rows, t.reductions, t.execution_time, t.message, s.supplier_name, t.uploaded_by, t.source, t.error_log_s3, t.aurora_cache_hits "
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


# ── MOT Check (admin only) ────────────────────────────────────────────────────
_mot_progress: dict = {}   # keyed by session_id: {pct, done, total, eta_s}
_mot_lock = threading.Lock()
_cap_lookup_progress: dict = {}
_cap_lookup_lock = threading.Lock()
_MOT_S3_PREFIX = "applications/pricing/uk-vehicle-list-evaluator/{env}/mot-check/"
_CAP_LOOKUP_S3_PREFIX = "applications/pricing/uk-vehicle-list-evaluator/{env}/cap-lookup/"

_CAP_LOOKUP_COLUMNS = [
    "VRM", "CAP ID", "Manufacturer", "Model", "Derivative", "Registration Date", "Note",
]


@router.get("/cap-lookup", response_class=HTMLResponse)
def cap_lookup_page(request: Request, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    _track(request, 'page_view', '/cap-lookup')
    return templates.TemplateResponse("cap_lookup.html", {"request": request, "user": user, "BASE_URL": BASE_URL})


@router.get("/cap-lookup/progress")
def cap_lookup_progress(request: Request, user: str = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=403)
    sid = request.session.get('session_id', '')
    with _cap_lookup_lock:
        p = _cap_lookup_progress.get(sid, {"pct": 0, "done": 0, "total": 0, "eta_s": None})
    return p


@router.post("/cap-lookup/process")
def cap_lookup_process(request: Request, payload: dict, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        raise HTTPException(status_code=403)

    from list_processor.cap_client import CAPClient
    from list_processor.structs import VehicleRow
    from concurrent.futures import ThreadPoolExecutor, as_completed

    vrms = [v.strip().upper() for v in (payload.get("vrms") or []) if v.strip()]
    if not vrms:
        raise HTTPException(status_code=400, detail="No VRMs provided")

    sid = request.session.get('session_id', '')
    total = len(vrms)
    _track(request, 'cap_lookup', '/cap-lookup', {'vrm_count': total})
    with _cap_lookup_lock:
        _cap_lookup_progress[sid] = {"pct": 0, "done": 0, "total": total, "eta_s": None}

    cap = CAPClient()
    results = [None] * total
    start = time.time()
    done_count = 0

    def fetch(idx_vrm):
        idx, vrm = idx_vrm
        try:
            vehicle = VehicleRow(vrm=vrm, mileage=0)
            data = cap.vrm_valuation(vehicle)
            return idx, vrm, data, None
        except Exception as e:
            return idx, vrm, None, str(e)

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(fetch, (i, v)): i for i, v in enumerate(vrms)}
        for fut in as_completed(futures):
            idx, vrm, data, err = fut.result()
            results[idx] = (vrm, data, err)
            done_count += 1
            elapsed = time.time() - start
            avg = elapsed / done_count
            eta_s = (total - done_count) * avg
            with _cap_lookup_lock:
                _cap_lookup_progress[sid] = {
                    "pct": round(done_count / total * 100, 1),
                    "done": done_count,
                    "total": total,
                    "eta_s": round(eta_s, 1),
                }

    with _cap_lookup_lock:
        _cap_lookup_progress.pop(sid, None)

    rows = []
    for vrm, data, err in results:
        if err or not data:
            rows.append([vrm, "", "", "", "", "", err or "Error"])
        else:
            rows.append([
                vrm,
                data.get('capid') or "",
                data.get('capman') or "",
                data.get('capmod') or "",
                data.get('capder') or "",
                data.get('registered_date') or "",
                data.get('note') or "",
            ])

    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    ts_str = _uk_now().strftime("%Y%m%d_%H%M%S")
    filename = f"cap_lookup_{ts_str}.xlsx"
    s3_key = _CAP_LOOKUP_S3_PREFIX.format(env=env) + filename

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CAP Lookup"

    header_fill = PatternFill("solid", fgColor="BDD7EE")
    header_font = Font(bold=True)
    error_fill  = PatternFill("solid", fgColor="FFC7CE")
    thin = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    note_col = _CAP_LOOKUP_COLUMNS.index("Note") + 1

    ws.append(_CAP_LOOKUP_COLUMNS)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
        cell.border = border

    for row in rows:
        ws.append(row)
        row_idx = ws.max_row
        for col_idx, cell in enumerate(ws[row_idx], start=1):
            cell.border = border
            if col_idx == note_col and cell.value:
                cell.fill = error_fill

    ws.freeze_panes = "A2"
    for col in ws.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max(max_len + 2, 10), 40)

    fd, tmp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        import boto3 as _boto3
        wb.save(tmp_path)
        with open(tmp_path, 'rb') as f:
            _boto3.client('s3').upload_fileobj(
                f, s3_bucketname, s3_key,
                ExtraArgs={"Metadata": {"vrm_count": str(len(vrms)), "processed_by": user}},
            )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    return {"columns": _CAP_LOOKUP_COLUMNS, "rows": rows, "filename": filename}


@router.get("/cap-lookup/history")
def cap_lookup_history(user: str = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=403)
    import boto3
    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    prefix = _CAP_LOOKUP_S3_PREFIX.format(env=env)
    try:
        resp = boto3.client('s3').list_objects_v2(Bucket=s3_bucketname, Prefix=prefix)
        objects = sorted(resp.get('Contents', []), key=lambda o: o['LastModified'], reverse=True)
        s3 = boto3.client('s3')

        def _meta(key):
            try:
                m = s3.head_object(Bucket=s3_bucketname, Key=key).get('Metadata', {})
                return {"vrm_count": int(m['vrm_count']) if m.get('vrm_count') else None, "processed_by": m.get('processed_by')}
            except Exception:
                return {"vrm_count": None, "processed_by": None}

        result = []
        for o in objects:
            if o['Key'] == prefix:
                continue
            meta = _meta(o['Key'])
            result.append({
                "filename":     o['Key'].replace(prefix, ''),
                "ts":           o['LastModified'].astimezone(ZoneInfo('Europe/London')).strftime("%d/%m/%Y %H:%M"),
                "size_kb":      round(o['Size'] / 1024, 1),
                "vrm_count":    meta["vrm_count"],
                "processed_by": meta["processed_by"],
            })
        return result
    except Exception as e:
        logger.warning("CAP lookup history S3 list failed: %s", e)
        return []


@router.get("/cap-lookup/download/{filename}")
def cap_lookup_download(filename: str, user: str = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=403)
    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    s3_key = _CAP_LOOKUP_S3_PREFIX.format(env=env) + filename
    fd, tmp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        download_file(tmp_path, s3_bucketname, s3_key)
        return FileResponse(
            tmp_path, filename=filename,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise HTTPException(status_code=404, detail="File not found in S3")


@router.get("/cap-lookup/load/{filename}")
def cap_lookup_load(filename: str, user: str = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=403)
    import openpyxl
    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    s3_key = _CAP_LOOKUP_S3_PREFIX.format(env=env) + filename
    fd, tmp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        download_file(tmp_path, s3_bucketname, s3_key)
        wb = openpyxl.load_workbook(tmp_path, read_only=True, data_only=True)
        ws = wb.active
        rows_iter = iter(ws.iter_rows(values_only=True))
        header = next(rows_iter, None)
        if not header:
            return {"columns": [], "rows": []}
        columns = [str(c) if c is not None else "" for c in header]
        rows = [[str(c) if c is not None else "" for c in row] for row in rows_iter]
        wb.close()
        return {"columns": columns, "rows": rows}
    except Exception:
        raise HTTPException(status_code=404, detail="File not found in S3")
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


@router.get("/mot-check", response_class=HTMLResponse)
def mot_check_page(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    _track(request, 'page_view', '/mot-check')
    return templates.TemplateResponse("mot_check.html", {"request": request, "user": user, "BASE_URL": BASE_URL})


@router.get("/mot-check/progress")
def mot_check_progress(request: Request, user: str = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=403)
    sid = request.session.get('session_id', '')
    with _mot_lock:
        p = _mot_progress.get(sid)
    if p is not None:
        return p
    # Fall back to S3 completion marker (poll may hit a different Lambda instance)
    try:
        import boto3 as _boto3
        env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
        s3_key = _MOT_S3_PREFIX.format(env=env) + f"progress_{sid}.json"
        obj = _boto3.client('s3', region_name='eu-west-1').get_object(Bucket=s3_bucketname, Key=s3_key)
        return json.loads(obj['Body'].read())
    except Exception:
        pass
    return {"pct": 0, "done": 0, "total": 0, "eta_s": None, "complete": False, "filename": None, "error": None}


@router.post("/mot-check/process")
def mot_check_process(request: Request, payload: dict, user: str = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=403)

    vrms = [v.strip().upper() for v in (payload.get("vrms") or []) if v.strip()]
    if not vrms:
        raise HTTPException(status_code=400, detail="No VRMs provided")

    sid = request.session.get('session_id', '')
    total = len(vrms)
    _track(request, 'mot_check', '/mot-check', {'vrm_count': total})

    with get_database_connection() as conn:
        settings = get_settings(conn, user=user)
    mot_workers = int(settings.get('mot_workers', 10))

    initial_state = {"pct": 0, "done": 0, "total": total, "eta_s": None, "complete": False, "filename": None, "error": None}
    with _mot_lock:
        _mot_progress[sid] = initial_state

    t = threading.Thread(target=_mot_process_background, args=(sid, vrms, user, mot_workers), daemon=True)
    t.start()

    return {"queued": True, "total": total}


def _mot_process_background(sid: str, vrms: list, user: str, mot_workers: int):
    from utils.dvsa_client import DVSAClient, NOT_FOUND as DVSA_NOT_FOUND
    from concurrent.futures import ThreadPoolExecutor, as_completed

    total = len(vrms)
    results = [None] * total
    start = time.time()
    done_count = 0
    errored_indices = {}
    _last_s3_write = [0.0]  # mutable for closure

    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    s3_progress_key = _MOT_S3_PREFIX.format(env=env) + f"progress_{sid}.json"

    def _write_progress_to_s3(state: dict):
        try:
            import boto3 as _boto3
            _boto3.client('s3', region_name='eu-west-1').put_object(
                Bucket=s3_bucketname, Key=s3_progress_key,
                Body=json.dumps(state).encode(), ContentType='application/json',
            )
        except Exception as _e:
            logger.warning("[DVSA] S3 progress write failed: %s", _e)

    # Write initial state to S3 as first thing in the thread — cold-start poll instances
    # need to see total > 0 from their first poll, before any DVSA requests complete.
    with _mot_lock:
        _initial = _mot_progress.get(sid, {"pct": 0, "done": 0, "total": total, "eta_s": None, "complete": False, "filename": None, "error": None})
    threading.Thread(target=_write_progress_to_s3, args=(_initial,), daemon=True).start()

    client = DVSAClient()

    def fetch(idx_vrm):
        idx, vrm = idx_vrm
        try:
            return idx, vrm, client.get_vehicle_by_vrm(vrm), None
        except Exception as e:
            logger.error("[DVSA] %s fetch error: %s", vrm, e)
            return idx, vrm, None, str(e)

    try:
        with ThreadPoolExecutor(max_workers=mot_workers) as pool:
            futures = {pool.submit(fetch, (i, v)): i for i, v in enumerate(vrms)}
            for fut in as_completed(futures):
                idx, vrm, vehicle_json, err = fut.result()
                not_found = vehicle_json is DVSA_NOT_FOUND
                results[idx] = _build_mot_row(vrm, None if not_found else vehicle_json, not_found=not_found)
                if err:
                    errored_indices[idx] = vrm
                done_count += 1
                elapsed = time.time() - start
                avg = elapsed / done_count
                eta_s = (total - done_count) * avg
                state = {
                    "pct": round(done_count / total * 100, 1),
                    "done": done_count,
                    "total": total,
                    "eta_s": round(eta_s, 1),
                    "complete": False,
                    "filename": None,
                    "error": None,
                }
                with _mot_lock:
                    _mot_progress[sid] = state
                # Write to S3 at most every 2s so cold-start poll instances see live progress
                now = time.time()
                if now - _last_s3_write[0] >= 2.0:
                    _last_s3_write[0] = now
                    threading.Thread(target=_write_progress_to_s3, args=(state,), daemon=True).start()

        if errored_indices:
            logger.warning("[DVSA] Retrying %d failed VRMs after 5s: %s", len(errored_indices), list(errored_indices.values()))
            time.sleep(5)
            for idx, vrm in errored_indices.items():
                try:
                    vehicle_json = client.get_vehicle_by_vrm(vrm)
                    not_found = vehicle_json is DVSA_NOT_FOUND
                    results[idx] = _build_mot_row(vrm, None if not_found else vehicle_json, not_found=not_found)
                    logger.info("[DVSA] Retry succeeded for %s", vrm)
                except Exception as e:
                    logger.error("[DVSA] Retry failed for %s: %s", vrm, e)

        total_elapsed = round(time.time() - start, 1)
        columns, rows, filename, counts = _mot_build_and_upload(results, vrms, user)

        completion = {
            "pct": 100,
            "done": total,
            "total": total,
            "eta_s": 0,
            "complete": True,
            "filename": filename,
            "error": None,
            "counts": counts,
        }
        with _mot_lock:
            _mot_progress[sid] = completion
        _write_progress_to_s3(completion)
        logger.info("[DVSA] MOT check complete for sid=%s: %d VRMs in %.1fs", sid, total, total_elapsed)

    except Exception as e:
        logger.error("[DVSA] MOT background error for sid=%s: %s", sid, e)
        error_state = {
            "pct": 0, "done": 0, "total": total, "eta_s": None,
            "complete": True, "filename": None, "error": str(e),
        }
        with _mot_lock:
            _mot_progress[sid] = error_state
        _write_progress_to_s3(error_state)


def _mot_build_and_upload(results: list, vrms: list, user: str):
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    columns = ["Registration", "Make", "Model", "Colour", "Fuel Type",
               "Date Registered", "Most Recent MOT Date", "MOT Expiry Date",
               "Odometer", "Status"]
    max_pairs = max((r["pairs"] for r in results), default=0)
    for i in range(1, max_pairs + 1):
        sfx = ("st" if i == 1 else "nd" if i == 2 else "rd" if i == 3 else "th")
        columns += [f"{i}{sfx} MOT Date", f"{i}{sfx} MOT Mileage"]
    columns.append("Comments")

    rows = []
    for r in results:
        row = [r["vrm"], r["make"], r["model"], r["colour"], r["fuel"],
               r["date_registered"], r["last_mot_date"], r["expiry_date"],
               r["odometer"], r["status"]]
        for d, m in r["test_pairs"]:
            row += [d, m]
        row += ["", ""] * (max_pairs - len(r["test_pairs"]))
        row.append(_build_mot_comments(r))
        rows.append(row)

    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    ts_str = _uk_now().strftime("%Y%m%d_%H%M%S")
    filename = f"mot_check_{ts_str}.xlsx"
    s3_key = _MOT_S3_PREFIX.format(env=env) + filename

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "MOT Check"

    header_fill = PatternFill("solid", fgColor="BDD7EE")
    header_font = Font(bold=True)
    valid_fill   = PatternFill("solid", fgColor="C6EFCE")
    expired_fill = PatternFill("solid", fgColor="FFC7CE")
    thin = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    status_col   = columns.index("Status")   + 1 if "Status"   in columns else None
    comments_col = columns.index("Comments") + 1 if "Comments" in columns else None

    ws.append(columns)
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
        cell.border = border

    for row in rows:
        ws.append(row)
        row_idx = ws.max_row
        for col_idx, cell in enumerate(ws[row_idx], start=1):
            cell.border = border
            if col_idx == status_col:
                val = cell.value or ""
                if val == "VALID":
                    cell.fill = valid_fill
                elif val == "EXPIRED":
                    cell.fill = expired_fill
            elif col_idx == comments_col and cell.value:
                cell.fill = expired_fill

    ws.freeze_panes = "A2"
    for col in ws.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max(max_len + 2, 10), 40)

    fd, tmp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        import boto3 as _boto3
        s3 = _boto3.client('s3', region_name='eu-west-1')
        wb.save(tmp_path)
        expired_count   = sum(1 for r in results if r.get("status") == "EXPIRED")
        unknown_count   = sum(1 for r in results if r.get("status") == "UNKNOWN")
        not_found_count = sum(1 for r in results if r.get("status") == "NOT_FOUND")

        def _has_mileage_warn(r):
            pairs = r.get("test_pairs", [])
            for j in range(1, len(pairs)):
                prev_m = pairs[j-1][1]
                curr_m = pairs[j][1]
                try:
                    if curr_m is not None and prev_m is not None and int(str(curr_m).replace(',','')) < int(str(prev_m).replace(',','')):
                        return True
                except (ValueError, TypeError):
                    pass
            return False

        def _has_early_mot(r):
            import datetime as _dt
            pairs = r.get("test_pairs", [])
            def _parse(s):
                if not s: return None
                for fmt in ('%d/%m/%Y', '%Y-%m-%d'):
                    try: return _dt.datetime.strptime(s, fmt).date()
                    except ValueError: pass
                return None
            dates = [_parse(p[0]) for p in pairs]
            for j in range(1, len(dates)):
                if dates[j-1] and dates[j]:
                    prev = dates[j-1]
                    try:
                        due = prev.replace(year=prev.year + 1)
                    except ValueError:
                        due = prev.replace(year=prev.year + 1, day=28)
                    if (due - dates[j]).days >= 60:
                        return True
            return False

        mileage_warn_count = sum(1 for r in results if _has_mileage_warn(r))
        early_mot_count    = sum(1 for r in results if _has_early_mot(r))
        valid_count        = sum(1 for r in results if r.get("status") == "VALID")

        metadata = {
            "vrm_count":          str(len(vrms)),
            "valid_count":        str(valid_count),
            "expired_count":      str(expired_count),
            "unknown_count":      str(unknown_count),
            "not_found_count":    str(not_found_count),
            "mileage_warn_count": str(mileage_warn_count),
            "early_mot_count":    str(early_mot_count),
            "processed_by":       user,
        }
        with open(tmp_path, 'rb') as f:
            s3.upload_fileobj(f, s3_bucketname, s3_key, ExtraArgs={"Metadata": metadata})
        # Upload JSON sidecar so load endpoint can serve results without re-parsing XLSX
        json_key = s3_key.replace('.xlsx', '.json')
        s3.put_object(
            Bucket=s3_bucketname, Key=json_key,
            Body=json.dumps({"columns": columns, "rows": rows}).encode(),
            ContentType='application/json',
            Metadata=metadata,
        )
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    return columns, rows, filename, {
        "vrm_count":          len(vrms),
        "valid_count":        valid_count,
        "expired_count":      expired_count,
        "unknown_count":      unknown_count,
        "not_found_count":    not_found_count,
        "mileage_warn_count": mileage_warn_count,
        "early_mot_count":    early_mot_count,
    }


@router.get("/mot-check/history")
def mot_check_history(user: str = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=403)
    import boto3
    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    prefix = _MOT_S3_PREFIX.format(env=env)
    try:
        resp = boto3.client('s3').list_objects_v2(Bucket=s3_bucketname, Prefix=prefix)
        objects = resp.get('Contents', [])
        objects.sort(key=lambda o: o['LastModified'], reverse=True)
        import boto3 as _boto3
        s3 = _boto3.client('s3')

        def _metadata(key):
            try:
                m = s3.head_object(Bucket=s3_bucketname, Key=key).get('Metadata', {})
                def _int(k): return int(m[k]) if m.get(k) else None
                return {
                    "vrm_count":          _int("vrm_count"),
                    "valid_count":        _int("valid_count"),
                    "expired_count":      _int("expired_count"),
                    "unknown_count":      _int("unknown_count"),
                    "not_found_count":    _int("not_found_count"),
                    "mileage_warn_count": _int("mileage_warn_count"),
                    "early_mot_count":    _int("early_mot_count"),
                    "processed_by":       m.get('processed_by'),
                }
            except Exception:
                return {"vrm_count": None, "valid_count": None, "expired_count": None, "unknown_count": None, "not_found_count": None, "mileage_warn_count": None, "early_mot_count": None, "processed_by": None}

        xlsx_objects = [o for o in objects if o['Key'] != prefix and o['Key'].replace(prefix, '').endswith('.xlsx')][:20]

        from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _asc
        with _TPE(max_workers=10) as pool:
            futures = {pool.submit(_metadata, o['Key']): o for o in xlsx_objects}
            meta_map = {fut: futures[fut] for fut in futures}
            results_map = {}
            for fut in _asc(futures):
                results_map[id(meta_map[fut])] = fut.result()

        result = []
        for o in xlsx_objects:
            meta = results_map[id(o)]
            result.append({
                "filename":           o['Key'].replace(prefix, ''),
                "ts":                 o['LastModified'].strftime("%d/%m/%Y %H:%M"),
                "vrm_count":          meta["vrm_count"],
                "valid_count":        meta["valid_count"],
                "expired_count":      meta["expired_count"],
                "unknown_count":      meta["unknown_count"],
                "not_found_count":    meta["not_found_count"],
                "mileage_warn_count": meta["mileage_warn_count"],
                "early_mot_count":    meta["early_mot_count"],
                "processed_by":       meta["processed_by"],
            })
        return result
    except Exception as e:
        logger.warning(f"MOT history S3 list failed: {e}")
        return []


@router.get("/mot-check/download/{filename}")
def mot_check_download(filename: str, user: str = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=403)
    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    s3_key = _MOT_S3_PREFIX.format(env=env) + filename
    fd, tmp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        download_file(tmp_path, s3_bucketname, s3_key)
        return FileResponse(tmp_path, filename=filename, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise HTTPException(status_code=404, detail="File not found in S3")


@router.get("/mot-check/load/{filename}")
def mot_check_load(filename: str, user: str = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=403)
    import boto3 as _boto3
    env = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'
    prefix = _MOT_S3_PREFIX.format(env=env)
    s3 = _boto3.client('s3', region_name='eu-west-1')

    # Try JSON sidecar first — much faster than downloading and re-parsing XLSX
    json_key = prefix + filename.replace('.xlsx', '.json')
    try:
        obj = s3.get_object(Bucket=s3_bucketname, Key=json_key)
        return json.loads(obj['Body'].read())
    except Exception:
        pass

    # Fall back to XLSX parsing for older files without a sidecar
    import openpyxl
    s3_key = prefix + filename
    fd, tmp_path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        download_file(tmp_path, s3_bucketname, s3_key)
        wb = openpyxl.load_workbook(tmp_path, read_only=True, data_only=True)
        ws = wb.active
        rows_iter = iter(ws.iter_rows(values_only=True))
        header = next(rows_iter, None)
        if not header:
            return {"columns": [], "rows": []}
        columns = [str(c) if c is not None else "" for c in header]
        rows = []
        for row in rows_iter:
            rows.append([str(c) if c is not None else "" for c in row])
        wb.close()
        return {"columns": columns, "rows": rows}
    except Exception as e:
        raise HTTPException(status_code=404, detail="File not found in S3")
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _parse_mot_datetime(s: str):
    if not s:
        return None
    s = s.strip()
    try:
        if "T" in s:
            return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        pass
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _to_ddmmyyyy(s: str) -> str:
    if not s:
        return ""
    dt = _parse_mot_datetime(s)
    if not dt:
        return ""
    return dt.strftime("%d/%m/%Y")




def _build_mot_comments(r: dict) -> str:
    notes = []

    pairs = r.get("test_pairs") or []
    # Mileage discrepancy: each mileage should be >= the previous
    prev_mileage = None
    for _date, mileage_str in pairs:
        try:
            m = int(str(mileage_str).replace(",", "")) if mileage_str else None
        except ValueError:
            m = None
        if prev_mileage is not None and m is not None and m < prev_mileage:
            notes.append("Mileage discrepancy")
            break
        if m is not None:
            prev_mileage = m

    # Current odometer vs most recent historical mileage
    if pairs:
        try:
            last_m = int(str(pairs[-1][1]).replace(",", "")) if pairs[-1][1] else None
        except ValueError:
            last_m = None
        try:
            odom = int(str(r.get("odometer", "")).replace(",", "")) if r.get("odometer") else None
        except ValueError:
            odom = None
        if last_m is not None and odom is not None and odom < last_m:
            if "Mileage discrepancy" not in notes:
                notes.append("Mileage discrepancy")

    # Early MOT: any MOT done 60+ days before the previous was due to expire
    dates = []
    for date_str, _ in pairs:
        try:
            d = datetime.datetime.strptime(date_str, "%d/%m/%Y").date() if date_str else None
        except ValueError:
            d = None
        dates.append(d)

    for j in range(1, len(dates)):
        prev_d = dates[j - 1]
        curr_d = dates[j]
        if prev_d is None or curr_d is None:
            continue
        try:
            due = datetime.date(prev_d.year + 1, prev_d.month, prev_d.day)
        except ValueError:
            due = datetime.date(prev_d.year + 1, 3, 1)
        if (due - curr_d).days >= 60:
            notes.append("Early MOT test")
            break

    return "; ".join(notes)


def _build_mot_row(vrm: str, vehicle_json, not_found: bool = False) -> dict:
    import zoneinfo
    uk_tz = zoneinfo.ZoneInfo("Europe/London")

    if not_found:
        return {"vrm": vrm, "make": "", "model": "", "colour": "", "fuel": "",
                "date_registered": "", "last_mot_date": "", "expiry_date": "",
                "odometer": "", "status": "NOT_FOUND", "test_pairs": [], "pairs": 0}

    empty = {"vrm": vrm, "make": "", "model": "", "colour": "", "fuel": "",
              "date_registered": "", "last_mot_date": "", "expiry_date": "",
              "odometer": "", "status": "UNKNOWN", "test_pairs": [], "pairs": 0}

    if not vehicle_json:
        return empty

    v = vehicle_json[0] if isinstance(vehicle_json, list) and vehicle_json else vehicle_json
    if not isinstance(v, dict):
        return empty

    tests = [t for t in (v.get("motTests") or [])
             if (t.get("testResult") or "").upper() == "PASSED"]
    tests.sort(key=lambda t: (_parse_mot_datetime(t.get("completedDate") or "") or datetime.datetime.min))

    most_recent = tests[-1] if tests else {}
    expiry = _to_ddmmyyyy(most_recent.get("expiryDate") or v.get("motTestExpiryDate") or "")

    status = "UNKNOWN"
    if expiry:
        try:
            exp_d = datetime.datetime.strptime(expiry, "%d/%m/%Y").date()
            today = datetime.datetime.now(uk_tz).date()
            status = "VALID" if exp_d >= today else "EXPIRED"
        except Exception:
            pass

    pairs = [(_to_ddmmyyyy(t.get("completedDate") or ""),
              str(t.get("odometerValue") or "").strip()) for t in tests]

    return {
        "vrm": vrm,
        "make": v.get("make") or "",
        "model": v.get("model") or "",
        "colour": v.get("primaryColour") or "",
        "fuel": v.get("fuelType") or "",
        "date_registered": _to_ddmmyyyy(v.get("firstUsedDate") or ""),
        "last_mot_date": _to_ddmmyyyy(most_recent.get("completedDate") or ""),
        "expiry_date": expiry,
        "odometer": str(most_recent.get("odometerValue") or "").strip(),
        "status": status,
        "test_pairs": pairs,
        "pairs": len(pairs),
    }


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


@router.get("/delete-task/{task_id}")
def delete_task(task_id: str, user: str = Depends(get_current_user)):
    with get_database_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM tasks WHERE task_id = %s AND status = 'pending'", (task_id,))
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
                break
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
            return

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

    logger.info(f"fetch_supplier_stock: fetched task_id={task_id} for {supplier} ({len(input_df)} rows)")
    await elaborate_task({'task_id': task_id, 'cap_workers': data['cap_workers'], 'cap_rate_limit': data['cap_rate_limit'], 'at_workers': data['at_workers'], 'at_rate_limit': data['at_rate_limit'], 'valuation_cache_hours': data.get('valuation_cache_hours'), 'start_time': data.get('fetch_start_time')})


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

        fd_in,  tmp_in  = tempfile.mkstemp(suffix='.' + file_ext)
        fd_out, tmp_out = tempfile.mkstemp(suffix='.xlsx')
        os.close(fd_in)
        os.close(fd_out)

        try:
            t = data.get('start_time') or time.time()

            download_file(tmp_in, s3_bucketname, s3_location)

            input_df = read_dataframe_file(tmp_in)
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

            snapshot_suppliers = {'Enterprise', 'Motability', '1link'}
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
            message = str(e)

        with get_database_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS error_log_s3 TEXT")
                cursor.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS aurora_cache_hits INTEGER")
                cursor.execute(
                    "UPDATE tasks SET status = %s, message = %s, total_rows = %s, execution_time = %s, s3_location = %s, reductions = %s, error_log_s3 = %s, aurora_cache_hits = %s WHERE task_id = %s",
                    (status, message, total_rows, execution_time, s3_location if status == 'success' else None, reductions if status == 'success' else None, error_log_s3 if status == 'success' else None, aurora_cache_hits if status == 'success' else None, data['task_id']),
                )
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