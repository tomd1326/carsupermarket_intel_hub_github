"""Orders & Deliveries report page.

Replaces the SharePoint/Power Query `daily_obeya` spreadsheet. Serves the
per-vehicle deal rows from sales_performance__p once; the browser applies the
Orders/Deliveries lens and date-range filtering.

The Orders/Deliveries toggle is a single switch that flips three linked things
(matching the two Power Query variants that fed the old spreadsheet):

  Orders lens      -> filter/bucket by ORDER_ACKNOWLEDGED_DATE,
                      keep STATUS_ACKNOWLEDGED + STATUS_PAID
  Deliveries lens  -> filter/bucket by INVOICE_POSTED_DATE,
                      keep STATUS_PAID only

Every row carries both dates and both status values, so the switch is a
client-side re-filter with no server round-trip. Credited invoices are
excluded at source (invoice_credited = FALSE), matching daily_obeya__p.
"""
import datetime
import json
import logging
import time
import zoneinfo

import boto3

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from utils.snowflake_client import SnowflakeClient
from .core import BASE_URL, templates, _track, get_current_user, s3_bucketname, s3_bucket_region, _S3_ENV

logger = logging.getLogger(__name__)

router = APIRouter()

# Columns pulled from sales_performance__p, in the order the spreadsheet grid
# shows them. (sf_column, json_key) — json_key is what the browser reads.
# Both dates and the raw status come through regardless of lens so the toggle
# works client-side.
_COLUMNS = [
    ("VRM", "vrm"),
    ("CAP_MAKE_MODEL", "make_model"),
    ("DERIVATIVE", "derivative"),
    ("SUPPLIER_NAME", "supplier"),
    ("SUPPLIER_CHANNEL", "supplier_channel"),
    ("ACKNOWLEDGED_DAYS_IN_STOCK", "ack_dis"),
    ("ACKNOWLEDGED_DAYS_IN_STOCK_BAND", "dis_band"),
    ("ACKNOWLEDGED_DAYS_ADVERTISED", "dts"),
    ("PURCHASE_PRICE_INC_VAT", "purch_price"),
    ("SALE_PRICE_AFTER_DISCOUNT", "sell_price_after_disc"),
    ("PRICE_BAND", "price_band"),
    ("FINANCE_SUBSIDY", "finance_subsidy"),
    ("CHASSIS_MARGIN_AFTER_SUBSIDY", "chassis_mgn_aft_subs"),
    ("VEHICLE_COSTS", "vehicle_costs"),
    ("WRITE_DOWN", "write_down"),
    ("METAL_MARGIN", "metal_mgn"),
    ("GPU_M_COSTS", "gpu_m_costs"),
    ("GPU_M", "gpu_m"),
    ("FINANCE_COMMISSION", "finance_comm"),
    ("TOTAL_PRODUCT_MARGIN", "product_mgn"),
    ("GPU_S_COSTS", "gpu_s_costs"),
    ("GPU_S", "gpu_s"),
    ("GPU_MS", "gpu_ms"),
    ("ROI_PERCENTAGE", "roi_pct"),
    ("CAP_LIVE_CLEAN_PURCHASE", "cap_live_purchase"),
    ("CAP_LIVE_CLEAN_LATEST", "cap_live_order"),
    ("AUTOTRADER_LATEST_RETAIL_VALUATION", "at_val_order"),
    ("FUEL_TYPE_CATEGORY", "fuel"),
    ("COLOUR", "colour"),
    ("MILEAGE", "mileage"),
    ("MILEAGE_BAND", "mileage_band"),
    ("DATE_FIRST_REGISTERED", "dfr"),
    ("AGE_BAND", "age_band"),
    ("DEAL_SITE_CLEAN", "site"),
    ("SALES_TYPE", "sales_type"),
    ("DISCOUNT_INC_VAT", "discount"),
    ("PX_VALUATION_STATUS", "px_status"),
    ("DEAL_STATUS", "deal_status"),
    ("INVOICE_STATUS", "invoice_status"),
    ("ORDER_ACKNOWLEDGED_DATE", "order_date"),
    ("ORDER_ACKNOWLEDGED_WEEK", "order_week"),
    ("INVOICE_POSTED_DATE", "delivered"),
    ("ORDER_URL", "order_url"),
    ("INVOICE_URL", "invoice_url"),
]

