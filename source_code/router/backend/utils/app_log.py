"""Stored log of Snowflake queries, SharePoint file activity, and warnings and
errors, shown on the admin-only Logs page.

Lambda writes logs to stdout (CloudWatch) and keeps nothing, so each entry is
also written to the app_logs table in Aurora. Three things are stored:
- every Snowflake query run through SnowflakeClient.execute: the start of the
  SQL, how long it took, rows returned, the Snowflake query id and any error
- log lines about the Daily Buyers Sheet / Cars Bought files (INFO and above)
- every other WARNING and ERROR log line

Writing a log row must never break the work that is being logged. Every write is
wrapped, has a short database timeout, and cannot log about its own failure
(a write that fails inside a write is dropped, not retried or logged again).

Query parameters (the values bound to %s) are never stored.
"""

import contextvars
import logging
import os
import re
import threading

logger = logging.getLogger(__name__)

SOURCE_SNOWFLAKE = 'snowflake'
SOURCE_SHAREPOINT = 'sharepoint'
SOURCE_APP = 'app'

# Log text is cut to these lengths so one huge statement or traceback cannot
# bloat the table.
_MAX_SQL_CHARS = 600
_MAX_MESSAGE_CHARS = 2000

# Old rows are deleted once per this many inserts in each process, and whenever
# the Logs page is opened. Lambda freezes background threads after the response,
# so cleanup runs inside a normal write instead of on a timer.
_CLEANUP_EVERY = 200

DEFAULT_RETENTION_DAYS = 30

# Substrings that mark a log line as relating to a SharePoint file. These match
# the prefixes the Daily Buyers Sheet and Cars Bought code paths in
# routers/buying_requirements.py already log with.
_SHAREPOINT_MATCH = (
    'daily buyers sheet',
    'cars bought',
    'dbs',
    'cb fetch',
    'sharepoint',
)

_ENV = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'

# page / task / user of the web request being served. A dict, so a dependency
# that runs in a worker thread (where a copy of the context is used) can still
# fill in the user for the code that runs afterwards.
_request_ctx: contextvars.ContextVar = contextvars.ContextVar('app_log_request', default=None)

# Schema-qualified names after FROM / JOIN. Only names with a dot are kept, so
# EXTRACT(YEAR FROM some_column) and CTE names are not mistaken for tables.
_TABLE_RE = re.compile(r'\b(?:FROM|JOIN)\s+((?:"?[A-Za-z_][\w$]*"?\.){1,2}"?[A-Za-z_][\w$]*"?)', re.IGNORECASE)
_MAX_TABLES_CHARS = 300

_in_write = threading.local()
_table_ready = False
_insert_count = 0
_count_lock = threading.Lock()


def begin_request(path: str) -> None:
    """Called at the start of each web request. The page is the first part of
    the path (/kpi/data -> kpi) and the task is the full path."""
    path = path or ''
    first = path.strip('/').split('/')[0] if path.strip('/') else 'home'
    _request_ctx.set({'page': first, 'task': path, 'user': None})


def note_user(user: str | None) -> None:
    ctx = _request_ctx.get()
    if ctx is not None and user:
        ctx['user'] = user


def current_context() -> dict:
    return _request_ctx.get() or {'page': None, 'task': None, 'user': None}


def query_tag() -> str:
    """Label sent to Snowflake as QUERY_TAG, so the same page and task show in
    Snowflake's own query history."""
    ctx = current_context()
    return f"vle|{ctx['page'] or '-'}|{ctx['task'] or '-'}"[:200]


def extract_tables(sql: str) -> str:
    """The SCHEMA.TABLE names a statement reads, upper case, comma separated.
    The database part is dropped, as every query reads the same database."""
    found = []
    for name in _TABLE_RE.findall(sql or ''):
        parts = name.replace('"', '').upper().split('.')[-2:]
        full = '.'.join(parts)
        if full not in found:
            found.append(full)
    return ', '.join(found)[:_MAX_TABLES_CHARS]


