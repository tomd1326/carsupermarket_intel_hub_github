"""KPI dashboard: Snowflake daily summary, SharePoint sessions, Gemini AI summary."""

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
import os
import requests
import logging
import datetime


from utils.db_utils import (get_database_connection, get_settings)
from utils.snowflake_client import SnowflakeClient
from utils.sharepoint_client import SharePointClient
from utils.secrets_manager_client import get_secret

from .core import (BASE_URL, templates, _track, get_current_user)

logger = logging.getLogger(__name__)

router = APIRouter()


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
        weekly_start = weekly_end - datetime.timedelta(weeks=9) + datetime.timedelta(days=1)

        fetch_start = min(window_start, monthly_start_dt, weekly_start)

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

        def _week_monday(d: datetime.date) -> datetime.date:
            return d - datetime.timedelta(days=d.weekday())

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
        week_mondays = []
        w = _week_monday(weekly_end)
        for _ in range(9):
            week_mondays.append(w)
            w -= datetime.timedelta(weeks=1)
        week_mondays = list(reversed(week_mondays))
        current_week_monday = _week_monday(today)
        week_mondays.append(current_week_monday)

        weekly_labels             = [f"W{m.isocalendar()[1]} ({m.day}-{month_abbrs[m.month-1]})" for m in week_mondays]
        weekly_n1_labels          = [f"W{(m - datetime.timedelta(weeks=52)).isocalendar()[1]} ({(m - datetime.timedelta(weeks=52)).day}-{month_abbrs[(m - datetime.timedelta(weeks=52)).month-1]})" for m in week_mondays]
        weekly_orders             = [None if m == current_week_monday else _sum_week(m, 'orders')             for m in week_mondays]
        weekly_orders_ly          = [_sum_week(m, 'orders_n1')                                                 for m in week_mondays]
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
_kpi_sp_cache_date: datetime.date | None = None


def warm_sharepoint_cache() -> None:
    """Refresh the SharePoint KPI cache if stale. Called by the /home cache warmer."""
    global _kpi_sp_cache_date
    today = datetime.date.today()
    if not _kpi_sp_cache or _kpi_sp_cache_date != today:
        result = _build_kpi_sharepoint_data()
        _kpi_sp_cache.clear()
        _kpi_sp_cache.update(result)
        _kpi_sp_cache_date = today
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

    # 13 months back — same window as the Snowflake query
    first_of_this_month = today.replace(day=1)
    cutoff = (first_of_this_month - datetime.timedelta(days=1)).replace(day=1)
    for _ in range(12):
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

    def _week_monday(d: datetime.date) -> datetime.date:
        return d - datetime.timedelta(days=d.weekday())

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
    w = _week_monday(last_sunday)
    for _ in range(9):
        week_mondays.append(w)
        w -= datetime.timedelta(weeks=1)
    week_mondays = list(reversed(week_mondays))
    current_week_monday = _week_monday(today)
    week_mondays.append(current_week_monday)

    weekly_labels = [f"W{m.isocalendar()[1]} ({m.day}-{month_abbrs[m.month-1]})" for m in week_mondays]
    weekly_sessions = [None if m == current_week_monday else _sum_week_sp(m) for m in week_mondays]
    weekly_sessions_ly = [_sum_week_sp(m - datetime.timedelta(weeks=52)) for m in week_mondays]

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
    global _kpi_sp_cache, _kpi_sp_cache_date
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    try:
        today = datetime.date.today()
        if _kpi_sp_cache_date == today and _kpi_sp_cache:
            logger.info("KPI SharePoint: serving from cache (date=%s)", today)
            return _kpi_sp_cache
        logger.info("KPI SharePoint: cache miss (date=%s, cached=%s), fetching", today, _kpi_sp_cache_date)
        result = _build_kpi_sharepoint_data()
        _kpi_sp_cache = result
        _kpi_sp_cache_date = today
        return _kpi_sp_cache
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
    """Return monthly orders: last 3 complete months, current month, next 3 months."""
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    try:
        import calendar
        today = datetime.date.today()
        first_of_this_month = today.replace(day=1)

        def _shift_month(d: datetime.date, n: int) -> datetime.date:
            m = d.month - 1 + n
            return datetime.date(d.year + m // 12, m % 12 + 1, 1)

        month_firsts = [_shift_month(first_of_this_month, n) for n in range(-3, 4)]
        fetch_start = month_firsts[0]
        last_mf = month_firsts[-1]
        fetch_end = datetime.date(last_mf.year, last_mf.month, calendar.monthrange(last_mf.year, last_mf.month)[1])

        with SnowflakeClient() as sf:
            rows, cols = sf.execute("""
                SELECT
                    SNAPSHOT_DATE,
                    ORDERS,
                    ORDERS_N_MINUS_1,
                    ORDERS_TARGET
                FROM PROD_CARSUPERMARKET_DB.product_slt_kpis.daily_summary
                WHERE SNAPSHOT_DATE >= %s AND SNAPSHOT_DATE <= %s
                ORDER BY SNAPSHOT_DATE
            """, [str(fetch_start), str(fetch_end)])

        col_map = {c.upper(): i for i, c in enumerate(cols)}

        def _col(r, name):
            i = col_map.get(name)
            return r[i] if i is not None else None

        by_date: dict[str, dict] = {}
        for r in rows:
            d = _col(r, 'SNAPSHOT_DATE')
            d = d.isoformat() if isinstance(d, datetime.date) else str(d)[:10]
            by_date[d] = {
                'orders':         _col(r, 'ORDERS'),
                'orders_n1':      _col(r, 'ORDERS_N_MINUS_1'),
                'orders_target':  _col(r, 'ORDERS_TARGET'),
            }

        def _int(val):
            try:
                return int(val) if val is not None else None
            except (TypeError, ValueError):
                return None

        def _sum_month(mf: datetime.date, field: str):
            """Sum field over the month; None if the month has no data at all."""
            days_in = calendar.monthrange(mf.year, mf.month)[1]
            vals = [_int(by_date.get(datetime.date(mf.year, mf.month, day).isoformat(), {}).get(field)) for day in range(1, days_in + 1)]
            known = [v for v in vals if v is not None]
            return sum(known) if known else None

        month_abbrs = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

        monthly_labels        = [month_abbrs[mf.month - 1] + ' ' + str(mf.year)[2:] for mf in month_firsts]
        monthly_orders        = [_sum_month(mf, 'orders') if mf < first_of_this_month else None for mf in month_firsts]
        monthly_orders_ly     = [_sum_month(mf, 'orders_n1')     for mf in month_firsts]
        monthly_orders_target = [_sum_month(mf, 'orders_target') for mf in month_firsts]

        return {
            "status": "ok",
            "today": today.isoformat(),
            "monthly_labels": monthly_labels,
            "monthly_orders": monthly_orders,
            "monthly_orders_ly": monthly_orders_ly,
            "monthly_orders_target": monthly_orders_target,
        }
    except Exception as e:
        logger.error(f"Weekly Business Review data failed: {e}")
        return {"status": "error", "message": str(e)}
