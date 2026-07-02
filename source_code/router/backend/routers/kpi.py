"""KPI dashboard: Snowflake daily summary, SharePoint sessions, Gemini AI summary."""

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
import os
import json
import time
import requests
import logging
import datetime


from utils.db_utils import (get_database_connection, get_prod_pricer_connection, get_settings)
from utils.snowflake_client import SnowflakeClient
from utils.sharepoint_client import SharePointClient
from utils.secrets_manager_client import get_secret

from .core import (BASE_URL, templates, _track, get_current_user)

logger = logging.getLogger(__name__)

router = APIRouter()


def _week_monday_of(d: datetime.date) -> datetime.date:
    return d - datetime.timedelta(days=d.weekday())


def _prev_iso_week_monday(monday: datetime.date) -> datetime.date:
    """Monday of the same ISO week NUMBER in the previous ISO year.
    This is the N-1 week definition for all weekly charts — equivalent week
    number, not the same 7 dates shifted by 52 weeks."""
    iso_year, iso_week, _ = monday.isocalendar()
    try:
        return datetime.date.fromisocalendar(iso_year - 1, iso_week, 1)
    except ValueError:
        # Week 53 with no equivalent in the previous ISO year — use its last week
        return datetime.date.fromisocalendar(iso_year - 1, 52, 1)


