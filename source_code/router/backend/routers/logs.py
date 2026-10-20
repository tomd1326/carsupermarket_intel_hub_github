"""Logs page: Snowflake queries, SharePoint file activity, and warnings and
errors, stored by utils/app_log.py. Admin only.

Not in the page registry on purpose (see page_registry.py): like Settings, it
must never become something an admin can grant to another user. The page and
every data endpoint check for the admin themselves.

Snowflake impact (bytes and partitions scanned) is looked up on demand, for the
rows on screen only, from INFORMATION_SCHEMA.QUERY_HISTORY_BY_USER. The app's
Snowflake role cannot read SNOWFLAKE.ACCOUNT_USAGE (checked 2026-10-20), so
that is limited to the last 7 days of the app user's own queries. Credits are
not available from this source.
"""
import datetime
import logging
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from utils import app_log
from utils.db_utils import get_database_connection, get_settings, save_settings_partial
from utils.snowflake_client import SnowflakeClient
from .core import BASE_URL, PageRedirect, get_current_user, is_admin, templates

logger = logging.getLogger(__name__)

router = APIRouter()

_QUERY_ID_RE = re.compile(r'^[0-9a-fA-F-]{36}$')
_MAX_IMPACT_IDS = 200
_SOURCES = (app_log.SOURCE_SNOWFLAKE, app_log.SOURCE_SHAREPOINT, app_log.SOURCE_APP)


def _page_admin(request: Request) -> str:
    user = get_current_user(request)
    if not user:
        raise PageRedirect(BASE_URL + "/login")
    if not is_admin(user):
        raise PageRedirect(BASE_URL + "/home")
    return user


def _api_admin(request: Request) -> str:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    if not is_admin(user):
        raise HTTPException(status_code=403)
    return user


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, user: str = Depends(_page_admin)):
    with get_database_connection() as conn:
        app_log._ensure_table(conn)
        try:
            app_log.cleanup(conn)
        except Exception as e:
            conn.rollback()
            logger.warning("Log cleanup failed: %s", e)
        settings = get_settings(conn)
    return templates.TemplateResponse("logs.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "retention_days": settings['logs_retention_days'],
        "page_size": settings['logs_page_size'],
    })


def _like(text: str) -> str:
    return '%' + text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def _where(source: str, level: str, page: str, task: str, q: str,
           date_from: str, date_to: str, min_duration_ms: Optional[int],
           failed_only: bool, table: str = ''):
    clauses, params = [], []
    if table:
        clauses.append("tables ILIKE %s")
        params.append(_like(table))
    if source in _SOURCES:
        clauses.append("source = %s")
        params.append(source)
    if level in ('INFO', 'WARNING', 'ERROR'):
        clauses.append("level = %s")
        params.append(level)
    if page:
        clauses.append("page = %s")
        params.append(page)
    if task:
        clauses.append("task = %s")
        params.append(task)
    if q:
        clauses.append("message ILIKE %s")
        params.append(_like(q))
    if date_from:
        clauses.append("logged_at >= (%s::date)::timestamp AT TIME ZONE 'Europe/London'")
        params.append(date_from)
    if date_to:
        clauses.append("logged_at < ((%s::date + 1)::timestamp AT TIME ZONE 'Europe/London')")
        params.append(date_to)
    if min_duration_ms:
        clauses.append("duration_ms >= %s")
        params.append(min_duration_ms)
    if failed_only:
        clauses.append("success = FALSE")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _valid_date(value: str) -> str:
    try:
        datetime.date.fromisoformat(value)
        return value
    except (TypeError, ValueError):
        return ''


@router.get("/logs/data")
def logs_data(request: Request, source: str = '', level: str = '', page: str = '', task: str = '',
              q: str = '', date_from: str = '', date_to: str = '',
              min_duration_ms: Optional[int] = None, failed_only: bool = False, table: str = '',
              sort: str = 'newest', offset: int = 0, user: str = Depends(_api_admin)):
    where, params = _where(source, level, page, task, q.strip()[:200],
                           _valid_date(date_from), _valid_date(date_to),
                           min_duration_ms if min_duration_ms and min_duration_ms > 0 else None,
                           failed_only, table.strip()[:100])
    order = "duration_ms DESC NULLS LAST, id DESC" if sort == 'duration' else "id DESC"
    with get_database_connection() as conn:
        app_log._ensure_table(conn)
        page_size = get_settings(conn)['logs_page_size']
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM app_logs" + where, params)
            total = cur.fetchone()[0]
            cur.execute(
                "SELECT id, to_char(logged_at AT TIME ZONE 'Europe/London', 'DD/MM/YYYY HH24:MI:SS'),"
                " source, level, page, task, username, message, query_id, duration_ms,"
                " rows_returned, success, tables"
                " FROM app_logs" + where + " ORDER BY " + order + " LIMIT %s OFFSET %s",
                params + [page_size, max(0, offset)])
            rows = cur.fetchall()
    keys = ('id', 'time', 'source', 'level', 'page', 'task', 'user', 'message', 'query_id',
            'duration_ms', 'rows_returned', 'success', 'tables')
    return {"status": "ok", "total": total, "page_size": page_size,
            "rows": [dict(zip(keys, r)) for r in rows]}


