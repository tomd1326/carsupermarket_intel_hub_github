"""Buying requirements: Snowflake/DBS/Cars Bought caches, SSE streams, task download."""
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, FileResponse
import json
import os
import requests
import time
import logging
import datetime


from utils.s3_tools import download_file
from utils.db_utils import (get_database_connection, get_settings)
from utils.snowflake_client import SnowflakeClient
from utils.sharepoint_client import SharePointClient
from list_processor.cap_client import CAPClient
from utils.file_handler import read_dataframe_file, write_dataframe_file
from utils.sharepoint_log import get_entries as get_sharepoint_log_entries

from .core import (BASE_URL, _S3_ENV, templates, s3_bucketname, _track, get_current_user)

logger = logging.getLogger(__name__)

router = APIRouter()


def _prewarm_sharepoint(enable_cars_bought: bool) -> None:
    """
    Start the SharePoint worksheet fetches as soon as the page is requested, so
    they are already in flight (or finished) by the time the browser's DBS and
    Cars Bought requests arrive a moment later.

    Both files are fetched concurrently, so the wait is the slower file rather
    than the sum of the two. The single-flight locks in _get_dbs_purchases /
    _get_cb_purchases mean the browser's request reuses this work instead of
    starting its own.

    Fire-and-forget: never blocks or fails the page render. If the caches are
    already fresh these calls return immediately and cost nothing.
    """
    def _warm(label, fn):
        try:
            fn()
        except Exception as e:
            logger.warning(f"SharePoint prewarm ({label}) failed: {e}")

    try:
        # Only worth starting a fetch when there is nothing usable cached. On a
        # normal load the page reads the -cached endpoints, which are already
        # served from memory or S3, so prewarming would just repeat that work.
        targets = []
        if _get_dbs_purchases_cached(quiet=True) is None:
            targets.append(("Daily Buyers Sheet", _get_dbs_purchases))
        if enable_cars_bought and _get_cb_purchases_cached(quiet=True) is None:
            targets.append(("Cars Bought", _get_cb_purchases))
        if not targets:
            return
        for label, fn in targets:
            threading.Thread(
                target=_warm, args=(label, fn), name=f"sp-prewarm-{label}", daemon=True
            ).start()
    except Exception as e:
        logger.warning(f"SharePoint prewarm could not start: {e}")


