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
import requests

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse

from utils.db_utils import get_database_connection, get_user_preference
from utils.snowflake_client import SnowflakeClient
from .core import BASE_URL, templates, _track, get_current_user, s3_bucketname, s3_bucket_region, _S3_ENV
from .kpi import _get_gemini_api_key

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
    # Selling price before the vehicle discount, including the admin fee
    # actually charged on the deal (the fee is mandatory but can be waived, so
    # a flat amount would overstate a waived one). Built in the model beside
    # SALE_PRICE_AFTER_DISCOUNT_INCL_ADMIN_FEE. The discount is its own column.
    ("SALE_PRICE_BEFORE_DISCOUNT_INCL_ADMIN_FEE", "sell_price_incl_fee"),
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
    ("CAP_LIVE_CLEAN_PURCHASE", "cap_live_purchase"),
    ("CAP_LIVE_CLEAN_LATEST", "cap_live_order"),
    ("AUTOTRADER_LATEST_RETAIL_VALUATION", "at_val_order"),
    ("AUTOTRADER_LATEST_RETAIL_RATING", "at_rating"),
    ("FUEL_TYPE_CATEGORY", "fuel"),
    ("COLOUR", "colour"),
    ("MILEAGE", "mileage"),
    ("MILEAGE_BAND", "mileage_band"),
    ("DATE_FIRST_REGISTERED", "dfr"),
    ("AGE_BAND", "age_band"),
    ("DEAL_SITE_CLEAN", "site"),
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
_CACHE_VERSION = 10
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
        row["sell_pct_at"] = _ratio(row["sell_price_incl_fee"], row["at_val_order"])
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
def orders_deliveries_page(request: Request, user: str = Depends(get_current_user)):
    _track(request, 'page_view', '/reports/orders-deliveries')
    try:
        with get_database_connection() as conn:
            font_summary = _saved_font(conn, user, 'orders_deliveries_font_summary')
            font_vehicle = _saved_font(conn, user, 'orders_deliveries_font_vehicle',
                                       _FONT_DEFAULT_VEHICLE)
    except Exception as e:
        logger.warning("Orders & Deliveries: could not load saved font sizes: %s", e)
        font_summary = _FONT_DEFAULT
        font_vehicle = _FONT_DEFAULT_VEHICLE
    return templates.TemplateResponse("orders_deliveries.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "font_summary": font_summary, "font_vehicle": font_vehicle,
    })


@router.get("/reports/orders-deliveries/data")
def orders_deliveries_data(request: Request, refresh: int = 0,
                           user: str = Depends(get_current_user)):
    """Cached deal rows. `refresh=1` forces a rebuild from Snowflake (the
    page's manual Refresh button)."""
    try:
        return _get_data(force_refresh=bool(refresh))
    except Exception as e:
        logger.error(f"Orders & Deliveries data failed: {e}")
        return {"status": "error", "message": str(e)}