def _ensure_table(conn) -> None:
    global _table_ready
    if _table_ready:
        return
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS app_logs ("
            " id BIGSERIAL PRIMARY KEY,"
            " logged_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            " environment VARCHAR(10) NOT NULL DEFAULT '',"
            " source VARCHAR(20) NOT NULL,"
            " level VARCHAR(10) NOT NULL DEFAULT 'INFO',"
            " page VARCHAR(100),"
            " task VARCHAR(200),"
            " username VARCHAR(100),"
            " logger_name VARCHAR(100),"
            " message TEXT,"
            " query_id VARCHAR(60),"
            " duration_ms INTEGER,"
            " rows_returned INTEGER,"
            " success BOOLEAN)"
        )
        # Added after the table first shipped.
        cur.execute("ALTER TABLE app_logs ADD COLUMN IF NOT EXISTS tables VARCHAR(300)")
        cur.execute("CREATE INDEX IF NOT EXISTS app_logs_logged_at_idx ON app_logs (logged_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS app_logs_source_idx ON app_logs (source, logged_at DESC)")
    conn.commit()
    _table_ready = True


def ensure_table() -> None:
    from utils.db_utils import get_database_connection
    with get_database_connection() as conn:
        _ensure_table(conn)


def cleanup(conn) -> None:
    """Delete rows older than the retention setting (Logs page > Settings)."""
    days = DEFAULT_RETENTION_DAYS
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key = 'logs_retention_days'")
        row = cur.fetchone()
        if row:
            try:
                days = max(1, int(row[0]))
            except (TypeError, ValueError):
                pass
        cur.execute("DELETE FROM app_logs WHERE logged_at < NOW() - make_interval(days => %s)", (days,))
    conn.commit()


def _write(source: str, level: str, message: str, logger_name: str | None = None,
           query_id: str | None = None, duration_ms: int | None = None,
           rows_returned: int | None = None, success: bool | None = None,
           tables: str | None = None) -> None:
    """Insert one row. Never raises, never logs."""
    global _insert_count
    if getattr(_in_write, 'active', False):
        return
    _in_write.active = True
    try:
        from utils.db_utils import get_database_connection
        ctx = current_context()
        with get_database_connection() as conn:
            try:
                _ensure_table(conn)
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = 3000")
                    cur.execute(
                        "INSERT INTO app_logs (environment, source, level, page, task, username,"
                        " logger_name, message, query_id, duration_ms, rows_returned, success, tables)"
                        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (_ENV, source, level, ctx['page'], ctx['task'], ctx['user'], logger_name,
                         (message or '')[:_MAX_MESSAGE_CHARS], query_id, duration_ms, rows_returned, success,
                         tables),
                    )
                conn.commit()
                with _count_lock:
                    _insert_count += 1
                    due = _insert_count % _CLEANUP_EVERY == 0
                if due:
                    cleanup(conn)
            except Exception:
                conn.rollback()
                raise
    except Exception:
        # A log write must never break the code that logged.
        pass
    finally:
        _in_write.active = False


def record_snowflake(sql: str, query_id: str | None, duration_ms: int, rows_returned: int | None,
                     error: BaseException | None) -> None:
    """One Snowflake statement. The SQL text is cut short and its bound
    parameters are not stored."""
    text = re.sub(r'\s+', ' ', sql or '').strip()[:_MAX_SQL_CHARS]
    if error is not None:
        message = f"{text} -- ERROR: {error}"
    else:
        message = text
    _write(SOURCE_SNOWFLAKE, 'ERROR' if error is not None else 'INFO', message, 'snowflake_client',
           query_id=query_id, duration_ms=duration_ms, rows_returned=rows_returned,
           success=error is None, tables=extract_tables(sql))


class AppLogHandler(logging.Handler):
    """Stores SharePoint-file lines (INFO and above) and all other WARNING and
    ERROR lines. Never raises."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if getattr(_in_write, 'active', False):
                return
            message = record.getMessage()
            low = message.lower()
            if any(m in low for m in _SHAREPOINT_MATCH):
                source = SOURCE_SHAREPOINT
            elif record.levelno >= logging.WARNING:
                source = SOURCE_APP
            else:
                return
            if record.exc_info and record.exc_info[1] is not None:
                message = f"{message} -- {record.exc_info[1]}"
            _write(source, record.levelname, message, record.name)
        except Exception:
            pass


def install() -> None:
    """Attach the handler to the root logger, once."""
    root = logging.getLogger()
    if any(isinstance(h, AppLogHandler) for h in root.handlers):
        return
    handler = AppLogHandler()
    handler.setLevel(logging.INFO)
    root.addHandler(handler)