@router.get("/buying-requirements", response_class=HTMLResponse)
def buying_requirements(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    with get_database_connection() as conn:
        settings = get_settings(conn, user=user)
    _track(request, 'page_view', '/buying-requirements')
    enable_cars_bought = user == 'tdrayson' or settings.get('enable_cars_bought', False)
    show_sharepoint_log = user == 'tdrayson'
    _prewarm_sharepoint(enable_cars_bought)
    return templates.TemplateResponse("buying_requirements.html", {"request": request, "user": user, "BASE_URL": BASE_URL, "settings": settings, "enable_cars_bought": enable_cars_bought, "show_sharepoint_log": show_sharepoint_log})


@router.get("/buying-requirements/sharepoint-log")
def buying_requirements_sharepoint_log(request: Request, user: str = Depends(get_current_user)):
    """Recent SharePoint-file log lines, newest first. tdrayson only."""
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    if user != 'tdrayson':
        raise HTTPException(status_code=403, detail="Forbidden")
    try:
        return {"status": "ok", "entries": get_sharepoint_log_entries()}
    except Exception as e:
        logger.error(f"Buying requirements SharePoint log failed: {e}")
        return {"status": "error", "message": str(e)}


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
        logger.error(f"Buying requirements Snowflake data failed: {e}")
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
            no_rrp      = _dbs_cache.get("no_rrp")
        return {"status": "ok", "purchases": out, "vehicles": vehicles, "total": total_vrms, "no_rrp": no_rrp, "fetched_at": fetched_at}
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
            return {"status": "ok", "purchases": {}, "vehicles": [], "total": None, "no_rrp": None, "fetched_at": None}
        purchases, vehicles = result
        if purchases and isinstance(next(iter(purchases)), tuple):
            purchases = {f"{k[0]}|{k[1]}": v for k, v in purchases.items()}
        with _cache_lock:
            fetched_at = _dbs_cache.get("fetched_at")
            total_vrms = _dbs_cache.get("total_vrms", len(vehicles))
            no_rrp     = _dbs_cache.get("no_rrp")
        if fetched_at is None:
            fetched_at = _load_dbs_fetched_at_from_s3()
        return {"status": "ok", "purchases": purchases, "vehicles": vehicles, "total": total_vrms, "no_rrp": no_rrp, "fetched_at": fetched_at}
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
            no_rrp      = _cb_cache.get("no_rrp")
        return {"status": "ok", "purchases": out, "vehicles": vehicles, "total": total_vrms, "no_rrp": no_rrp, "fetched_at": fetched_at}
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
            return {"status": "ok", "purchases": {}, "vehicles": [], "total": None, "no_rrp": None, "fetched_at": None}
        purchases, vehicles = result
        if purchases and isinstance(next(iter(purchases)), tuple):
            purchases = {f"{k[0]}|{k[1]}": v for k, v in purchases.items()}
        with _cache_lock:
            fetched_at = _cb_cache.get("fetched_at")
            total_vrms = _cb_cache.get("total_vrms", sum(purchases.values()) if purchases else 0)
            no_rrp     = _cb_cache.get("no_rrp")
        return {"status": "ok", "purchases": purchases, "vehicles": vehicles, "total": total_vrms, "no_rrp": no_rrp, "fetched_at": fetched_at}
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


# Worksheet read bounds. Graph's cost scales with the requested range, not with
# how much data is in it, so these are kept as tight as the sheets allow —
# reading A1:Z2000 for the Buyers Sheet took ~13s for 21 rows of data.
#
# WARNING: these column bounds are POSITIONAL. Column S is the last column we
# need from both sheets. If anyone inserts a column to the LEFT of S in either
# workbook, the data we need shifts to T and will be silently cut off — the
# parser looks columns up by header name, so it will not crash, it will just log
# "missing required column(s)" and return no purchases. Widen the bound here if
# the sheet layout changes. Same applies to the row bounds if the sheets grow.
_DBS_SUBFOLDER = "Department/Buying/B2B"
_DBS_FILENAME  = "NEW DAILY BUYERS SHEET.xlsx"
_DBS_SHEET     = "Today"
_DBS_COLUMNS   = ["CAP ID", "RRP", "RRP 0%"]
_DBS_MAX_COL   = "S"
_DBS_MAX_ROW   = 500

_CB_SUBFOLDER  = "Cars Bought"
_CB_FILENAME   = "CARS BOUGHT 2026.xlsx"
_CB_SHEET      = "TO GO ON"
_CB_MAX_COL    = "S"
_CB_MAX_ROW    = 2000

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
# Schema version of a Snowflake row built below. Bump on ANY change to the row's
# shape or field meaning — adding, removing, reordering, or repurposing a slot.
# A payload written by a different version is discarded and refetched, so an
# index-based reader never sees fields laid out to an older schema. Costs one
# refetch on the first load after deploy, then caching resumes as normal.
_SF_CACHE_VERSION = 2
_dbs_cache: dict = {"purchases": None, "vehicles": None, "expires": 0.0, "fetched_at": None, "no_rrp": None}
_cb_cache: dict  = {"purchases": None, "vehicles": None, "expires": 0.0, "fetched_at": None, "no_rrp": None}
_cache_lock = threading.Lock()

# Raw Daily Buyers Sheet worksheet values — short-lived, process-local cache so
# Cars Bought's VRM dedup (_fetch_dbs_vrms) can reuse whatever _get_dbs_purchases
# just fetched instead of re-running the whole 5-round-trip SharePoint fetch for
# the same file/sheet. Short TTL because this is purely to collapse fetches that
# land within the same page-load burst, not a substitute for _dbs_cache's own TTL.
_dbs_raw_values_cache: dict = {"values": None, "expires": 0.0}
_DBS_RAW_TTL_SECONDS = 60

# Serialises the actual SharePoint download. Without it, concurrent callers all
# miss the cache together (the TTL check above releases _cache_lock before the
# slow fetch) and each starts its own download — the Daily Buyers Sheet endpoint
# and Cars Bought's VRM dedup run in parallel on every refresh, so this happened
# every time. Holding this lock across the fetch makes the second caller wait
# for and reuse the first caller's result instead.
_dbs_raw_fetch_lock = threading.Lock()

# Single-flight locks for the full purchase rebuilds (see _get_dbs_purchases /
# _get_cb_purchases). Separate from _cache_lock, which is only ever held for
# quick dict reads/writes and must never be held across a network call.
_dbs_build_lock = threading.Lock()
_cb_build_lock = threading.Lock()
_sf_build_lock = threading.Lock()


def _fetch_dbs_raw_values() -> list:
    """Fetch the raw Daily Buyers Sheet worksheet, reusing a very recent fetch if one exists."""

    def _cached_if_fresh():
        with _cache_lock:
            cached = _dbs_raw_values_cache["values"]
            if cached is not None and time.monotonic() < _dbs_raw_values_cache["expires"]:
                return cached
        return None

    fresh = _cached_if_fresh()
    if fresh is not None:
        return fresh

    with _dbs_raw_fetch_lock:
        # Re-check: another caller may have completed the fetch while we waited.
        fresh = _cached_if_fresh()
        if fresh is not None:
            return fresh
        values = SharePointClient().get_worksheet_values(
            _DBS_SUBFOLDER, _DBS_FILENAME, _DBS_SHEET,
            max_col=_DBS_MAX_COL, max_row=_DBS_MAX_ROW,
        )
        with _cache_lock:
            _dbs_raw_values_cache["values"] = values
            _dbs_raw_values_cache["expires"] = time.monotonic() + _DBS_RAW_TTL_SECONDS
        return values

# dbt run hours (UK local time) — data available 15 min after each
_SF_SCHEDULE_HOURS_UK = [3, 7, 10, 13, 16]
_SF_READY_OFFSET_MIN  = 15

# Fallbacks used only if the settings lookup fails; the live values come from
# the Buying Requirements settings page (see _sp_cache_seconds below).
_DBS_TTL_SECONDS    = 30 * 60
_DBS_S3_TTL_SECONDS = 30 * 60
_CB_TTL_SECONDS     = 30 * 60
_CB_S3_TTL_SECONDS  = 30 * 60

# Cache-duration settings are read per request but change rarely, so keep a
# short in-process memo rather than hitting Aurora on every cache check.
#
# This MUST have its own lock, not _cache_lock. These durations are read from
# inside code that already holds _cache_lock (e.g. _adopt_dbs_s3_cache setting
# an expiry), and _cache_lock is a plain Lock — reacquiring it on the same
# thread deadlocks the request permanently.
_cache_settings = {"values": None, "expires": 0.0}
_CACHE_SETTINGS_TTL = 60
_cache_settings_lock = threading.Lock()


def _cache_durations() -> tuple[int, int]:
    """
    (sharepoint_seconds, snowflake_seconds) from settings.

    snowflake_seconds of 0 means "no time limit — follow the dbt schedule",
    which is the default and the normal case.

    Safe to call while holding _cache_lock — see the note above.
    """
    now = time.monotonic()
    with _cache_settings_lock:
        cached = _cache_settings["values"]
        if cached is not None and now < _cache_settings["expires"]:
            return cached
    try:
        with get_database_connection() as conn:
            s = get_settings(conn, user='buyer')
        values = (int(s.get('sharepoint_cache_minutes', 30)) * 60,
                  int(s.get('snowflake_cache_minutes', 0)) * 60)
    except Exception as e:
        logger.warning(f"Could not read cache duration settings, using defaults: {e}")
        values = (_DBS_TTL_SECONDS, 0)
    with _cache_settings_lock:
        _cache_settings["values"] = values
        _cache_settings["expires"] = now + _CACHE_SETTINGS_TTL
    return values


def _sp_cache_seconds() -> int:
    return _cache_durations()[0]
_S3_BUCKET         = "aa-raw-sources-uk"
# _S3_ENV imported from core
_SF_S3_KEY         = f"applications/pricing/uk-vehicle-list-evaluator/{_S3_ENV}/cache/buying_requirements_v22.json"
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
    """
    True if the cached Snowflake rows are still usable.

    The data only changes when dbt runs, so the primary test is whether the
    cache predates the most recent schedule window. The optional
    'snowflake_cache_minutes' setting adds a maximum age on top of that, for
    when you want a refetch sooner than the next dbt run (0 = schedule only).
    """
    if cached_at < _sf_last_valid_window_utc():
        return False
    max_age = _cache_durations()[1]
    if max_age > 0 and (time.time() - cached_at) > max_age:
        return False
    return True


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
        # Absent version = written before versioning, so its layout is unknown.
        if payload.get("version") != _SF_CACHE_VERSION:
            return None
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
            Body=json.dumps({"rows": rows, "cached_at": cached_at, "version": _SF_CACHE_VERSION}, default=_json_default).encode(),
            ContentType="application/json",
        )
    except Exception as e:
        logger.warning(f"Failed to write Snowflake S3 cache: {e}")


