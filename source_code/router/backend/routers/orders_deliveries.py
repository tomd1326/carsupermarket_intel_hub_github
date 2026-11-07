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
from fastapi.responses import HTMLResponse

from utils.db_utils import get_database_connection, get_user_preference, get_settings
from utils.snowflake_client import SnowflakeClient
from .core import (BASE_URL, templates, _track, s3_bucketname,
                   s3_bucket_region, require_page, require_page_api, is_admin)
from utils import od_bands, od_shading
from utils.cortex import cortex_complete
from utils import ai_knowledge

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
    ("CAP_ID", "cap_id"),
    ("SUPPLIER_NAME", "supplier"),
    ("SUPPLIER_CHANNEL", "supplier_channel"),
    ("ACKNOWLEDGED_DAYS_IN_STOCK", "ack_dis"),
    ("ACKNOWLEDGED_DAYS_IN_STOCK_BAND", "dis_band"),
    ("ACKNOWLEDGED_DAYS_ADVERTISED", "dts"),
    ("PURCHASE_PRICE_INC_VAT", "purch_price"),
    # Selling price before the vehicle discount, including the admin fee
    # actually charged on the deal (the fee is mandatory but can be waived, so
    # a flat amount would overstate a waived one). Built in the model beside
    # SALE_PRICE_AFTER_DISCOUNT_INC_ADMIN_FEE. The discount is its own column.
    ("SALE_PRICE_BEFORE_DISCOUNT_INC_ADMIN_FEE", "sell_price_incl_fee"),
    # Same price with the vehicle discount taken off. Not shown as a column —
    # it exists so PTM % is measured on what the customer actually paid, and so
    # it matches daily_summary__p, which builds its own price position from
    # this column. Sell stays before discount with Discount beside it.
    ("SALE_PRICE_AFTER_DISCOUNT_INC_ADMIN_FEE", "sell_price_after_disc_incl_fee"),
    # Raw prices the price band is worked out from (see utils/od_bands.py). The
    # band itself is no longer read from the model, so the admin can edit it.
    ("VEHICLE_SELLING_PRICE_INC_VAT", "vehicle_selling_price"),
    ("SALE_PRICE_AFTER_DISCOUNT", "sale_price_after_discount"),
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
    ("CAP_LIVE_CLEAN_PURCHASE", "cap_live_purchase"),
    ("CAP_LIVE_CLEAN_LATEST", "cap_live_order"),
    ("AUTOTRADER_LATEST_RETAIL_VALUATION", "at_val_order"),
    ("AUTOTRADER_LATEST_RETAIL_RATING", "at_rating"),
    ("FUEL_TYPE_CATEGORY", "fuel"),
    ("COLOUR", "colour"),
    ("MILEAGE", "mileage"),
    ("DATE_FIRST_REGISTERED", "dfr"),
    ("DEAL_SITE_CLEAN", "site"),
    # Not displayed: the key of the vehicle's notes (routers/vehicle_comments.py).
    ("AUTOVIEW_STOCK_ID", "autoview_stock_id"),
    ("SALES_TYPE", "sales_type"),
    # Rate/Cash vs 0% split, bucketed in the model so the browser does not
    # redo it on every slicer click.
    ("SALES_TYPE_BUCKET", "sales_type_bucket"),
    ("DISCOUNT_INC_VAT", "discount"),
    # Not displayed — drives the Orders/Deliveries lens filter (see LENS in the
    # template), so it must stay in the payload even though no column shows it.
    ("DEAL_STATUS", "deal_status"),
    ("INVOICE_STATUS", "invoice_status"),
    ("ORDER_ACKNOWLEDGED_DATE", "order_date"),
    # Period keys for the Order/Delivery Week/Month slicer, one per lens and
    # granularity. Built in the model so the browser does not recompute ISO
    # weeks for every row on every render. Weeks are already 'yyyy-Www'; the
    # month columns are DATEs, so they are cast to 'yyyy-MM' to match.
    ("ORDER_ACKNOWLEDGED_WEEK", "order_week"),
    ("TO_CHAR(ORDER_ACKNOWLEDGED_MONTH, 'YYYY-MM') AS ORDER_MONTH", "order_month"),
    ("INVOICE_POSTED_WEEK", "delivered_week"),
    ("TO_CHAR(INVOICE_POSTED_MONTH, 'YYYY-MM') AS DELIVERED_MONTH", "delivered_month"),
    ("INVOICE_POSTED_DATE", "delivered"),
    ("VEHICLE_URL", "vehicle_url"),
    ("ORDER_URL", "order_url"),
    ("INVOICE_URL", "invoice_url"),
    ("AUTOTRADER_PORTAL_LINK", "at_portal_url"),
]