# Hard limit on the data: 12 complete months plus the current month. The floor
# is the first day of the month 12 months before the current month, so the
# current month + the 12 prior months are fully covered. Applies to both the
# order date and the invoice-posted date (see the OR filter in _build_data).
_FETCH_MONTHS_BACK = 12


def _fetch_start() -> datetime.date:
    today = datetime.date.today()
    first = today.replace(day=1)
    for _ in range(_FETCH_MONTHS_BACK):
        first = (first - datetime.timedelta(days=1)).replace(day=1)
    return first


# ── Caching ───────────────────────────────────────────────────────────────
# The rows barely change within a day and the page fetches everything once,
# then slices client-side — so serve a cached payload rather than hitting
# Snowflake on every load. Mirrors the KPI SharePoint cache pattern:
#   memory cache -> S3 cache -> Snowflake build -> stale S3 fallback.
# sales_performance__p is a `daily`-tagged dbt model (rebuilt overnight), so a
# payload is fresh if it was built after the most recent 06:00 UK boundary.
# A manual "Refresh" on the page passes refresh=1 to force a rebuild.
#
# Version the S3 key: bump _CACHE_VERSION whenever _COLUMNS changes, otherwise
# an old cached payload (missing the new columns) is served until it expires.
_CACHE_VERSION = 2
_S3_KEY = (f"applications/pricing/uk-vehicle-list-evaluator/shared/cache/"
           f"orders_deliveries_v{_CACHE_VERSION}.json")

_mem_cache: dict | None = None
_mem_cache_at: float = 0.0


def _cache_is_fresh(cached_at: float) -> bool:
    uk = zoneinfo.ZoneInfo("Europe/London")
    now_uk = datetime.datetime.now(uk)
    boundary = now_uk.replace(hour=6, minute=0, second=0, microsecond=0)
    if boundary > now_uk:
        boundary -= datetime.timedelta(days=1)
    return cached_at >= boundary.timestamp()


def _s3():
    # aa-raw-sources-uk is always eu-west-1 regardless of Lambda region
    return boto3.client("s3", region_name=s3_bucket_region)


def _s3_load() -> tuple[float, dict | None]:
    try:
        obj = _s3().get_object(Bucket=s3_bucketname, Key=_S3_KEY)
        payload = json.loads(obj["Body"].read())
        return payload.get("cached_at", 0.0), payload.get("data")
    except Exception as e:
        logger.warning("Orders & Deliveries S3 cache read failed: %s", e)
        return 0.0, None


def _s3_save(cached_at: float, data: dict) -> None:
    try:
        _s3().put_object(
            Bucket=s3_bucketname, Key=_S3_KEY,
            Body=json.dumps({"cached_at": cached_at, "data": data}).encode(),
            ContentType="application/json",
        )
    except Exception as e:
        logger.warning("Orders & Deliveries S3 cache write failed: %s", e)