def _fmt_age(seconds: float) -> str:
    """Human-readable data age for cache log lines."""
    seconds = max(0.0, float(seconds or 0.0))
    if seconds < 90:
        return f"{int(seconds)}s"
    minutes = seconds / 60.0
    if minutes < 90:
        return f"{minutes:.0f}m"
    return f"{minutes / 60.0:.1f}h"


def _purchases_keys_to_tuples(purchases: dict) -> dict:
    """S3 stores purchase keys as "cap|band" strings; callers index by (cap_id, band)."""
    out = {}
    for k, v in (purchases or {}).items():
        if isinstance(k, tuple):
            out[k] = v
            continue
        cap_str, _, band = str(k).partition('|')
        try:
            cap_id = int(cap_str)
        except (TypeError, ValueError):
            cap_id = cap_str
        out[(cap_id, band)] = v
    return out


def _load_dbs_cache_from_s3() -> tuple[dict, list] | None:
    payload = _s3_load(_DBS_S3_KEY)
    if not payload:
        return None
    purchases = payload.get("purchases", {})
    vehicles  = payload.get("vehicles", [])
    with _cache_lock:
        _dbs_cache["total_vrms"] = payload.get("total_vrms", len(vehicles))
        _dbs_cache["no_rrp"]     = payload.get("no_rrp")
    return purchases, vehicles