# Hard limit on the data: 3 complete calendar months, plus the current
# (in-progress) month. The floor is the 1st of the month 3 months before the
# current one, so every day of those 3 months is covered in full — no partial
# month or week ever enters the fetch. Applies to both the order date and the
# invoice-posted date (see the OR filter in _build_data).
#
# Calendar months, not a week count: a fixed number of weeks can land the
# floor mid-month depending on where in the month "today" falls, which is
# exactly what caused a real bug — the Period slicer offered an early week
# (W18 2026, 484 real orders) as if it had data, when the fetch floor only
# actually captured 1 of those 484 rows (via the OTHER lens's date, pulled in
# by the OR clause in the SQL below). Anchoring on calendar months makes "is
# this month complete" a fact the server guarantees, not something the
# frontend has to work out.
#
# Was 13 months, then 13 weeks — the original 13-month window made the JSON
# payload big enough to hit the Lambda/API Gateway 6MB response limit (a real
# PROD 500 with no application-level exception, since the failure happens
# after the handler returns, during response marshalling — nothing to catch
# or log).
_FETCH_MONTHS_BACK = 3


def _fetch_start() -> datetime.date:
    today = datetime.date.today()
    first_of_this_month = today.replace(day=1)
    y, m = first_of_this_month.year, first_of_this_month.month - _FETCH_MONTHS_BACK
    while m <= 0:
        m += 12
        y -= 1
    return datetime.date(y, m, 1)


# ── Caching ───────────────────────────────────────────────────────────────
# The rows barely change within a run and the page fetches everything once,
# then slices client-side — so serve a cached payload rather than hitting
# Snowflake on every load. Mirrors the KPI SharePoint cache pattern:
#   memory cache -> S3 cache -> Snowflake build -> stale S3 fallback.
# dbt rebuilds sales_performance__p five times a day, cron "15 8,10,12,14,16"
# Europe/London, and finishes by :45 past. A payload is fresh if it was built
# after the most recent HH:45 boundary that has already passed, so the page
# picks up each day's runs automatically without a fixed single-refresh time.
# A manual "Refresh" on the page passes refresh=1 to force a rebuild.
#
# Version the S3 key: bump _CACHE_VERSION whenever _COLUMNS changes, otherwise
# an old cached payload (missing the new columns) is served until it expires.
_CACHE_VERSION = 14
_S3_KEY = (f"applications/pricing/uk-vehicle-list-evaluator/shared/cache/"
           f"orders_deliveries_v{_CACHE_VERSION}.json")

_mem_cache: dict | None = None
_mem_cache_at: float = 0.0


# dbt run start hours (Europe/London); each run finishes by :45 past.
_DBT_RUN_HOURS = (8, 10, 12, 14, 16)


def _latest_dbt_boundary(now_uk: datetime.datetime) -> datetime.datetime:
    """Most recent HH:45 UK time by which a dbt run has finished, at or
    before now_uk. Falls back to the previous day's last run if none of
    today's runs have finished yet."""
    for hour in reversed(_DBT_RUN_HOURS):
        boundary = now_uk.replace(hour=hour, minute=45, second=0, microsecond=0)
        if boundary <= now_uk:
            return boundary
    yesterday = now_uk - datetime.timedelta(days=1)
    return yesterday.replace(hour=_DBT_RUN_HOURS[-1], minute=45, second=0, microsecond=0)