@router.get("/logs/filters")
def logs_filters(request: Request, user: str = Depends(_api_admin)):
    """Pages and Snowflake tasks that appear in the stored log, for the dropdowns."""
    with get_database_connection() as conn:
        app_log._ensure_table(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT page FROM app_logs WHERE page IS NOT NULL ORDER BY page LIMIT 200")
            pages = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT DISTINCT task FROM app_logs WHERE source = %s AND task IS NOT NULL"
                        " ORDER BY task LIMIT 300", (app_log.SOURCE_SNOWFLAKE,))
            tasks = [r[0] for r in cur.fetchall()]
    return {"status": "ok", "pages": pages, "tasks": tasks}


class ImpactRequest(BaseModel):
    query_ids: List[str]


@router.post("/logs/impact")
def logs_impact(body: ImpactRequest, request: Request, user: str = Depends(_api_admin)):
    """Bytes and partitions scanned for the given Snowflake query ids.

    One query against INFORMATION_SCHEMA.QUERY_HISTORY_BY_USER, narrowed to the
    time the ids were logged. Runs only when the admin clicks the button, and is
    not itself written to the log."""
    ids = [i for i in dict.fromkeys(body.query_ids) if _QUERY_ID_RE.match(i)][:_MAX_IMPACT_IDS]
    if not ids:
        return {"status": "ok", "impact": {}}
    with get_database_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT EXTRACT(EPOCH FROM MIN(logged_at)), EXTRACT(EPOCH FROM MAX(logged_at))"
                        " FROM app_logs WHERE query_id = ANY(%s)", (ids,))
            first, last = cur.fetchone()
    if first is None:
        return {"status": "ok", "impact": {}}
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    start = max(int(first) - 600, int(now) - 7 * 24 * 3600 + 60)
    end = int(last) + 2 * 3600
    marks = ", ".join(["%s"] * len(ids))
    # SELECT * on purpose: the columns this function offers differ from the
    # ACCOUNT_USAGE view (it has no partition counts), so only the ones that
    # come back are used.
    sql = (
        "SELECT * FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY_BY_USER("
        f"USER_NAME => CURRENT_USER(), END_TIME_RANGE_START => TO_TIMESTAMP_LTZ({start}),"
        f" END_TIME_RANGE_END => TO_TIMESTAMP_LTZ({end}), RESULT_LIMIT => 10000))"
        f" WHERE query_id IN ({marks})"
    )
    try:
        with SnowflakeClient() as sf:
            rows, cols = sf.execute(sql, ids, log=False)
    except Exception as e:
        logger.warning("Logs page could not read Snowflake query history: %s", e)
        return {"status": "error", "message": str(e)[:300]}
    wanted = {'bytes_scanned': 'bytes_scanned',
              'percentage_scanned_from_cache': 'cache_percent',
              'rows_produced': 'rows_produced',
              'warehouse_size': 'warehouse_size',
              'execution_status': 'status',
              'partitions_scanned': 'partitions_scanned',
              'partitions_total': 'partitions_total'}
    names = [c.lower() for c in cols]
    id_pos = names.index('query_id')
    impact = {}
    for r in rows:
        item = {out: r[names.index(src)] for src, out in wanted.items() if src in names}
        # The percentage comes back as a fraction (0 to 1).
        if item.get('cache_percent') is not None:
            item['cache_percent'] = round(float(item['cache_percent']) * 100, 1)
        impact[r[id_pos]] = item
    return {"status": "ok", "impact": impact}


class LogSettings(BaseModel):
    retention_days: int
    page_size: int


@router.post("/logs/settings")
def logs_settings(body: LogSettings, request: Request, user: str = Depends(_api_admin)):
    if not (1 <= body.retention_days <= 365) or not (10 <= body.page_size <= 500):
        raise HTTPException(status_code=400, detail="Days kept must be 1 to 365 and rows per page 10 to 500.")
    with get_database_connection() as conn:
        save_settings_partial(conn, {
            'logs_retention_days': body.retention_days,
            'logs_page_size': body.page_size,
        })
    return {"status": "ok"}