def _adopt_dbs_s3_cache() -> dict | None:
    """
    Populate the in-process DBS cache from a fresh S3 payload, if one exists.

    Without this a cold Lambda — or one whose 30-minute in-process TTL has just
    expired — pays the full ~8-15s SharePoint fetch even though a perfectly good
    S3 payload is sitting there. Mirrors what _get_sf_rows already does for
    Snowflake. Returns the purchases dict (tuple keys) or None on a miss.
    """
    payload = _s3_load(_DBS_S3_KEY)
    if not payload:
        return None
    purchases = _purchases_keys_to_tuples(payload.get("purchases", {}))
    vehicles  = payload.get("vehicles", [])
    fetched_at = payload.get("fetched_at")
    with _cache_lock:
        _dbs_cache["purchases"]  = purchases
        _dbs_cache["vehicles"]   = vehicles
        _dbs_cache["total_vrms"] = payload.get("total_vrms", len(vehicles))
        _dbs_cache["no_rrp"]     = payload.get("no_rrp")
        _dbs_cache["fetched_at"] = fetched_at
        # Hold it in memory for whatever is left of the S3 payload's own life.
        remaining = max(0.0, payload.get("expires_at", 0.0) - time.time())
        _dbs_cache["expires"]    = time.monotonic() + min(_sp_cache_seconds(), remaining)
    logger.info("Daily Buyers Sheet: served from S3 cache (no SharePoint fetch needed)")
    return purchases