def _cache_is_fresh(cached_at: float) -> bool:
    uk = zoneinfo.ZoneInfo("Europe/London")
    now_uk = datetime.datetime.now(uk)
    boundary = _latest_dbt_boundary(now_uk)
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
        # Entries in _COLUMNS may be a bare column ("VRM") or a full expression
        # with an alias ("TO_CHAR(...) AS ORDER_MONTH"). Snowflake returns the
        # ALIAS as the column name, so look up the alias, not the expression —
        # otherwise the lookup misses and every row silently gets None. That is
        # what emptied the whole page under "By Month": order_month /
        # delivered_month came back null for every row, so no row matched the
        # period selection and every other slicer lost its values too.
        name = sf_col.upper()
        if " AS " in name:
            name = name.rsplit(" AS ", 1)[1].strip()
        i = idx.get(name)
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
        row["sell_pct_at"] = _ratio(row["sell_price_after_disc_incl_fee"], row["at_val_order"])
        out.append(row)

    return {
        "status": "ok",
        "today": datetime.date.today().isoformat(),
        "fetch_start": fetch_start.isoformat(),
        "columns": keys + ["purchase_cap_pct", "sell_pct_at"],
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


_FONT_DEFAULT = 13          # page font (everything except the vehicle grid)
_FONT_DEFAULT_VEHICLE = 12  # the vehicle view's grid, which has its own resizer
_FONT_MIN, _FONT_MAX = 8, 20


def _saved_font(conn, user: str, pref_key: str, default: int = _FONT_DEFAULT) -> int:
    """Mirrors the JS loadFont() clamp/fallback so the server-rendered label
    always matches what loadFont() would set — otherwise the label jumps on
    load. Kept in sync manually with FONT_MIN/FONT_MAX/FONT_DEFAULT in the JS."""
    try:
        v = get_user_preference(conn, user, pref_key)
        v = int(v)
        if _FONT_MIN <= v <= _FONT_MAX:
            return v
    except Exception:
        pass
    return default


@router.get("/reports/orders-deliveries", response_class=HTMLResponse)
def orders_deliveries_page(request: Request, user: str = Depends(require_page('orders_deliveries'))):
    _track(request, 'page_view', '/reports/orders-deliveries')
    ai_enabled = False
    try:
        with get_database_connection() as conn:
            font_summary = _saved_font(conn, user, 'orders_deliveries_font_summary')
            font_vehicle = _saved_font(conn, user, 'orders_deliveries_font_vehicle',
                                       _FONT_DEFAULT_VEHICLE)
            ai_enabled = bool(get_settings(conn).get('orders_deliveries_ai'))
    except Exception as e:
        logger.warning("Orders & Deliveries: could not load saved font sizes: %s", e)
        font_summary = _FONT_DEFAULT
        font_vehicle = _FONT_DEFAULT_VEHICLE
    # Rendered into the page so the first paint uses the saved highlighting.
    shading = od_shading.load()
    return templates.TemplateResponse("orders_deliveries.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "font_summary": font_summary, "font_vehicle": font_vehicle,
        "ai_enabled": ai_enabled, "shading": shading, "is_admin": is_admin(user),
    })


@router.get("/reports/orders-deliveries/data")
def orders_deliveries_data(request: Request, refresh: int = 0,
                           user: str = Depends(require_page_api('orders_deliveries'))):
    """Cached deal rows. `refresh=1` forces a rebuild from Snowflake (the
    page's manual Refresh button)."""
    try:
        data = _get_data(force_refresh=bool(refresh))
        if data.get("status") == "ok":
            # Bands are worked out at serve time from the saved limits, so an
            # edit in Settings shows on the next page load with no cache rebuild.
            data = {**data, "band_orders": od_bands.apply(data["rows"], od_bands.load_limits())}
        return data
    except Exception as e:
        logger.error(f"Orders & Deliveries data failed: {e}")
        return {"status": "error", "message": str(e)}


# ── Page Settings popup (admin only) ────────────────────────────────────────
# Bands tab: utils/od_bands.py. Highlighting tab: utils/od_shading.py. Both are
# saved in kpi_layout, so they apply to every user and every environment.
@router.get("/reports/orders-deliveries/settings")
def orders_deliveries_settings(request: Request, user: str = Depends(require_page_api('orders_deliveries'))):
    limits = od_bands.load_limits()
    return {"status": "ok",
            "bands": {"limits": limits, "defaults": od_bands.DEFAULT_LIMITS,
                      "titles": od_bands.BAND_TITLES,
                      "labels": {k: od_bands.labels(k, v) for k, v in limits.items()}},
            "shading": {"saved": od_shading.load(), "defaults": od_shading.DEFAULTS}}