# ── Ask AI ──────────────────────────────────────────────────────────────────
# The browser does the slicing and sends a compact table of per-slice figures,
# not the vehicle rows. Gemini is being asked to read a ranked breakdown and
# explain it, never to aggregate raw data itself — it is unreliable at
# arithmetic over thousands of rows and reliable at narrating a table that
# already has the totals worked out.
_AI_SYSTEM_PROMPT = """You are a motor retail analyst for a UK used-car supermarket.

You are given per-slice figures for a set of deal metrics, already aggregated.
Each row gives a slice (one dimension value, or a pair of them), the number of
vehicles, the mean per vehicle for each period, the change against the previous
period, and `impact` = change x volume, which is that slice's contribution to
the overall movement in currency.

Answer as a buyer would want it: what went wrong, in what part of the business,
on what kind of vehicle, and where to go and look. Structure it as:

1. WHAT MOVED — one line. The metric, the per-unit change, the period pair.
2. WHY — the causal chain through the component metrics.
   For GPU-M: chassis margin after subsidy, vehicle costs and write-downs. Say
   which moved and by how much per unit. "GPU-M fell because chassis margin fell
   £180/unit, while costs were flat" is the answer; "GPU-M fell because GPU-M
   fell" is not.
   For GPU-S: finance penetration, finance commission per financed deal, and
   total product margin. Penetration and commission rate are different problems
   with different fixes — always say which one moved. A GPU-S fall with flat
   commission per financed deal is a penetration problem; a fall with flat
   penetration is a rate or product-mix problem.
3. WHERE — the supplier channel, then the kind of vehicle within it (fuel, price
   band, age, mileage). Name the specific combination, not each dimension
   separately: "B2B2C diesels over 60k miles", not "B2B2C was down and diesel was
   down".
4. GO AND LOOK — finish with the slicer selection that isolates the problem, as
   a short instruction: "Select Supplier Channel = B2B2C, Fuel = Diesel to see
   these 40 vehicles." Pick the narrowest slice that still holds enough vehicles
   to matter.

On figures:
- Lead with per-unit changes — that is how the business thinks about GPU. Use
  the total impact only to establish that a slice is material enough to mention,
  or when the reader needs the cash number. Do not open with total impact and do
  not quote it for every slice. Quote it at most twice in an answer; a slice's
  per-unit change and its vehicle count together already say how much it matters.
- Do not repeat the same slice list under two headings. If WHY already named the
  slices, WHERE should add what those vehicles have in common (age, mileage,
  price band) rather than listing them again.
- A per-unit swing on a handful of vehicles is not an explanation. Check the
  volume before calling something a driver, and say when a finding rests on a
  small number of vehicles.
- NEVER present a change in purchase price or sell price as a cause. Those move
  whenever the profile of vehicles sold changes — a week with pricier cars has a
  higher purchase price and a higher sell price, and neither says anything about
  performance. Margin is what matters: chassis margin and GPU-M on the metal
  side, finance and product margin on the back end. Use the price columns only
  as evidence that the mix changed ("purchase price up £3,900/unit, so these are
  materially different cars"), never as the explanation itself.
- Mix matters: a metric can fall because the mix shifted toward lower-margin
  vehicles even when no slice got worse. Check for that and say so when it is
  what happened, because the fix is a different one. If a slice's margin fell
  while its purchase and sell prices both moved a lot, suspect mix first and say
  so rather than calling it a margin problem.
- If the figures do not support a clear cause, say so plainly rather than
  picking the largest number and calling it the reason.

Money is GBP. Metric names: GPU-M is metal gross profit per unit, GPU-S is
service/product gross profit per unit, GPU-MS is the two combined. Chassis
margin after subsidy is the vehicle's own margin before costs.

Be concise — aim for under 250 words unless the question asks for more. No
preamble, no restating the question, no summary paragraph at the end. Prefer
short bullets over long sentences.

Follow-up questions continue this conversation. The data table above is the
only data you get for the whole thread, so answer follow-ups from it and from
what you have already said. If a follow-up asks for a figure the table does not
contain — a dimension that was not broken out, or a slice listed as omitted —
say so and name what would need to be selected on the page to get it, rather
than estimating."""

# Turns kept when replaying a conversation: the first (which carries the data
# table) plus the most recent ones. Each turn adds to the prompt, and the free
# tier caps input tokens per minute, so an unbounded thread would eventually
# fail on quota mid-conversation.
_AI_MAX_HISTORY_TURNS = 11