def _save_dbs_cache_to_s3(purchases: dict, vehicles: list, ttl: int = _DBS_TTL_SECONDS, total_vrms: int = None, no_rrp: int = None) -> None:
    _s3_save(_DBS_S3_KEY, {"purchases": {f"{k[0]}|{k[1]}": v for k, v in purchases.items()}, "vehicles": vehicles, "total_vrms": total_vrms, "no_rrp": no_rrp, "fetched_at": time.time()}, ttl)


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

    # Serialise the rebuild. The page opens /stream-snowflake and calls
    # /sf-data at the same moment, so without this both miss the cache together
    # and each runs the full buying_requirements query.
    entered_at = time.time()
    with _sf_build_lock:
        with _cache_lock:
            cached_at = _sf_cache.get("cached_at", 0.0)
            rows = _sf_cache["rows"]
            # Reuse whatever the holder of the lock just fetched. On a forced
            # refresh that means any result produced after we started waiting —
            # two racing Refresh Data clicks share one query rather than two.
            if rows is not None and (cached_at >= entered_at or (not force and _sf_cache_is_fresh(cached_at))):
                return rows
        return _build_sf_rows(force)


def _build_sf_rows(force: bool = False) -> list:
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
                COALESCE(MILEAGE_BAND, '') AS MILEAGE_BAND,
                WEBSITE_URL,
                IS_ADVERTISED
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
    #  27:invoice_posted_date, 28:vehicle_updated_at, 29:make_model, 30:cap_live_clean,
    #  31:vehicle_costs, 32:gpu_m_costs, 33:mileage_band, 34:website_url,
    #  35:is_advertised]
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
        _g(r, 'WEBSITE_URL'),
        (1 if _g(r, 'IS_ADVERTISED') else 0),
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

    # Only worth logging when there is actually work to do — this runs on both
    # the DBS and Cars Bought paths, so logging unconditionally produced several
    # identical "0 VRMs need lookup" lines per refresh.
    if not need_api:
        return rows
    logger.info(f"VRM CAP cache: {len(need_api)} VRMs need CAP API lookup")
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

    # Serialise the rebuild so concurrent callers that missed the TTL check
    # together don't each run the full parse.
    with _dbs_build_lock:
        with _cache_lock:
            if not force and _dbs_cache["purchases"] is not None and time.monotonic() < _dbs_cache["expires"]:
                return _dbs_cache["purchases"]
        # Fall back to S3 before paying for a live SharePoint fetch.
        if not force:
            adopted = _adopt_dbs_s3_cache()
            if adopted is not None:
                return adopted
        return _build_dbs_purchases()


def _build_dbs_purchases() -> dict:
    for _attempt in range(1, 4):
        try:
            values = _fetch_dbs_raw_values()
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
            logger.warning(
                f"Daily Buyers Sheet: missing required column(s) — REG={reg_idx} CAP ID={cap_idx} "
                f"RRP={rrp_idx} RRP 0%={rrp0_idx}. Headers: {headers_norm}. "
                f"NOTE: the sheet is only read to column {_DBS_MAX_COL} — if a column has been "
                f"inserted to the left, the ones we need have shifted past that bound. "
                f"Widen _DBS_MAX_COL if so."
            )
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
    logger.info(f"Daily Buyers Sheet: {sum(purchases.values())} purchases in {len(purchases)} cap/band keys (from {data_rows} rows, {total_dbs_vrms} unique VRMs, skipped: {skipped_no_cap} no CAP ID, {skipped_no_rrp} no RRP, {skipped_no_sf} not in Snowflake)")
    fetched_at = time.time()
    with _cache_lock:
        _dbs_cache["purchases"]  = purchases
        _dbs_cache["vehicles"]   = vehicles
        _dbs_cache["total_vrms"] = total_dbs_vrms
        _dbs_cache["no_rrp"]     = skipped_no_rrp
        _dbs_cache["expires"]    = time.monotonic() + _sp_cache_seconds()
        _dbs_cache["fetched_at"] = fetched_at
    _save_dbs_cache_to_s3(purchases, vehicles, _sp_cache_seconds(), total_dbs_vrms, skipped_no_rrp)
    return purchases