def _build_data() -> dict:
    """Query sales_performance__p and shape the row payload. Expensive — only
    called on a cache miss or a manual refresh."""
    select_cols = ", ".join(sf for sf, _ in _COLUMNS)
    keys = [key for _, key in _COLUMNS]
    fetch_start = _fetch_start()

    # A deal appears under the Orders lens by its ack date and under the
    # Deliveries lens by its posted date — either date being in-window is
    # enough to include the row, so OR the two floors.
    with SnowflakeClient() as sf:
        rows, cols = sf.execute(f"""
            SELECT {select_cols}
            FROM PROD_CARSUPERMARKET_DB.product_sales_performance.sales_performance
            WHERE invoice_credited = FALSE
              AND (
                    ORDER_ACKNOWLEDGED_DATE >= %s
                    OR INVOICE_POSTED_DATE   >= %s
              )
            ORDER BY ORDER_ACKNOWLEDGED_DATE DESC
        """, [str(fetch_start), str(fetch_start)])

    idx = {c.upper(): i for i, c in enumerate(cols)}

    def _val(r, sf_col):
        i = idx.get(sf_col.upper())
        if i is None:
            return None
        v = r[i]
        if isinstance(v, datetime.datetime):
            return v.date().isoformat()
        if isinstance(v, datetime.date):
            return v.isoformat()
        return v

    def _num(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _ratio(n, d):
        n, d = _num(n), _num(d)
        if n is None or not d:
            return None
        return round(n / d, 4)

    out = []
    for r in rows:
        row = {key: _val(r, sf) for sf, key in _COLUMNS}
        # Derived columns (matched to the Power Query)
        row["purchase_cap_pct"] = _ratio(row["purch_price"], row["cap_live_purchase"])
        row["sell_pct_at"] = _ratio(row["sell_price_after_disc"], row["at_val_order"])
        # year_month from the posted date (Deliveries grouping);
        # year_week comes straight from ORDER_ACKNOWLEDGED_WEEK
        row["year_month"] = (row["delivered"] or "")[:7] or None
        out.append(row)

    return {
        "status": "ok",
        "today": datetime.date.today().isoformat(),
        "fetch_start": fetch_start.isoformat(),
        "columns": keys + ["purchase_cap_pct", "sell_pct_at", "year_month"],
        "rows": out,
    }


def _get_data(force_refresh: bool = False) -> dict:
    """memory cache → S3 cache → Snowflake build → stale S3 fallback.

    `today` is always stamped fresh at serve time, never taken from the cached
    payload — the frontend's default "current week" selection depends on it, so
    a cache built yesterday must not report yesterday as today."""
    global _mem_cache, _mem_cache_at
    today = datetime.date.today().isoformat()

    if not force_refresh:
        if _mem_cache is not None and _cache_is_fresh(_mem_cache_at):
            logger.info("Orders & Deliveries: serving from memory cache")
            return {**_mem_cache, "cached_at": _mem_cache_at, "today": today}
        cached_at, cached = _s3_load()
        if cached and _cache_is_fresh(cached_at):
            logger.info("Orders & Deliveries: serving from S3 cache")
            _mem_cache, _mem_cache_at = cached, cached_at
            return {**cached, "cached_at": cached_at, "today": today}

    logger.info("Orders & Deliveries: building from Snowflake (force_refresh=%s)", force_refresh)
    try:
        result = _build_data()
    except Exception as e:
        logger.error("Orders & Deliveries build failed: %s", e)
        # Fall back to any S3 payload we have, even if stale
        cached_at, cached = _s3_load()
        if cached:
            logger.warning("Orders & Deliveries: build failed, serving stale S3 cache")
            return {**cached, "cached_at": cached_at, "today": today, "stale": True}
        return {"status": "error", "message": str(e)}

    now = time.time()
    _mem_cache, _mem_cache_at = result, now
    _s3_save(now, result)
    return {**result, "cached_at": now, "today": today}


def warm_cache() -> None:
    """Refresh the Orders & Deliveries cache if stale. Safe to call from the
    /home cache warmer."""
    _get_data()
    logger.info("Home cache warm (Orders & Deliveries) complete")


@router.get("/reports/orders-deliveries", response_class=HTMLResponse)
def orders_deliveries_page(request: Request, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    _track(request, 'page_view', '/reports/orders-deliveries')
    return templates.TemplateResponse("orders_deliveries.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
    })


@router.get("/reports/orders-deliveries/data")
def orders_deliveries_data(request: Request, refresh: int = 0,
                           user: str = Depends(get_current_user)):
    """Cached deal rows. `refresh=1` forces a rebuild from Snowflake (the
    page's manual Refresh button)."""
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    try:
        return _get_data(force_refresh=bool(refresh))
    except Exception as e:
        logger.error(f"Orders & Deliveries data failed: {e}")
        return {"status": "error", "message": str(e)}