@router.post("/reports/orders-deliveries/settings")
def orders_deliveries_settings_save(request: Request, payload: dict,
                                    user: str = Depends(require_page_api('orders_deliveries'))):
    """Bands and highlighting, both checked before either is written."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the Orders & Deliveries settings"}
    payload = payload or {}
    bands, shading = payload.get("bands"), payload.get("shading")
    err = od_bands.validate(bands) or od_shading.validate(shading)
    if err:
        return {"status": "error", "message": err}
    try:
        from routers import kpi
        kpi._write_kpi_layout_values({od_bands.KPI_KEY: od_bands.clean(bands),
                                      od_shading.KPI_KEY: od_shading.clean(shading)})
    except Exception as e:
        logger.error(f"Orders & Deliveries settings save failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "shading": od_shading.clean(shading)}


@router.post("/reports/orders-deliveries/bands/preview")
def orders_deliveries_bands_preview(request: Request, payload: dict,
                                    user: str = Depends(require_page_api('orders_deliveries'))):
    """The labels a set of limits would produce, without saving."""
    err = od_bands.validate(payload)
    if err:
        return {"status": "error", "message": err}
    limits = od_bands.clean(payload)
    return {"status": "ok", "labels": {k: od_bands.labels(k, v) for k, v in limits.items()}}


# ── Ask AI ──────────────────────────────────────────────────────────────────
# The browser does the slicing and sends a compact table of per-slice figures,
# not the vehicle rows. The model is being asked to read a ranked breakdown and
# explain it, never to aggregate raw data itself — it is unreliable at
# arithmetic over thousands of rows and reliable at narrating a table that
# already has the totals worked out.
#
# The instructions, answer length, temperature, history length and knowledge
# are set in Settings > AI (utils/ai_knowledge.py holds the defaults).
@router.post("/reports/orders-deliveries/ask-ai")
def orders_deliveries_ask_ai(request: Request, payload: dict,
                             user: str = Depends(require_page_api('orders_deliveries'))):
    """Answer a free-text question about the pre-aggregated slice table the
    browser sends up, via Snowflake Cortex.

    `history` (optional) is the conversation so far, as
    [{"role": "user"|"model", "text": ...}, ...]. When present the question is
    a follow-up: the turns are replayed and `context` may be omitted, since the
    data table is already in the first user turn."""
    if not is_admin(user):
        raise HTTPException(status_code=403)
    with get_database_connection() as conn:
        if not get_settings(conn).get('orders_deliveries_ai'):
            return {"status": "error", "message": "Ask AI is turned off in Settings > AI"}

    question =(payload or {}).get("question")
    context = (payload or {}).get("context")
    history = (payload or {}).get("history") or []
    if not isinstance(question, str) or not question.strip():
        return {"status": "error", "message": "Missing question"}
    if not isinstance(history, list):
        return {"status": "error", "message": "history must be a list of turns"}
    # Only a first turn has to carry the data — a follow-up rides on history.
    if not history and (not isinstance(context, str) or not context.strip()):
        return {"status": "error", "message": "Missing data context"}
    ai, _, system = ai_knowledge.page_prompt('orders_deliveries')
    # Turns kept when replaying a conversation: the first (which carries the
    # data table) plus the most recent ones, capped here rather than trusting
    # the browser so a long thread cannot grow without limit.
    max_turns = ai["history_turns"]
    if len(history) > max_turns:
        history = history[:1] + history[-(max_turns - 1):] if max_turns > 1 else history[:1]

    # Follow-ups replay the conversation as separate turns rather than one
    # concatenated blob, so the model can tell its own previous answers from
    # the new question. The data table is sent ONCE, on the first turn — the
    # browser omits it on follow-ups it judges answerable from the exchange so
    # far, which is what keeps a multi-question thread short.
    first = f"=== DATA ===\n{context}\n\n=== QUESTION ===\n{question.strip()}"
    messages = [{"role": "system", "content": system}]
    if history:
        # history[0] carries the data table; later turns are question/answer text.
        for turn in history:
            role = turn.get("role")
            text = turn.get("text")
            if role not in ("user", "model") or not isinstance(text, str) or not text.strip():
                continue
            messages.append({"role": "assistant" if role == "model" else "user", "content": text})
        messages.append({"role": "user", "content": question.strip()})
    else:
        messages.append({"role": "user", "content": first})

    prompt_chars = sum(len(m["content"]) for m in messages)
    logger.info("Orders & Deliveries Ask AI: turns=%s prompt_chars=%s follow_up=%s",
                len(messages), prompt_chars, bool(history))

    try:
        data = cortex_complete(messages, max_tokens=ai["max_tokens"], temperature=ai["temperature"])
        usage = data.get("usage", {})
        answer = data["choices"][0]["messages"].strip()
        if not answer:
            return {"status": "error", "message": "Cortex returned an empty answer"}
        # On a first turn, hand back the exact text the prompt was built from.
        # The browser stores it as turn 0 so a later follow-up replays the data
        # table verbatim instead of re-deriving (and possibly re-wording) it.
        return {"status": "ok", "answer": answer,
                "truncated": False,
                **({} if history else {"first_turn_text": first}),
                "tokens_in": usage.get("prompt_tokens"),
                "tokens_out": usage.get("completion_tokens")}
    except Exception as e:
        logger.error("Orders & Deliveries Ask AI request failed: %s", e)
        return {"status": "error", "message": str(e)}