def _get_dbs_purchases_cached(quiet: bool = False) -> tuple[dict, list] | None:
    """
    Return (purchases, vehicles) from in-process or S3 cache only — never fetches live.

    quiet=True suppresses the cache-layer log line, for internal probes (the
    prewarm) that only want to know whether anything is cached.
    """
    with _cache_lock:
        if _dbs_cache["purchases"] is not None and time.monotonic() < _dbs_cache["expires"]:
            if not quiet:
                age = time.time() - (_dbs_cache.get("fetched_at") or time.time())
                logger.info(f"Daily Buyers Sheet: served from memory cache (data {_fmt_age(age)} old)")
            return _dbs_cache["purchases"], _dbs_cache.get("vehicles") or []
    result = _load_dbs_cache_from_s3()
    if quiet:
        return result
    if result is None:
        logger.info("Daily Buyers Sheet: no cached data available (memory and S3 both empty/expired)")
    else:
        age = time.time() - (_load_dbs_fetched_at_from_s3() or time.time())
        logger.info(f"Daily Buyers Sheet: served from S3 cache (data {_fmt_age(age)} old)")
    return result


# ── Cars Bought cache helpers ─────────────────────────────────────────────────

def _load_cb_cache_from_s3() -> tuple[dict, list] | None:
    payload = _s3_load(_CB_S3_KEY)
    if not payload:
        return None
    with _cache_lock:
        _cb_cache["total_vrms"] = payload.get("total_vrms")
        _cb_cache["no_rrp"]     = payload.get("no_rrp")
        # Carry the original fetch time through so callers can report data age.
        _cb_cache["fetched_at"] = payload.get("fetched_at")
    return payload.get("purchases", {}), payload.get("vehicles", [])


def _adopt_cb_s3_cache() -> dict | None:
    """
    Populate the in-process Cars Bought cache from a fresh S3 payload, if one
    exists — avoids a ~8s SharePoint fetch plus a Snowflake dedup query on a
    cold Lambda. See _adopt_dbs_s3_cache.
    """
    payload = _s3_load(_CB_S3_KEY)
    if not payload:
        return None
    purchases = _purchases_keys_to_tuples(payload.get("purchases", {}))
    vehicles  = payload.get("vehicles", [])
    with _cache_lock:
        _cb_cache["purchases"]  = purchases
        _cb_cache["vehicles"]   = vehicles
        _cb_cache["total_vrms"] = payload.get("total_vrms")
        _cb_cache["no_rrp"]     = payload.get("no_rrp")
        _cb_cache["fetched_at"] = payload.get("fetched_at")
        remaining = max(0.0, payload.get("expires_at", 0.0) - time.time())
        _cb_cache["expires"]    = time.monotonic() + min(_sp_cache_seconds(), remaining)
    logger.info("Cars Bought: served from S3 cache (no SharePoint fetch needed)")
    return purchases


def _save_cb_cache_to_s3(purchases: dict, vehicles: list, fetched_at: float, ttl: int = _CB_S3_TTL_SECONDS, total_vrms: int = None, no_rrp: int = None) -> None:
    _s3_save(_CB_S3_KEY, {"purchases": {f"{k[0]}|{k[1]}": v for k, v in purchases.items()}, "vehicles": vehicles, "total_vrms": total_vrms, "no_rrp": no_rrp, "fetched_at": fetched_at}, ttl)