@router.post("/reports/orders-deliveries/ask-ai")
def orders_deliveries_ask_ai(request: Request, payload: dict,
                             user: str = Depends(get_current_user)):
    """Answer a free-text question about the pre-aggregated slice table the
    browser sends up. Restricted while the shared Gemini key is in use.

    `history` (optional) is the conversation so far, as
    [{"role": "user"|"model", "text": ...}, ...]. When present the question is
    a follow-up: the turns are replayed and `context` may be omitted, since the
    data table is already in the first user turn."""
    if user != 'tdrayson':
        raise HTTPException(status_code=403)

    question = (payload or {}).get("question")
    context = (payload or {}).get("context")
    history = (payload or {}).get("history") or []
    if not isinstance(question, str) or not question.strip():
        return {"status": "error", "message": "Missing question"}
    if not isinstance(history, list):
        return {"status": "error", "message": "history must be a list of turns"}
    # Only a first turn has to carry the data — a follow-up rides on history.
    if not history and (not isinstance(context, str) or not context.strip()):
        return {"status": "error", "message": "Missing data context"}
    # A runaway thread would quietly grow the prompt on every turn until it
    # trips the token quota, so cap it here rather than trusting the browser.
    if len(history) > _AI_MAX_HISTORY_TURNS:
        history = history[:1] + history[-(_AI_MAX_HISTORY_TURNS - 1):]

    api_key = _get_gemini_api_key()
    if not api_key:
        return {"status": "error", "message": "Gemini API key is not configured"}

    # Follow-ups replay the conversation as separate turns rather than one
    # concatenated blob, so the model can tell its own previous answers from
    # the new question. The data table is sent ONCE, on the first turn — the
    # browser omits it on follow-ups it judges answerable from the exchange so
    # far, which is what keeps a multi-question thread inside the token quota.
    contents = []
    first = f"{_AI_SYSTEM_PROMPT}\n\n=== DATA ===\n{context}\n\n=== QUESTION ===\n{question.strip()}"
    if history:
        # history[0] carries the data table; later turns are question/answer text.
        for i, turn in enumerate(history):
            role = turn.get("role")
            text = turn.get("text")
            if role not in ("user", "model") or not isinstance(text, str) or not text.strip():
                continue
            contents.append({"role": role, "parts": [{"text": text}]})
        contents.append({"role": "user", "parts": [{"text": question.strip()}]})
    else:
        contents.append({"role": "user", "parts": [{"text": first}]})

    prompt_chars = sum(len(p["parts"][0]["text"]) for p in contents)
    logger.info("Orders & Deliveries Ask AI: turns=%s prompt_chars=%s follow_up=%s",
                len(contents), prompt_chars, bool(history))

    try:
        resp = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            params={"key": api_key},
            headers={"Content-Type": "application/json"},
            json={
                # gemini-2.5-flash is a thinking model: its reasoning tokens are
                # charged against maxOutputTokens BEFORE any visible text, so a
                # 4096 cap truncated answers mid-sentence. thinkingBudget caps
                # the reasoning separately, leaving the rest for the answer.
                "contents": contents,
                "generationConfig": {
                    "maxOutputTokens": 16384,
                    "temperature": 0.2,
                    "thinkingConfig": {"thinkingBudget": 8192},
                },
            },
            timeout=60,
        )
        try:
            data = resp.json()
        except ValueError:
            data = {}

        if not resp.ok:
            error = data.get("error") if isinstance(data, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            return {"status": "error", "message": message or resp.reason or "Gemini request failed"}

        candidate = data["candidates"][0]
        usage = data.get("usageMetadata", {})
        finish_reason = candidate.get("finishReason", "unknown")
        logger.info(
            "Orders & Deliveries Ask AI: finishReason=%s tokens_in=%s tokens_out=%s thoughts=%s",
            finish_reason,
            usage.get("promptTokenCount"),
            usage.get("candidatesTokenCount"),
            usage.get("thoughtsTokenCount"),
        )
        parts = candidate["content"]["parts"]
        answer = "".join(part.get("text", "") for part in parts).strip()
        if not answer:
            # MAX_TOKENS with no text means the whole budget went on reasoning.
            if finish_reason == "MAX_TOKENS":
                return {"status": "error",
                        "message": "The model used its whole output budget on reasoning "
                                   "and returned no answer. Try a more specific question."}
            return {"status": "error", "message": "Gemini returned an empty answer"}
        # On a first turn, hand back the exact text the prompt was built from.
        # The browser stores it as turn 0 so a later follow-up replays the data
        # table verbatim instead of re-deriving (and possibly re-wording) it.
        return {"status": "ok", "answer": answer,
                # The browser marks a truncated answer rather than showing it as
                # if it were complete — a cut-off analysis reads as a finished
                # one otherwise.
                "truncated": finish_reason == "MAX_TOKENS",
                **({} if history else {"first_turn_text": first}),
                "tokens_in": usage.get("promptTokenCount"),
                "tokens_out": usage.get("candidatesTokenCount")}
    except requests.exceptions.RequestException as e:
        logger.error("Orders & Deliveries Ask AI request failed: %s", e)
        return {"status": "error", "message": str(e)}
    except (KeyError, IndexError, TypeError) as e:
        logger.error("Orders & Deliveries Ask AI response parse failed: %s", e)
        return {"status": "error", "message": "Could not read Gemini response"}