@router.get("/kpi", response_class=HTMLResponse)
def kpi_page(request: Request, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    _track(request, 'page_view', '/kpi')
    with get_database_connection() as conn:
        settings = get_settings(conn, user=user)
    return templates.TemplateResponse("kpi.html", {"request": request, "user": user, "BASE_URL": BASE_URL, "kpi_ai_summary": settings.get("kpi_ai_summary", False)})


@router.get("/kpi/data")
def kpi_data(request: Request, user: str = Depends(get_current_user)):
    """Return daily_summary KPI data: daily window + 13 months of monthly totals."""
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    try:
        import calendar
        today = datetime.date.today()

        # ── Date windows ──────────────────────────────────────────────────────
        window_start = today - datetime.timedelta(days=24)
        daily_end    = today + datetime.timedelta(days=10)
        # Extend fetch to end of next month so monthly/weekly target sums are complete
        _nm = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
        window_end = (_nm + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1)

        first_of_this_month = today.replace(day=1)
        monthly_end = first_of_this_month - datetime.timedelta(days=1)
        monthly_start_dt = (first_of_this_month - datetime.timedelta(days=1)).replace(day=1)
        for _ in range(12):
            monthly_start_dt = (monthly_start_dt - datetime.timedelta(days=1)).replace(day=1)

        days_since_monday = today.weekday()
        last_sunday = today - datetime.timedelta(days=days_since_monday + 1)
        weekly_end = last_sunday

        # Week mondays (9 complete weeks + current) and their N-1 equivalents —
        # computed before the query so the fetch window covers the N-1 weeks
        week_mondays = []
        w = _week_monday_of(weekly_end)
        for _ in range(9):
            week_mondays.append(w)
            w -= datetime.timedelta(weeks=1)
        week_mondays = list(reversed(week_mondays))
        current_week_monday = _week_monday_of(today)
        week_mondays.append(current_week_monday)
        prev_week_mondays = [_prev_iso_week_monday(m) for m in week_mondays]

        fetch_start = min(window_start, monthly_start_dt, prev_week_mondays[0])

        with SnowflakeClient() as sf:
            rows, cols = sf.execute("""
                SELECT
                    SNAPSHOT_DATE,
                    ORDERS,
                    ORDERS_N_MINUS_1,
                    ORDERS_TARGET,
                    ORDERS_TARGET_N_MINUS_1,
                    SESSIONS_TARGET,
                    SESSIONS_TARGET_N_MINUS_1
                FROM PROD_CARSUPERMARKET_DB.product_slt_kpis.daily_summary
                WHERE SNAPSHOT_DATE >= %s AND SNAPSHOT_DATE <= %s
                ORDER BY SNAPSHOT_DATE
            """, [str(fetch_start), str(window_end)])

        col_map = {c.upper(): i for i, c in enumerate(cols)}

        def _col(r, name):
            i = col_map.get(name)
            return r[i] if i is not None else None

        # Index all columns by date
        by_date: dict[str, dict] = {}
        for r in rows:
            d = _col(r, 'SNAPSHOT_DATE')
            d = d.isoformat() if isinstance(d, datetime.date) else str(d)[:10]
            by_date[d] = {
                'orders':                   _col(r, 'ORDERS'),
                'orders_n1':                _col(r, 'ORDERS_N_MINUS_1'),
                'orders_target':            _col(r, 'ORDERS_TARGET'),
                'orders_target_n1':         _col(r, 'ORDERS_TARGET_N_MINUS_1'),
                'sessions_target':          _col(r, 'SESSIONS_TARGET'),
                'sessions_target_n1':       _col(r, 'SESSIONS_TARGET_N_MINUS_1'),
            }

        def _int(val):
            try:
                return int(val) if val is not None else None
            except (TypeError, ValueError):
                return None

        def _int0(val):
            v = _int(val)
            return v if v is not None else 0

        month_abbrs = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

        def _sum_month(ym: str, field: str) -> int:
            y, mo = int(ym[:4]), int(ym[5:7])
            days_in = calendar.monthrange(y, mo)[1]
            return sum(_int0(by_date.get(datetime.date(y, mo, day).isoformat(), {}).get(field)) for day in range(1, days_in + 1))

        def _sum_week(monday: datetime.date, field: str) -> int:
            return sum(_int0(by_date.get((monday + datetime.timedelta(days=i)).isoformat(), {}).get(field)) for i in range(7))

        # ── Daily arrays ──────────────────────────────────────────────────────
        dates = []
        d_iter = window_start
        while d_iter <= daily_end:
            dates.append(d_iter.isoformat())
            d_iter += datetime.timedelta(days=1)

        orders            = [_int(by_date.get(d, {}).get('orders'))            for d in dates]
        orders_ly         = [_int(by_date.get(d, {}).get('orders_n1'))         for d in dates]
        orders_target     = [_int(by_date.get(d, {}).get('orders_target'))     for d in dates]
        sessions_target   = [_int(by_date.get(d, {}).get('sessions_target'))   for d in dates]

        known = [v for v in orders if v is not None]
        average = round(sum(known) / len(known), 1) if known else None

        # ── Monthly arrays ────────────────────────────────────────────────────
        months = []
        m_iter = monthly_start_dt
        while m_iter <= monthly_end:
            months.append(m_iter.strftime('%Y-%m'))
            days_in = calendar.monthrange(m_iter.year, m_iter.month)[1]
            m_iter = (m_iter.replace(day=days_in) + datetime.timedelta(days=1)).replace(day=1)
        months.append(first_of_this_month.strftime('%Y-%m'))

        current_ym = first_of_this_month.strftime('%Y-%m')
        monthly_orders            = [None if m == current_ym else _sum_month(m, 'orders')            for m in months]
        monthly_orders_ly         = [_sum_month(m, 'orders_n1')                                       for m in months]
        monthly_orders_target     = [_sum_month(m, 'orders_target')      for m in months]
        monthly_sessions_target   = [_sum_month(m, 'sessions_target')    for m in months]
        monthly_labels            = [month_abbrs[int(m[5:7]) - 1] + ' ' + m[2:4]                     for m in months]
        monthly_days              = [calendar.monthrange(int(m[:4]), int(m[5:7]))[1]                  for m in months]

        # ── Weekly arrays ─────────────────────────────────────────────────────
        # N-1 weeks use the same ISO week NUMBER of the previous year (Mon-Sun),
        # summed from the raw daily values — not the N_MINUS_1 column, which is
        # a same-calendar-date shift
        weekly_labels             = [f"W{m.isocalendar()[1]} ({m.day}-{month_abbrs[m.month-1]})" for m in week_mondays]
        weekly_n1_labels          = [f"W{pm.isocalendar()[1]} ({pm.day}-{month_abbrs[pm.month-1]})" for pm in prev_week_mondays]
        weekly_orders             = [None if m == current_week_monday else _sum_week(m, 'orders') for m in week_mondays]
        weekly_orders_ly          = [_sum_week(pm, 'orders') for pm in prev_week_mondays]
        weekly_orders_target      = [_sum_week(m, 'orders_target')       for m in week_mondays]
        weekly_sessions_target    = [_sum_week(m, 'sessions_target')     for m in week_mondays]

        return {
            "status": "ok",
            "today": today.isoformat(),
            # daily
            "dates": dates,
            "orders": orders,
            "orders_ly": orders_ly,
            "orders_target": orders_target,
            "sessions_target": sessions_target,
            "average": average,
            # monthly
            "monthly_labels": monthly_labels,
            "monthly_days": monthly_days,
            "monthly_orders": monthly_orders,
            "monthly_orders_ly": monthly_orders_ly,
            "monthly_orders_target": monthly_orders_target,
            "monthly_sessions_target": monthly_sessions_target,
            # weekly
            "weekly_labels": weekly_labels,
            "weekly_n1_labels": weekly_n1_labels,
            "weekly_orders": weekly_orders,
            "weekly_orders_ly": weekly_orders_ly,
            "weekly_orders_target": weekly_orders_target,
            "weekly_sessions_target": weekly_sessions_target,
        }
    except Exception as e:
        logger.error(f"KPI data failed: {e}")
        return {"status": "error", "message": str(e)}


# ── Custom charts: metric catalogue ──────────────────────────────────────────
# Columns from product_slt_kpis.daily_summary available for user-created charts.
# (column, description, suggested_agg) — suggested_agg is the sensible default
# for weekly/monthly rollups: 'sum' for daily flow events, 'avg' for stock
# levels and pre-averaged metrics.
KPI_METRICS = [
    ("ORDERS", "Apex orders acknowledged on the day", "sum"),
    ("ORDERS_RATE_TOTAL", "Orders financed on a standard rate (non interest-free)", "sum"),
    ("ORDERS_CASH_TOTAL", "Orders paid in cash (no finance)", "sum"),
    ("ORDERS_RATE_0_15K", "Rate-financed orders with vehicle price £0-15k", "sum"),
    ("ORDERS_RATE_15_25K", "Rate-financed orders with vehicle price £15-25k", "sum"),
    ("ORDERS_RATE_25K_PLUS", "Rate-financed orders with vehicle price £25k+", "sum"),
    ("ORDERS_CASH_0_15K", "Cash orders with vehicle price £0-15k", "sum"),
    ("ORDERS_CASH_15_25K", "Cash orders with vehicle price £15-25k", "sum"),
    ("ORDERS_CASH_25K_PLUS", "Cash orders with vehicle price £25k+", "sum"),
    ("ORDERS_PRICE_CHANGED_LAST_7_DAYS", "Orders where the vehicle had a price change in the 7 days before the order", "sum"),
    ("ORDERS_PUBLISHED_LAST_7_DAYS", "Orders where the vehicle was first advertised in the 7 days before the order", "sum"),
    ("PURCHASES", "Vehicles purchased into stock on the day", "sum"),
    ("PUBLISHED", "Vehicles first advertised on the day", "sum"),
    ("STOCK_ON_SALE", "Advertised vehicles in stock (end of day)", "avg"),
    ("STOCK_ON_SALE_STORES", "Advertised stock physically located at a retail store", "avg"),
    ("STOCK_ON_SALE_RATE", "Advertised stock on standard rate finance (non interest-free)", "avg"),
    ("STOCK_ON_SALE_0PCT", "Advertised stock on 0% interest-free finance", "avg"),
    ("STOCK_ON_SALE_RATE_EV", "Advertised electric vehicles on standard rate finance", "avg"),
    ("TOTAL_STOCK", "All vehicles in stock, advertised or not (end of day)", "avg"),
    ("STOCK_45_PLUS_DAYS", "Vehicles in stock for 45 days or more", "avg"),
    ("STOCK_60_PLUS_DAYS", "Vehicles in stock for 60 days or more", "avg"),
    ("AVG_DAYS_IN_STOCK", "Average days in stock across all vehicles", "avg"),
    ("AVG_GPU_M_STOCK_ON_SALE", "Average GPU-M (£) of advertised stock", "avg"),
    ("AVG_GPU_M_B2B2C", "Average GPU-M (£) of advertised B2B2C stock", "avg"),
    ("AVG_GPU_M_CONSIGNMENT", "Average GPU-M (£) of advertised consignment stock", "avg"),
    ("AVG_GPU_M_C2B2C_PX", "Average GPU-M (£) of advertised part-exchange stock", "avg"),
    ("AVG_GPU_M_C2B2C_OTHER", "Average GPU-M (£) of advertised C2B2C (non-PX) stock", "avg"),
    ("AVG_AT_PRICE_POSITION_UNSOLD_STOCK_RATE", "Average Autotrader price position of unsold advertised rate stock", "avg"),
    ("AVG_AT_PRICE_POSITION_UNSOLD_STOCK_0PCT", "Average Autotrader price position of unsold advertised 0% stock", "avg"),
    ("AVG_AT_PRICE_POSITION_ORDERS_RATE", "Average Autotrader price position of rate vehicles ordered that day", "avg"),
    ("AVG_AT_PRICE_POSITION_ORDERS_0PCT", "Average Autotrader price position of 0% vehicles ordered that day", "avg"),
]
KPI_METRIC_COLUMNS = {m[0] for m in KPI_METRICS}

# Live column discovery: N-1 / target siblings are detected by naming
# convention (<METRIC>_N_MINUS_1, <METRIC>_TARGET) against the actual table,
# so columns added to daily_summary later are picked up without code changes.
_ds_columns_cache: set | None = None
_ds_columns_cache_at: float = 0.0
_DS_COLUMNS_TTL = 600  # seconds


def _get_daily_summary_columns() -> set:
    global _ds_columns_cache, _ds_columns_cache_at
    if _ds_columns_cache is not None and time.time() - _ds_columns_cache_at < _DS_COLUMNS_TTL:
        return _ds_columns_cache
    try:
        with SnowflakeClient() as sf:
            rows, _ = sf.execute("""
                SELECT column_name
                FROM PROD_CARSUPERMARKET_DB.INFORMATION_SCHEMA.COLUMNS
                WHERE table_schema = 'PRODUCT_SLT_KPIS' AND table_name = 'DAILY_SUMMARY'
            """)
        _ds_columns_cache = {str(r[0]).upper() for r in rows}
        _ds_columns_cache_at = time.time()
    except Exception as e:
        logger.warning("daily_summary column discovery failed: %s", e)
        if _ds_columns_cache is None:
            # Minimal fallback: the metric columns plus the known orders siblings
            _ds_columns_cache = KPI_METRIC_COLUMNS | {"ORDERS_N_MINUS_1", "ORDERS_TARGET"}
    return _ds_columns_cache


def _sibling_columns(metric: str) -> tuple[str | None, str | None]:
    """Return (n1_col, target_col) for a metric — None where the column doesn't exist."""
    cols = _get_daily_summary_columns()
    n1 = f"{metric}_N_MINUS_1"
    target = f"{metric}_TARGET"
    return (n1 if n1 in cols else None, target if target in cols else None)

KPI_SECTIONS = ["Upper Funnel", "Results", "Stock", "Pricing", "Margin"]

KPI_DEFAULT_LAYOUT = {
    "rows": [
        {"id": "sessions", "type": "builtin", "section": "Upper Funnel"},
        {"id": "orders", "type": "builtin", "section": "Results"},
    ]
}


@router.get("/kpi/metrics")
def kpi_metrics(request: Request, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    live_cols = _get_daily_summary_columns()
    return {
        "status": "ok",
        "sections": KPI_SECTIONS,
        "metrics": [
            {
                "column": col,
                "description": desc,
                "suggested_agg": agg,
                "has_n1": f"{col}_N_MINUS_1" in live_cols,
                "has_target": f"{col}_TARGET" in live_cols,
            }
            for col, desc, agg in KPI_METRICS
        ],
    }


@router.get("/kpi/custom-data")
def kpi_custom_data(request: Request, metric: str, agg: str = "sum", user: str = Depends(get_current_user)):
    """Daily/weekly/monthly series for one whitelisted daily_summary column."""
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    metric = metric.upper()
    if metric not in KPI_METRIC_COLUMNS:
        return {"status": "error", "message": f"Unknown metric: {metric}"}
    if agg not in ("sum", "avg"):
        return {"status": "error", "message": f"Unknown agg: {agg}"}
    try:
        import calendar
        today = datetime.date.today()

        # Same date windows as /kpi/data
        window_start = today - datetime.timedelta(days=24)
        daily_end    = today + datetime.timedelta(days=10)
        _nm = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
        window_end = (_nm + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1)

        first_of_this_month = today.replace(day=1)
        monthly_end = first_of_this_month - datetime.timedelta(days=1)
        monthly_start_dt = (first_of_this_month - datetime.timedelta(days=1)).replace(day=1)
        for _ in range(12):
            monthly_start_dt = (monthly_start_dt - datetime.timedelta(days=1)).replace(day=1)

        days_since_monday = today.weekday()
        last_sunday = today - datetime.timedelta(days=days_since_monday + 1)
        weekly_end = last_sunday

        # Week mondays and their previous-ISO-year equivalents (same week number)
        week_mondays = []
        w = _week_monday_of(weekly_end)
        for _ in range(9):
            week_mondays.append(w)
            w -= datetime.timedelta(weeks=1)
        week_mondays = list(reversed(week_mondays))
        current_week_monday = _week_monday_of(today)
        week_mondays.append(current_week_monday)
        prev_week_mondays = [_prev_iso_week_monday(m) for m in week_mondays]

        fetch_start = min(window_start, monthly_start_dt, prev_week_mondays[0])

        n1_col, target_col = _sibling_columns(metric)
        # All column names come from the whitelists above — safe to interpolate
        select_cols = ["SNAPSHOT_DATE", metric] + ([n1_col] if n1_col else []) + ([target_col] if target_col else [])

        with SnowflakeClient() as sf:
            rows, cols = sf.execute(f"""
                SELECT {', '.join(select_cols)}
                FROM PROD_CARSUPERMARKET_DB.product_slt_kpis.daily_summary
                WHERE SNAPSHOT_DATE >= %s AND SNAPSHOT_DATE <= %s
                ORDER BY SNAPSHOT_DATE
            """, [str(fetch_start), str(window_end)])

        col_map = {c.upper(): i for i, c in enumerate(cols)}

        def _num(val):
            try:
                return float(val) if val is not None else None
            except (TypeError, ValueError):
                return None

        def _round(v):
            if v is None:
                return None
            return int(v) if float(v).is_integer() else round(v, 2)

        by_date: dict[str, dict] = {}
        for r in rows:
            d = r[col_map['SNAPSHOT_DATE']]
            d = d.isoformat() if isinstance(d, datetime.date) else str(d)[:10]
            by_date[d] = {
                'v':  _num(r[col_map[metric]]),
                'n1': _num(r[col_map[n1_col]]) if n1_col else None,
                't':  _num(r[col_map[target_col]]) if target_col else None,
            }

        def _agg(vals):
            known = [v for v in vals if v is not None]
            if not known:
                return None
            total = sum(known)
            return _round(total if agg == 'sum' else total / len(known))

        def _agg_month(ym: str, field: str):
            y, mo = int(ym[:4]), int(ym[5:7])
            days_in = calendar.monthrange(y, mo)[1]
            return _agg([by_date.get(datetime.date(y, mo, day).isoformat(), {}).get(field) for day in range(1, days_in + 1)])

        def _agg_week(monday: datetime.date, field: str):
            return _agg([by_date.get((monday + datetime.timedelta(days=i)).isoformat(), {}).get(field) for i in range(7)])

        month_abbrs = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

        # ── Daily arrays ──────────────────────────────────────────────────────
        dates = []
        d_iter = window_start
        while d_iter <= daily_end:
            dates.append(d_iter.isoformat())
            d_iter += datetime.timedelta(days=1)

        values        = [_round(by_date.get(d, {}).get('v'))  for d in dates]
        values_n1     = [_round(by_date.get(d, {}).get('n1')) for d in dates] if n1_col else None
        values_target = [_round(by_date.get(d, {}).get('t'))  for d in dates] if target_col else None

        # ── Monthly arrays ────────────────────────────────────────────────────
        months = []
        m_iter = monthly_start_dt
        while m_iter <= monthly_end:
            months.append(m_iter.strftime('%Y-%m'))
            days_in = calendar.monthrange(m_iter.year, m_iter.month)[1]
            m_iter = (m_iter.replace(day=days_in) + datetime.timedelta(days=1)).replace(day=1)
        months.append(first_of_this_month.strftime('%Y-%m'))

        current_ym = first_of_this_month.strftime('%Y-%m')
        monthly_values        = [None if m == current_ym else _agg_month(m, 'v') for m in months]
        monthly_values_n1     = [_agg_month(m, 'n1') for m in months] if n1_col else None
        monthly_values_target = [_agg_month(m, 't')  for m in months] if target_col else None
        monthly_labels        = [month_abbrs[int(m[5:7]) - 1] + ' ' + m[2:4] for m in months]

        # ── Weekly arrays ─────────────────────────────────────────────────────
        # Weekly N-1 = same ISO week NUMBER of the previous year, aggregated
        # from the metric's own daily history — available for every metric,
        # even without an _N_MINUS_1 column
        weekly_labels    = [f"W{m.isocalendar()[1]} ({m.day}-{month_abbrs[m.month-1]})" for m in week_mondays]
        weekly_n1_labels = [f"W{pm.isocalendar()[1]} ({pm.day}-{month_abbrs[pm.month-1]})" for pm in prev_week_mondays]
        weekly_values        = [None if m == current_week_monday else _agg_week(m, 'v') for m in week_mondays]
        weekly_values_n1     = [_agg_week(pm, 'v') for pm in prev_week_mondays]
        weekly_values_target = [_agg_week(m, 't')  for m in week_mondays] if target_col else None

        return {
            "status": "ok",
            "today": today.isoformat(),
            "metric": metric,
            "agg": agg,
            "has_n1": bool(n1_col),
            "has_target": bool(target_col),
            "dates": dates,
            "values": values,
            "values_n1": values_n1,
            "values_target": values_target,
            "monthly_labels": monthly_labels,
            "monthly_values": monthly_values,
            "monthly_values_n1": monthly_values_n1,
            "monthly_values_target": monthly_values_target,
            "weekly_labels": weekly_labels,
            "weekly_n1_labels": weekly_n1_labels,
            "weekly_values": weekly_values,
            "weekly_values_n1": weekly_values_n1,
            "weekly_values_target": weekly_values_target,
        }
    except Exception as e:
        logger.error(f"KPI custom data failed for {metric}: {e}")
        return {"status": "error", "message": str(e)}


def _validate_layout(layout: dict) -> str | None:
    """Return an error message if the layout payload is invalid, else None."""
    rows = layout.get("rows") if isinstance(layout, dict) else None
    if not isinstance(rows, list):
        return "layout.rows must be a list"
    for row in rows:
        if not isinstance(row, dict):
            return "each row must be an object"
        if row.get("type") not in ("builtin", "custom"):
            return f"bad row type: {row.get('type')}"
        if row.get("section") not in KPI_SECTIONS:
            return f"bad section: {row.get('section')}"
        if row["type"] == "builtin" and row.get("id") not in ("sessions", "orders"):
            return f"bad builtin id: {row.get('id')}"
        if row["type"] == "custom":
            if row.get("metric") not in KPI_METRIC_COLUMNS:
                return f"bad metric: {row.get('metric')}"
            if row.get("agg") not in ("sum", "avg"):
                return f"bad agg: {row.get('agg')}"
            title = row.get("title", row["metric"])
            if not isinstance(title, str) or not title.strip() or len(title) > 100:
                return "bad title"
            if not isinstance(row.get("invert", False), bool):
                return "bad invert"
            if row.get("fmt", "num") not in ("num", "pct"):
                return f"bad fmt: {row.get('fmt')}"
            decimals = row.get("decimals", 0)
            if not isinstance(decimals, int) or not (0 <= decimals <= 3):
                return f"bad decimals: {decimals}"
    builtin_ids = [r["id"] for r in rows if r["type"] == "builtin"]
    if sorted(builtin_ids) != ["orders", "sessions"]:
        return "layout must contain the sessions and orders builtin rows exactly once"
    return None


@router.get("/kpi/layout")
def kpi_layout_get(request: Request, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    try:
        # Layout always lives in the PROD cluster so local/dev edits and prod
        # serve the same dashboard
        with get_prod_pricer_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE IF NOT EXISTS kpi_layout (key VARCHAR(50) PRIMARY KEY, value TEXT)")
                cur.execute("SELECT value FROM kpi_layout WHERE key = 'layout'")
                row = cur.fetchone()
            conn.commit()
        layout = json.loads(row[0]) if row else KPI_DEFAULT_LAYOUT
        return {"status": "ok", "layout": layout, "sections": KPI_SECTIONS}
    except Exception as e:
        logger.error(f"KPI layout load failed: {e}")
        return {"status": "ok", "layout": KPI_DEFAULT_LAYOUT, "sections": KPI_SECTIONS}


@router.post("/kpi/layout")
def kpi_layout_save(request: Request, payload: dict, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    layout = (payload or {}).get("layout")
    err = _validate_layout(layout or {})
    if err:
        return {"status": "error", "message": err}
    try:
        # Always write to the PROD cluster — see kpi_layout_get
        with get_prod_pricer_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE TABLE IF NOT EXISTS kpi_layout (key VARCHAR(50) PRIMARY KEY, value TEXT)")
                cur.execute(
                    "INSERT INTO kpi_layout (key, value) VALUES ('layout', %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (json.dumps(layout),)
                )
            conn.commit()
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"KPI layout save failed: {e}")
        return {"status": "error", "message": str(e)}


def _get_gemini_api_key() -> str | None:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if api_key:
        return api_key

    # Local-only fallback for testing until the Gemini key can be moved into AWS Secrets Manager.
    fallback_api_key = (
        None
        if os.getenv("LOCAL_ENV") == "1" or not os.getenv("AWS_LAMBDA_FUNCTION_NAME")
        else None
    )

    secret_name = os.getenv("GEMINI_SECRET_NAME")
    if not secret_name:
        return fallback_api_key

    try:
        secret = get_secret(secret_name)
    except Exception as e:
        logger.warning("Could not load Gemini secret %s: %s", secret_name, e)
        return fallback_api_key

    return (
        secret.get("GEMINI_API_KEY")
        or secret.get("GOOGLE_API_KEY")
        or secret.get("api_key")
        or secret.get("key")
        or fallback_api_key
    )


@router.post("/kpi/ai-summary")
def kpi_ai_summary(request: Request, payload: dict, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        raise HTTPException(status_code=403)

    prompt = (payload or {}).get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return {"status": "error", "message": "Missing prompt"}

    api_key = _get_gemini_api_key()
    if not api_key:
        return {"status": "error", "message": "Gemini API key is not configured"}

    try:
        resp = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            params={"key": api_key},
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 4096, "temperature": 0.3},
            },
            timeout=30,
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
        finish_reason = candidate.get("finishReason", "unknown")
        usage = data.get("usageMetadata", {})
        logger.info(
            "Gemini AI summary: finishReason=%s tokens_in=%s tokens_out=%s",
            finish_reason,
            usage.get("promptTokenCount"),
            usage.get("candidatesTokenCount"),
        )
        parts = candidate["content"]["parts"]
        summary = "".join(part.get("text", "") for part in parts).strip()
        return {"status": "ok", "summary": summary}
    except requests.exceptions.RequestException as e:
        logger.error("KPI AI summary request failed: %s", e)
        return {"status": "error", "message": str(e)}
    except (KeyError, IndexError, TypeError) as e:
        logger.error("KPI AI summary response parse failed: %s", e)
        return {"status": "error", "message": "Could not read Gemini response"}


_kpi_sp_cache: dict = {}
_kpi_sp_cache_at: float = 0.0

# S3 cache for the aggregated SharePoint payload (small JSON, not the
# spreadsheet itself). Shared key (not env-scoped) so one fetch warms local,
# dev and prod. The SharePoint API is slow and unreliable — the S3 copy also
# serves as a stale fallback.
# The source spreadsheet is refreshed once per day between 08:00 and 09:00 UK,
# so a cached payload is fresh if fetched after the most recent 09:00 UK.
_SP_S3_BUCKET = "aa-raw-sources-uk"
_SP_S3_KEY = "applications/pricing/uk-vehicle-list-evaluator/shared/cache/kpi_sharepoint_sessions_v3.json"


def _sp_cache_is_fresh(cached_at: float) -> bool:
    import zoneinfo
    uk = zoneinfo.ZoneInfo("Europe/London")
    now_uk = datetime.datetime.now(uk)
    boundary = now_uk.replace(hour=9, minute=0, second=0, microsecond=0)
    if boundary > now_uk:
        boundary -= datetime.timedelta(days=1)
    return cached_at >= boundary.timestamp()


def _sp_s3_load() -> tuple[float, str | None, dict | None]:
    """Return (cached_at, date_iso, data) from the S3 cache, or (0.0, None, None)."""
    try:
        import boto3
        obj = boto3.client("s3", region_name="eu-west-1").get_object(Bucket=_SP_S3_BUCKET, Key=_SP_S3_KEY)
        payload = json.loads(obj["Body"].read())
        return payload.get("cached_at", 0.0), payload.get("date"), payload.get("data")
    except Exception as e:
        logger.warning("SharePoint S3 cache read failed: %s", e)
        return 0.0, None, None


def _sp_s3_save(cached_at: float, date_str: str, data: dict) -> None:
    try:
        import boto3
        boto3.client("s3", region_name="eu-west-1").put_object(
            Bucket=_SP_S3_BUCKET, Key=_SP_S3_KEY,
            Body=json.dumps({"cached_at": cached_at, "date": date_str, "data": data}).encode(),
            ContentType="application/json",
        )
    except Exception as e:
        logger.warning("SharePoint S3 cache write failed: %s", e)


def _get_sharepoint_data() -> dict:
    """Memory cache → S3 cache → SharePoint fetch → stale S3 fallback."""
    global _kpi_sp_cache, _kpi_sp_cache_at
    if _kpi_sp_cache.get("status") == "ok" and _sp_cache_is_fresh(_kpi_sp_cache_at):
        logger.info("KPI SharePoint: serving from memory cache")
        return _kpi_sp_cache

    cached_at, cache_date, cached = _sp_s3_load()
    if cached and cached.get("status") == "ok" and _sp_cache_is_fresh(cached_at):
        logger.info("KPI SharePoint: serving from S3 cache (fetched %s)", cache_date)
        _kpi_sp_cache = cached
        _kpi_sp_cache_at = cached_at
        return cached

    logger.info("KPI SharePoint: cache stale or missing, fetching from SharePoint")
    try:
        result = _build_kpi_sharepoint_data()
    except Exception as e:
        logger.error("KPI SharePoint fetch failed: %s", e)
        result = {"status": "error", "message": str(e)}

    if result.get("status") == "ok":
        now = time.time()
        _kpi_sp_cache = result
        _kpi_sp_cache_at = now
        _sp_s3_save(now, datetime.date.today().isoformat(), result)
        return result

    # SharePoint failed — fall back to the most recent S3 payload if we have one
    if cached and cached.get("status") == "ok":
        logger.warning("KPI SharePoint: fetch failed, serving stale S3 cache from %s", cache_date)
        stale = dict(cached)
        stale["stale"] = True
        stale["stale_date"] = cache_date
        return stale
    return result


def warm_sharepoint_cache() -> None:
    """Refresh the SharePoint KPI cache if stale. Called by the /home cache warmer."""
    _get_sharepoint_data()
    logger.info("Home cache warm (SharePoint KPI) complete")


def _build_kpi_sharepoint_data() -> dict:
    """Fetch Table1 and aggregate Sessions. Result is cached in _kpi_sp_cache."""
    import calendar

    client = SharePointClient()
    rows = client.get_table_rows(
        table_name="Table1",
        sharepoint_url="https://carsmkt.sharepoint.com/teams/marketing/Reports/Daily%20Spend%20and%20Enquiries.xlsx",
    )

    if not rows:
        return {"status": "error", "message": "No rows returned from SharePoint"}

    columns = list(rows[0].keys())
    col_map = {c.lower(): c for c in columns}
    date_col = col_map.get("date")
    sessions_col = next((col_map[k] for k in col_map if "session" in k), None)
    if not date_col or not sessions_col:
        return {"status": "error", "message": f"Could not find Date/Sessions columns. Found: {columns}"}

    def _parse_date(val) -> str | None:
        if val is None or val == "":
            return None
        if isinstance(val, (int, float)):
            try:
                return (datetime.date(1899, 12, 30) + datetime.timedelta(days=int(val))).isoformat()
            except Exception:
                return None
        s = str(val).strip()[:10]
        try:
            datetime.date.fromisoformat(s)
            return s
        except ValueError:
            return None

    today = datetime.date.today()

    # 26 months back: the monthly N-1 line needs up to 25 months of history
    # (LY of the oldest displayed month) and the ISO-week N-1 needs ~62 weeks
    first_of_this_month = today.replace(day=1)
    cutoff = (first_of_this_month - datetime.timedelta(days=1)).replace(day=1)
    for _ in range(25):
        cutoff = (cutoff - datetime.timedelta(days=1)).replace(day=1)
    cutoff_str = cutoff.isoformat()

    by_date: dict[str, int] = {}
    for row in rows:
        d = _parse_date(row.get(date_col))
        if not d or d < cutoff_str:
            continue
        v = row.get(sessions_col)
        try:
            by_date[d] = int(float(v)) if v not in (None, "") else 0
        except (ValueError, TypeError):
            by_date[d] = 0
    month_abbrs = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

    def _sum_month_sp(ym: str) -> int:
        y, mo = int(ym[:4]), int(ym[5:7])
        days_in = calendar.monthrange(y, mo)[1]
        return sum(by_date.get(datetime.date(y, mo, day).isoformat(), 0) for day in range(1, days_in + 1))

    def _sum_week_sp(monday: datetime.date) -> int:
        return sum(by_date.get((monday + datetime.timedelta(days=i)).isoformat(), 0) for i in range(7))

    first_of_this_month = today.replace(day=1)
    monthly_end = first_of_this_month - datetime.timedelta(days=1)
    m_iter = (first_of_this_month - datetime.timedelta(days=1)).replace(day=1)
    for _ in range(12):
        m_iter = (m_iter - datetime.timedelta(days=1)).replace(day=1)
    months = []
    while m_iter <= monthly_end:
        months.append(m_iter.strftime('%Y-%m'))
        days_in = calendar.monthrange(m_iter.year, m_iter.month)[1]
        m_iter = (m_iter.replace(day=days_in) + datetime.timedelta(days=1)).replace(day=1)
    months.append(first_of_this_month.strftime('%Y-%m'))

    monthly_sessions = [None if m == first_of_this_month.strftime('%Y-%m') else _sum_month_sp(m) for m in months]
    monthly_sessions_ly = [_sum_month_sp(f"{int(m[:4])-1}-{m[5:7]}") for m in months]
    monthly_labels = [month_abbrs[int(m[5:7]) - 1] + ' ' + m[2:4] for m in months]

    days_since_monday = today.weekday()
    last_sunday = today - datetime.timedelta(days=days_since_monday + 1)
    week_mondays = []
    w = _week_monday_of(last_sunday)
    for _ in range(9):
        week_mondays.append(w)
        w -= datetime.timedelta(weeks=1)
    week_mondays = list(reversed(week_mondays))
    current_week_monday = _week_monday_of(today)
    week_mondays.append(current_week_monday)

    weekly_labels = [f"W{m.isocalendar()[1]} ({m.day}-{month_abbrs[m.month-1]})" for m in week_mondays]
    weekly_sessions = [None if m == current_week_monday else _sum_week_sp(m) for m in week_mondays]
    # N-1 = same ISO week number of the previous year, not the same dates -52 weeks
    weekly_sessions_ly = [_sum_week_sp(_prev_iso_week_monday(m)) for m in week_mondays]

    window_start = today - datetime.timedelta(days=24)
    window_end = today + datetime.timedelta(days=10)
    dates = []
    d_iter = window_start
    while d_iter <= window_end:
        dates.append(d_iter.isoformat())
        d_iter += datetime.timedelta(days=1)

    daily_sessions = [by_date.get(d) if d <= today.isoformat() else None for d in dates]
    daily_sessions_ly = [by_date.get((datetime.date.fromisoformat(d) - datetime.timedelta(days=365)).isoformat()) for d in dates]

    return {
        "status": "ok",
        "sessions_col": sessions_col,
        "today": today.isoformat(),
        "monthly_labels": monthly_labels,
        "monthly_sessions": monthly_sessions,
        "monthly_sessions_ly": monthly_sessions_ly,
        "weekly_labels": weekly_labels,
        "weekly_sessions": weekly_sessions,
        "weekly_sessions_ly": weekly_sessions_ly,
        "dates": dates,
        "daily_sessions": daily_sessions,
        "daily_sessions_ly": daily_sessions_ly,
    }


@router.get("/kpi/sharepoint")
def kpi_sharepoint(request: Request, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    try:
        return _get_sharepoint_data()
    except Exception as e:
        logger.error("KPI SharePoint fetch failed: %s", e)
        return {"status": "error", "message": str(e)}


@router.get("/weekly-business-review", response_class=HTMLResponse)
def weekly_business_review_page(request: Request, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    _track(request, 'page_view', '/weekly-business-review')
    return templates.TemplateResponse("weekly_business_review.html", {"request": request, "user": user, "BASE_URL": BASE_URL})


@router.get("/weekly-business-review/data")
def weekly_business_review_data(request: Request, user: str = Depends(get_current_user)):
    """Return monthly Net Deliveries B2C (b2c_units): last 3 complete months, current month, next 3 months."""
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    try:
        today = datetime.date.today()
        first_of_this_month = today.replace(day=1)

        def _shift_month(d: datetime.date, n: int) -> datetime.date:
            m = d.month - 1 + n
            return datetime.date(d.year + m // 12, m % 12 + 1, 1)

        month_firsts = [_shift_month(first_of_this_month, n) for n in range(-3, 4)]
        # Fetch an extra year back so each displayed month's N-1 (12 months prior) is covered
        fetch_start = _shift_month(month_firsts[0], -12)
        fetch_end = month_firsts[-1]

        with SnowflakeClient() as sf:
            cost_rows, cost_cols = sf.execute("""
                SELECT MONTH_START, COST_AMOUNT
                FROM PROD_CARSUPERMARKET_DB.SOURCE_FINANCE.MANAGEMENT_ACCOUNTS
                WHERE COST_TYPE = 'b2c_units'
                  AND MONTH_START >= %s AND MONTH_START <= %s
            """, [str(fetch_start), str(fetch_end)])
            budget_rows, budget_cols = sf.execute("""
                SELECT MONTH_YEAR, VALUE
                FROM PROD_CARSUPERMARKET_DB.SOURCE_FINANCE.BUDGET
                WHERE ITEM = 'b2c_units'
                  AND MONTH_YEAR >= %s AND MONTH_YEAR <= %s
            """, [str(fetch_start), str(fetch_end)])

        cost_col_map = {c.upper(): i for i, c in enumerate(cost_cols)}
        budget_col_map = {c.upper(): i for i, c in enumerate(budget_cols)}

        def _num(val):
            try:
                return round(float(val)) if val is not None else None
            except (TypeError, ValueError):
                return None

        actual_by_month: dict[str, float] = {}
        for r in cost_rows:
            d = str(r[cost_col_map['MONTH_START']])[:10]
            actual_by_month[d] = _num(r[cost_col_map['COST_AMOUNT']])

        target_by_month: dict[str, float] = {}
        for r in budget_rows:
            d = str(r[budget_col_map['MONTH_YEAR']])[:10]
            target_by_month[d] = _num(r[budget_col_map['VALUE']])

        month_abbrs = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

        monthly_labels             = [month_abbrs[mf.month - 1] + ' ' + str(mf.year)[2:] for mf in month_firsts]
        # Actual only exists up to the current month; N-1 (actual, shifted -12) and
        # Target (budget) are forward-looking and populated for the full window
        monthly_b2c_units          = [actual_by_month.get(mf.isoformat()) if mf <= first_of_this_month else None for mf in month_firsts]
        monthly_b2c_units_ly       = [actual_by_month.get(_shift_month(mf, -12).isoformat()) for mf in month_firsts]
        monthly_b2c_units_target   = [target_by_month.get(mf.isoformat()) for mf in month_firsts]

        return {
            "status": "ok",
            "today": today.isoformat(),
            "monthly_labels": monthly_labels,
            "monthly_b2c_units": monthly_b2c_units,
            "monthly_b2c_units_ly": monthly_b2c_units_ly,
            "monthly_b2c_units_target": monthly_b2c_units_target,
        }
    except Exception as e:
        logger.error(f"Weekly Business Review data failed: {e}")
        return {"status": "error", "message": str(e)}