class _CarsBoughtSharePointClient(SharePointClient):
    TEAM_SITE = "/sites/CarSupermarketOperationalData"


def _get_cb_purchases(force: bool = False) -> dict:
    """Return purchases dict keyed by (cap_id, price_band) for Cars Bought not on Apex."""
    now = time.monotonic()
    with _cache_lock:
        if not force and _cb_cache["purchases"] is not None and now < _cb_cache["expires"]:
            return _cb_cache["purchases"]

    # Serialise the rebuild so concurrent callers that missed the TTL check
    # together don't each run the full SharePoint + Snowflake fetch.
    with _cb_build_lock:
        with _cache_lock:
            if not force and _cb_cache["purchases"] is not None and time.monotonic() < _cb_cache["expires"]:
                return _cb_cache["purchases"]
        # Fall back to S3 before paying for a live SharePoint + Snowflake fetch.
        if not force:
            adopted = _adopt_cb_s3_cache()
            if adopted is not None:
                return adopted
        return _build_cb_purchases()


def _build_cb_purchases() -> dict:
    def _fetch_cb_values():
        for _attempt in range(1, 4):
            try:
                return _CarsBoughtSharePointClient().get_worksheet_values(
                    _CB_SUBFOLDER, _CB_FILENAME, _CB_SHEET,
                    max_col=_CB_MAX_COL, max_row=_CB_MAX_ROW,
                )
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
            # Fallback: extract non-empty VRMs from the in-process Snowflake cache
            with _cache_lock:
                sf_rows = _sf_cache.get("rows") or []
            result = {str(r[18]).strip().upper() for r in sf_rows if len(r) > 18 and r[18]}
            logger.info(f"Cars Bought: using Snowflake cache fallback — {len(result)} Apex VRMs for dedup")
            return result

    def _fetch_dbs_vrms():
        try:
            values = _fetch_dbs_raw_values()
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
        except ValueError as e:
            logger.warning(
                f"Cars Bought: missing required column(s) ({e}). Headers: {headers}. "
                f"NOTE: the sheet is only read to column {_CB_MAX_COL} — if a column has been "
                f"inserted to the left, the ones we need have shifted past that bound. "
                f"Widen _CB_MAX_COL if so."
            )
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
        _cb_cache["no_rrp"]      = skipped_no_rrp
        _cb_cache["expires"]     = time.monotonic() + _sp_cache_seconds()
        _cb_cache["fetched_at"]  = fetched_at
    _save_cb_cache_to_s3(purchases, vehicles, fetched_at, _sp_cache_seconds(), total_vrms, skipped_no_rrp)
    logger.info(f"Cars Bought: {total_vrms} unique VRMs, {sum(purchases.values())} in table ({len(purchases)} cap/band keys), skipped: {skipped_no_cap} no CAP ID, {skipped_no_rrp} no RRP, {skipped_no_sf} not in Snowflake")
    return purchases



def _get_cb_purchases_cached(quiet: bool = False) -> tuple[dict, list] | None:
    """Return (purchases, vehicles) from in-process or S3 cache only. See _get_dbs_purchases_cached."""
    with _cache_lock:
        if _cb_cache["purchases"] is not None and time.monotonic() < _cb_cache["expires"]:
            if not quiet:
                age = time.time() - (_cb_cache.get("fetched_at") or time.time())
                logger.info(f"Cars Bought: served from memory cache (data {_fmt_age(age)} old)")
            return _cb_cache["purchases"], _cb_cache.get("vehicles") or []
    result = _load_cb_cache_from_s3()
    if quiet:
        return result
    if result is None:
        logger.info("Cars Bought: no cached data available (memory and S3 both empty/expired)")
    else:
        with _cache_lock:
            fetched_at = _cb_cache.get("fetched_at")
        age = time.time() - (fetched_at or time.time())
        logger.info(f"Cars Bought: served from S3 cache (data {_fmt_age(age)} old)")
    return result


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
