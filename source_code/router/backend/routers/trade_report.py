"""Trade Report page.

Replaces the "Trade report LIVE.xlsx" workbook, which pulled the
trade_sales__p model (uk_dbt_product_data) in through a Google Sheet CSV and
then rebuilt the same tables five times over with COUNTIFS / AVERAGEIFS
(month by site, month by auction, year by site, year by month).

The page sends the whole table to the browser once and the browser does the
slicing, the same way Orders & Deliveries works: every view on the page is a
different cut of the same rows, so a change of view or period is instant and
needs no server call. The table holds every trade sale in the source (about
4,000 rows a year) so the payload stays well under the Lambda response limit.

Caching: memory -> S3 -> Snowflake build -> stale S3 fallback. The model is
built once a day, so a payload is kept for _CACHE_SECONDS and the page's
Refresh button forces a rebuild.
"""
import datetime
import decimal
import json
import logging
import time

import boto3

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse

from utils.db_utils import get_database_connection, get_user_preference
from utils.snowflake_client import SnowflakeClient
from . import kpi
from .core import (BASE_URL, templates, _track, s3_bucketname,
                   s3_bucket_region, require_page, require_page_api, is_admin)

logger = logging.getLogger(__name__)

router = APIRouter()

PAGE_KEY = 'trade_report'

# Columns read from the model, in payload order. (snowflake column, json key).
# The rows are sent as arrays in this order (see "columns" in the payload) so
# the payload is about half the size of one row-object per vehicle.
_COLUMNS = [
    ("VRM", "vrm"),
    ("STOCK_ID", "stock_id"),
    ("SALES_CHANNEL", "sales_channel"),
    ("SUPPLIER", "supplier"),
    ("SUPPLIER_CATEGORY", "supplier_category"),
    ("DAYS_IN_STOCK", "days_in_stock"),
    ("BRANCH_ID", "branch_id"),
    ("BRANCH_NAME", "branch_name"),
    ("BRANCH_CATEGORY", "branch_category"),
    ("AUCTION", "auction"),
    ("MAKE", "make"),
    ("MODEL", "model"),
    ("MILEAGE", "mileage"),
    ("FINANCIAL_YEAR", "financial_year"),
    ("FINANCIAL_PERIOD", "financial_period"),
    ("MONTH", "month"),
    ("INVOICE_DATE", "invoice_date"),
    ("VAT_STATUS", "vat_status"),
    ("PURCHASE_PRICE_EX_VAT", "purchase_price_ex_vat"),
    ("PURCHASE_PRICE_INC_VAT", "purchase_price_inc_vat"),
    ("NET_SALE_AMOUNT", "net_sale_amount"),
    ("SALE_VAT_AMOUNT", "sale_vat_amount"),
    ("GROSS_SALE_AMOUNT", "gross_sale_amount"),
    ("GROSS_MARGIN", "gross_margin"),
    ("SIV", "siv"),
    ("RECON_COST", "recon_cost"),
    ("TOTAL_COSTS", "total_costs"),
    ("NET_MARGIN", "net_margin"),
    ("CAP_ID", "cap_id"),
    ("CAP_VALUATION_LIVE_CLEAN", "cap_clean"),
    ("PURCHASE_PRICE_CAP_PERCENTAGE", "purchase_cap_pct"),
    ("SALE_CAP_PERCENTAGE", "sale_cap_pct"),
]

_TABLE = "PROD_CARSUPERMARKET_DB.PRODUCT_SALES_PERFORMANCE.TRADE_SALES"

# Bump whenever _COLUMNS changes, otherwise an old S3 payload (with the old
# column list) is served until it expires.
_CACHE_VERSION = 1
_S3_KEY = (f"applications/pricing/uk-vehicle-list-evaluator/shared/cache/"
           f"trade_report_v{_CACHE_VERSION}.json")
# The model is rebuilt once a day; an hour is short enough that a morning
# rebuild shows up without anyone pressing Refresh.
_CACHE_SECONDS = 60 * 60

_mem_cache: dict | None = None
_mem_cache_at: float = 0.0


def _s3():
    return boto3.client("s3", region_name=s3_bucket_region)


def _s3_load() -> tuple[float, dict | None]:
    try:
        obj = _s3().get_object(Bucket=s3_bucketname, Key=_S3_KEY)
        payload = json.loads(obj["Body"].read())
        return payload.get("cached_at", 0.0), payload.get("data")
    except Exception as e:
        logger.warning("Trade Report S3 cache read failed: %s", e)
        return 0.0, None


def _s3_save(cached_at: float, data: dict) -> None:
    try:
        _s3().put_object(
            Bucket=s3_bucketname, Key=_S3_KEY,
            Body=json.dumps({"cached_at": cached_at, "data": data}).encode(),
            ContentType="application/json",
        )
    except Exception as e:
        logger.warning("Trade Report S3 cache write failed: %s", e)


def _json_value(v):
    if isinstance(v, datetime.datetime):
        return v.date().isoformat()
    if isinstance(v, datetime.date):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        # Keep whole numbers whole (ids, counts), otherwise a float.
        return int(v) if v == v.to_integral_value() else float(v)
    return v


def _build_data() -> dict:
    """Read the whole trade_sales table. Only called on a cache miss or a
    manual refresh."""
    select_cols = ", ".join(sf for sf, _ in _COLUMNS)
    with SnowflakeClient() as sf:
        rows, cols = sf.execute(f"SELECT {select_cols} FROM {_TABLE} ORDER BY INVOICE_DATE DESC, STOCK_ID")
    idx = {c.upper(): i for i, c in enumerate(cols)}
    order = [idx[sf.upper()] for sf, _ in _COLUMNS]
    out = [[_json_value(r[i]) for i in order] for r in rows]
    return {
        "status": "ok",
        "columns": [key for _, key in _COLUMNS],
        "rows": out,
    }


def _get_data(force_refresh: bool = False) -> dict:
    global _mem_cache, _mem_cache_at
    now = time.time()
    if not force_refresh:
        if _mem_cache is not None and now - _mem_cache_at < _CACHE_SECONDS:
            return {**_mem_cache, "cached_at": _mem_cache_at}
        cached_at, cached = _s3_load()
        if cached and now - cached_at < _CACHE_SECONDS:
            _mem_cache, _mem_cache_at = cached, cached_at
            return {**cached, "cached_at": cached_at}

    logger.info("Trade Report: building from Snowflake (force_refresh=%s)", force_refresh)
    try:
        result = _build_data()
    except Exception as e:
        logger.error("Trade Report build failed: %s", e)
        cached_at, cached = _s3_load()
        if cached:
            logger.warning("Trade Report: build failed, serving stale S3 cache")
            return {**cached, "cached_at": cached_at, "stale": True}
        return {"status": "error", "message": str(e)}

    now = time.time()
    _mem_cache, _mem_cache_at = result, now
    _s3_save(now, result)
    return {**result, "cached_at": now}


# Settings > View Vehicles: the columns of the vehicle list, saved in kpi_layout
# with the other pages' settings. Nothing saved yet = the page's default columns.
_VIEW_KEY = 'trade_vehicle_view'
_VIEW_FORMATS = {'number', 'gbp', 'pct', 'text', 'date', 'month'}
_VIEW_SUMMARIES = {'none', 'sum', 'avg'}


def _clean_view(view) -> dict:
    """The setting as it is stored, or ValueError saying what is wrong with it."""
    if not isinstance(view, dict):
        raise ValueError("Nothing to save")
    known = {key for _, key in _COLUMNS}

    def label_of(item):
        label = item.get("label") or ""
        if not isinstance(label, str) or len(label.strip()) > 60:
            raise ValueError("A heading can be at most 60 characters")
        return label.strip()

    columns, seen = [], set()
    for c in view.get("columns") or []:
        if not isinstance(c, dict) or c.get("column") not in known or c["column"] in seen:
            raise ValueError(f"Unknown or repeated column: {c.get('column') if isinstance(c, dict) else c}")
        seen.add(c["column"])
        decimals = c.get("decimals")
        # bool is an int in Python, and True is not a number of decimal places.
        if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 4:
            raise ValueError(f"Decimals must be 0 to 4 for {c['column']}")
        if c.get("format") not in _VIEW_FORMATS:
            raise ValueError(f"Unknown format for {c['column']}")
        if c.get("summary", "none") not in _VIEW_SUMMARIES:
            raise ValueError(f"Unknown total for {c['column']}")
        columns.append({"column": c["column"], "label": label_of(c), "format": c["format"],
                        "decimals": decimals, "summary": c.get("summary", "none")})
    if not columns:
        raise ValueError("Choose at least one column")

    filters = []
    for f in view.get("filters") or []:
        if not isinstance(f, dict) or f.get("column") not in known:
            raise ValueError("Choose a column for each quick filter, or remove it")
        filters.append({"column": f["column"], "label": label_of(f)})
    return {"columns": columns, "filters": filters}


def _saved_view() -> dict | None:
    try:
        stored = kpi._read_kpi_layout_value(_VIEW_KEY)
        return _clean_view(stored) if stored else None
    except Exception as e:
        logger.warning("Trade Report: could not load the View Vehicles columns: %s", e)
        return None


_FONT_DEFAULT = 13
_FONT_MIN, _FONT_MAX = 8, 20
_FONT_PREF = 'trade_report_font'


def _saved_font(conn, user: str) -> int:
    try:
        v = int(get_user_preference(conn, user, _FONT_PREF))
        if _FONT_MIN <= v <= _FONT_MAX:
            return v
    except Exception:
        pass
    return _FONT_DEFAULT


@router.get("/reports/trade", response_class=HTMLResponse)
def trade_report_page(request: Request, user: str = Depends(require_page(PAGE_KEY))):
    _track(request, 'page_view', '/reports/trade')
    try:
        with get_database_connection() as conn:
            font_size = _saved_font(conn, user)
    except Exception as e:
        logger.warning("Trade Report: could not load saved font size: %s", e)
        font_size = _FONT_DEFAULT
    return templates.TemplateResponse("trade_report.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "font_size": font_size, "font_pref": _FONT_PREF,
        "is_admin": is_admin(user), "vehicle_view": _saved_view(),
    })


@router.get("/reports/trade/data")
def trade_report_data(request: Request, refresh: int = 0,
                      user: str = Depends(require_page_api(PAGE_KEY))):
    """Every trade sale row in the model. `refresh=1` forces a rebuild from
    Snowflake (the page's Refresh button)."""
    try:
        return _get_data(force_refresh=bool(refresh))
    except Exception as e:
        logger.error("Trade Report data failed: %s", e)
        return {"status": "error", "message": str(e)}


@router.post("/reports/trade/vehicle-view")
def trade_report_vehicle_view_save(request: Request, payload: dict,
                                   user: str = Depends(require_page_api(PAGE_KEY))):
    """Settings > View Vehicles. Admin only."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the View Vehicles columns"}
    try:
        view = _clean_view((payload or {}).get("view"))
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    try:
        kpi._write_kpi_layout_value(_VIEW_KEY, view)
    except Exception as e:
        logger.error("Trade Report View Vehicles save failed: %s", e)
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "view": view}
