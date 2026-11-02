"""Every SharePoint file the app reads: one list, one reader.

The list is the sharepoint_sources table (db/17_sharepoint_sources.sql), edited
in Settings > SharePoint. Pages ask for a file's copy with get_copy(key) and
never call SharePoint themselves.

How a request gets its copy (get_copy):
  - Checked within the file's check interval: the saved copy is returned. No
    SharePoint call at all.
  - Otherwise SharePoint is asked when the file was last saved. That is one
    small call that does not open the workbook (about 1 second), capped at
    CHECK_TIMEOUT so a slow SharePoint cannot hold the page up.
      - Not saved since the copy was taken: the copy is returned.
      - Saved since: a refresh job is created and the copy is returned marked
        pending. The download runs in its own Lambda invocation (the worker
        route /api/sharepoint/refresh-run), because a thread is frozen once the
        response is sent, and a big workbook can take longer than the 29 second
        API Gateway limit. Callers that must have the newest data now (Buying
        Requirements' Refresh Data) pass wait=True and the download runs
        inside the request instead, as it always has there.
      - The check failed: the copy is returned with the error, and the next
        check is not until the interval has passed again. A failed check never
        triggers a full read, unless there is no copy at all.

Refresh jobs (sharepoint_refresh_jobs):
  - Only one queued or running job per file (a partial unique index); a second
    request attaches to the open job instead of starting another.
  - A worker claims its job in one UPDATE (queued -> running). A job delivered
    twice, or a Lambda retry, finds it already claimed and does nothing, so
    one job never downloads twice. (A Postgres advisory lock would do the same
    but hold a connection for the whole download, and this pool has only 5.)
  - Before downloading, the worker checks the last-saved time again. If the
    copy already matches it nothing is downloaded, so a repeated job is cheap.
  - A copy is only replaced by one with a later saved time.
  - A job left queued or running past the stale limits is marked failed, so the
    next check can start a new one.
  - The worker route only downloads. It never checks and dispatches, so a
    worker can never start another worker.

Copies are kept in memory and in S3 under a shared key, so local, dev and prod
reuse each other's downloads (the files are the same for every environment).
"""
import datetime
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace

import psycopg2
import psycopg2.extras

from utils.db_utils import get_prod_pricer_connection
from utils.sharepoint_client import SharePointClient
from utils import workbook_values

logger = logging.getLogger(__name__)

_S3_BUCKET = "aa-raw-sources-uk"
_S3_PREFIX = "applications/pricing/uk-vehicle-list-evaluator/shared/cache/sharepoint/"

# Seconds allowed for the last-saved check during a page request.
CHECK_TIMEOUT = 5
# How long the file list (and its status columns) is reused in memory.
_SOURCES_TTL = 30
# A job queued this long never started (its invocation was lost); one running
# this long has outlived the Lambda's 600s timeout (terraform aws_lambda_timeout_1).
QUEUED_STALE_SECONDS = 180
RUNNING_STALE_SECONDS = 900
# How long a request that must wait for a download waits for one already
# running elsewhere. Kept under the API Gateway limit.
WAIT_SECONDS = 25

FILE_TYPES = {
    "kpi_daily_metrics": "KPI Daily Metrics",
    "daily_buyers_sheet": "Daily Buyers Sheet",
    "cars_bought": "Cars Bought",
    "wbr_recon": "Weekly Business Review Recon",
    "wbr_volume": "Weekly Business Review Volume",
}
KPI_FILE_TYPE = "kpi_daily_metrics"

# The Buying Requirements and Weekly Business Review files. Their code finds them
# by key, so they can be edited and switched off but not deleted, and a new file
# of any of these types would need code.
FIXED_KEYS = {"daily_buyers": "daily_buyers_sheet", "cars_bought": "cars_bought",
              "wbr_recon": "wbr_recon", "wbr_volume": "wbr_volume"}

# Header text the Buying Requirements code looks for, per field. Settings can
# replace any field's list; a field left blank there uses these.
DEFAULT_HEADERS = {
    "daily_buyers_sheet": {
        "REG": ["REG", "VRM", "REGISTRATION"],
        "CAP ID": ["CAP ID", "CAPID"],
        "RRP": ["RRP"],
        "RRP 0%": ["RRP 0%"],
        "MAKE": ["MAKE"],
        "MODEL": ["MODEL"],
        "DERIVATIVE": ["DERIVATIVE", "VARIANT"],
        "SUPPLIER": ["SUPPLIER"],
        "MILEAGE": ["MILEAGE"],
        "SIV": ["SIV", "PURCHASE PRICE", "PURCHASE_PRICE"],
        "DOR": ["DOR"],
        "CAP CLEAN LIVE": ["CAP CLEAN LIVE", "CAP LIVE CLEAN", "CAP CLEAN"],
        "AT RRP": ["AUTOTRADER RRP", "AT RRP", "AT PTM"],
        "AT RATING": ["RR"],
    },
    "cars_bought": {
        "REG": ["REG"],
        "CAP ID": ["CAP ID"],
        "RRP": ["RRP"],
        "RRP 0%": ["0%RRP"],
        "MAKE": ["MAKE"],
        "MODEL": ["MODEL"],
        "VARIANT": ["VARIANT"],
        "SUPPLIER": ["SUPPLIER"],
        "MILEAGE": ["MILEAGE"],
        "DATE": ["DATE"],
        "BUYER": ["BUYERS"],
        "SIV": ["SIV", "PURCHASE PRICE", "PURCHASE_PRICE"],
        "DOR": ["DOR"],
        "CAP CLEAN LIVE": ["CAP CLEAN LIVE", "CAP LIVE CLEAN", "CAP CLEAN"],
        "AT RRP": ["AUTOTRADER RRP", "AT RRP", "AT PTM"],
        "AT RATING": ["AT RATING"],
    },
    "wbr_recon": {
        "Date": ["Date"],
        "Sites Live": ["Sites Live"],
        "Hull Sent to Trans.": ["Hull Sent to Trans."],
        "Goole Sent to Trans.": ["Goole Sent to Trans."],
        "Total Live": ["Total Live"],
        "Cars Bought": ["Cars Bought"],
        "DOC Orders": ["DOC Orders"],
    },
    # One column per week: rows are found by their labels (wbr_report_data.volume_frame).
    "wbr_volume": {},
}
# Fields the purchase counts cannot be worked out without.
REQUIRED_HEADERS = ("REG", "CAP ID", "RRP", "RRP 0%")

# The files read before Settings > SharePoint existed. Added once, the first
# time the table is created; after that they are ordinary rows.
_SEED_SOURCES = [
    {
        "source_key": "spend", "name": "Daily Spend and Enquiries", "file_type": KPI_FILE_TYPE,
        "url": "https://carsmkt.sharepoint.com/teams/marketing/Reports/Daily%20Spend%20and%20Enquiries.xlsx",
        "read_kind": "table", "sheet_or_table": "Table1", "max_col": None, "max_row": None,
        "date_header": "Date", "column_names": {}, "check_minutes": 15, "sort_order": 10,
    },
    {
        "source_key": "at_ctr", "name": "Autotrader CTR", "file_type": KPI_FILE_TYPE,
        "url": ("https://carsmkt.sharepoint.com/teams/marketing/Shared%20Documents/"
                "Classifieds/AutoTrader/Autotrader%20CTR%20v3.xlsx"),
        "read_kind": "sheet", "sheet_or_table": "at_data", "max_col": "G", "max_row": 5000,
        "date_header": "Dynamic Date",
        # The metric names charts were saved with before this list existed.
        "column_names": {
            "Total search appearances": "at_search_appearances_total",
            "PPC search appearances": "at_search_appearances_ppc",
            "Market Extension natural search appearances": "at_search_appearances_natural",
            "Total advert views": "at_advert_views_total",
            "PPC advert views": "at_advert_views_ppc",
            "Market Extension natural advert views": "at_advert_views_natural",
        },
        "check_minutes": 15, "sort_order": 20,
    },
    {
        "source_key": "daily_buyers", "name": "Daily Buyers Sheet", "file_type": "daily_buyers_sheet",
        "url": ("https://carsmkt.sharepoint.com/sites/BuyingDrive/_layouts/15/Doc.aspx?sourcedoc="
                "%7BBCCED8DD-72BE-4FB6-808C-D2769789C025%7D&file=NEW%20DAILY%20BUYERS%20SHEET.xlsx"
                "&action=default&mobileredirect=true"),
        "read_kind": "sheet", "sheet_or_table": "Today", "max_col": "S", "max_row": 500,
        "date_header": None, "column_names": {}, "check_minutes": 30, "sort_order": 30,
    },
    {
        "source_key": "cars_bought", "name": "Cars Bought", "file_type": "cars_bought",
        "url": ("https://carsmkt.sharepoint.com/sites/CarSupermarketOperationalData/_layouts/15/Doc.aspx?"
                "sourcedoc=%7BC6F7895C-7AA8-48A7-B472-69D4B326E0A2%7D&file=CARS%20BOUGHT%202026.xlsx"
                "&action=default&mobileredirect=true"),
        "read_kind": "sheet", "sheet_or_table": "TO GO ON", "max_col": "S", "max_row": 2000,
        "date_header": None, "column_names": {}, "check_minutes": 30, "sort_order": 40,
    },
]

# Added later than the list above, so each is added on its own if it is missing.
# An edit made in Settings is never overwritten.
_ADDED_SOURCES = [
    {
        "source_key": "wbr_recon", "name": "WBR Reconditioning", "file_type": "wbr_recon",
        "url": "https://carsmkt.sharepoint.com/:x:/t/marketing/IQDgqQf3FIC3R7d3FES37sOVAcXHw-QpVoxyhYn0875Wm_4",
        "read_kind": "sheet", "sheet_or_table": "Data", "max_col": "Z", "max_row": 5000,
        "date_header": None, "column_names": {}, "check_minutes": 60, "sort_order": 50,
    },
    {
        "source_key": "wbr_volume", "name": "WBR Weekly Performance", "file_type": "wbr_volume",
        "url": "https://carsmkt.sharepoint.com/:x:/s/CarSupermarketOperationalData/IQD9rFZdVdPCR58KuzXcu6CzAba6UO53zOUFM16eN41W2AA",
        # Newest week on the left. CL reaches about 21 months back, enough for N-1.
        "read_kind": "sheet", "sheet_or_table": "Performance", "max_col": "CL", "max_row": 200,
        "date_header": None, "column_names": {}, "check_minutes": 60, "sort_order": 60,
    },
]

_SETTING_COLUMNS = ("name", "url", "read_kind", "sheet_or_table", "max_col", "max_row", "date_header",
                    "column_names", "check_minutes", "enabled", "sort_order")
_STATUS_COLUMNS = ("last_checked_at", "last_saved_at", "copy_saved_at", "last_downloaded_at",
                   "last_read_seconds", "last_row_count", "last_error", "last_error_at")


# ── Small helpers ────────────────────────────────────────────────────────────

def _ts(value):
    """SharePoint's ISO time, a datetime or None, as an aware datetime or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def _epoch(value) -> float:
    ts = _ts(value)
    return ts.timestamp() if ts else 0.0


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _short(e: Exception) -> str:
    return str(e).splitlines()[0][:300] if str(e) else type(e).__name__


def fingerprint(src: dict) -> str:
    """Which part of the file a copy holds. A copy taken with different
    settings (another link, sheet or range) is not used."""
    # "calamine": copies made by the Graph cell reads (dates as serial
    # numbers) are not reused once the file is read here instead.
    parts = ["calamine", src.get("url"), src.get("read_kind"), src.get("sheet_or_table"),
             src.get("max_col"), src.get("max_row")]
    return hashlib.sha1(json.dumps(parts, default=str).encode()).hexdigest()[:16]


class Copy:
    """A file's values as last downloaded, with what is known about them."""

    def __init__(self, values=None, modified=None, downloaded_at=None, fp=None,
                 pending=False, error=None):
        self.values = values            # list of rows; row 0 the first row read
        self.modified = modified        # SharePoint's saved time for these values (ISO)
        self.downloaded_at = downloaded_at  # epoch seconds
        self.fingerprint = fp
        self.pending = pending          # a newer version is being downloaded
        self.error = error              # why this copy may be out of date

    @property
    def saved_at(self):
        return _ts(self.modified)

    def but(self, **changes) -> "Copy":
        out = Copy(self.values, self.modified, self.downloaded_at, self.fingerprint, self.pending, self.error)
        for k, v in changes.items():
            setattr(out, k, v)
        return out


# ── Tables and the file list ─────────────────────────────────────────────────

_tables_ready = False
_tables_lock = threading.Lock()


def _insert_source(cur, s: dict) -> None:
    cur.execute("""
        INSERT INTO sharepoint_sources (source_key, name, file_type, url, read_kind,
            sheet_or_table, max_col, max_row, date_header, column_names,
            check_minutes, sort_order, updated_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'setup')
        ON CONFLICT (source_key) DO NOTHING
    """, (s["source_key"], s["name"], s["file_type"], s["url"], s["read_kind"],
          s["sheet_or_table"], s["max_col"], s["max_row"], s["date_header"],
          json.dumps(s["column_names"]), s["check_minutes"], s["sort_order"]))


def _ensure_tables(conn) -> None:
    """Create the two tables and add the existing files, once per process.
    Mirrors db/17_sharepoint_sources.sql."""
    global _tables_ready
    if _tables_ready:
        return
    with _tables_lock:
        if _tables_ready:
            return
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sharepoint_sources (
                    source_key         VARCHAR(50)  PRIMARY KEY,
                    name               VARCHAR(100) NOT NULL,
                    file_type          VARCHAR(30)  NOT NULL,
                    url                TEXT         NOT NULL,
                    read_kind          VARCHAR(10)  NOT NULL DEFAULT 'sheet',
                    sheet_or_table     VARCHAR(100) NOT NULL,
                    max_col            VARCHAR(3),
                    max_row            INTEGER,
                    date_header        VARCHAR(100),
                    column_names       JSONB        NOT NULL DEFAULT '{}'::jsonb,
                    check_minutes      INTEGER      NOT NULL DEFAULT 15,
                    enabled            BOOLEAN      NOT NULL DEFAULT TRUE,
                    sort_order         INTEGER      NOT NULL DEFAULT 0,
                    updated_by         VARCHAR(100),
                    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                    last_checked_at    TIMESTAMPTZ,
                    last_saved_at      TIMESTAMPTZ,
                    copy_saved_at      TIMESTAMPTZ,
                    last_downloaded_at TIMESTAMPTZ,
                    last_read_seconds  NUMERIC(7,2),
                    last_row_count     INTEGER,
                    last_error         TEXT,
                    last_error_at      TIMESTAMPTZ
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS sharepoint_refresh_jobs (
                    job_id        VARCHAR(32)  PRIMARY KEY,
                    source_key    VARCHAR(50)  NOT NULL,
                    status        VARCHAR(10)  NOT NULL,
                    reason        VARCHAR(20),
                    file_saved_at TIMESTAMPTZ,
                    requested_by  VARCHAR(100),
                    requested_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                    started_at    TIMESTAMPTZ,
                    finished_at   TIMESTAMPTZ,
                    message       TEXT
                )
            """)
            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS sharepoint_refresh_jobs_one_open
                    ON sharepoint_refresh_jobs (source_key) WHERE status IN ('queued', 'running')
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS sharepoint_refresh_jobs_recent
                    ON sharepoint_refresh_jobs (source_key, requested_at DESC)
            """)
            cur.execute("SELECT COUNT(*) FROM sharepoint_sources")
            if cur.fetchone()[0] == 0:
                for s in _SEED_SOURCES:
                    _insert_source(cur, s)
            for s in _ADDED_SOURCES:
                _insert_source(cur, s)
        conn.commit()
        _tables_ready = True


_sources_cache = {"rows": None, "at": 0.0}
_sources_lock = threading.Lock()


def list_sources(fresh: bool = False) -> list:
    """Every file in Settings > SharePoint, settings and status, in list order."""
    with _sources_lock:
        rows = _sources_cache["rows"]
        if not fresh and rows is not None and time.monotonic() - _sources_cache["at"] < _SOURCES_TTL:
            return rows
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM sharepoint_sources ORDER BY sort_order, name")
            rows = [dict(r) for r in cur.fetchall()]
        conn.commit()
    with _sources_lock:
        _sources_cache["rows"] = rows
        _sources_cache["at"] = time.monotonic()
    return rows


def get_source(key: str, fresh: bool = False) -> dict:
    for src in list_sources(fresh):
        if src["source_key"] == key:
            return src
    raise KeyError(f"No SharePoint file '{key}' in Settings > SharePoint")


def sources_of_type(file_type: str, enabled_only: bool = True) -> list:
    return [s for s in list_sources() if s["file_type"] == file_type and (s["enabled"] or not enabled_only)]


def check_seconds(key: str, default: int = 1800) -> int:
    """The file's check interval in seconds."""
    try:
        return max(1, int(get_source(key)["check_minutes"])) * 60
    except Exception as e:
        logger.warning("SharePoint check interval for %s unavailable, using %ss: %s", key, default, e)
        return default


def header_names(key: str) -> dict:
    """Field -> header texts for a Buying Requirements file: the code's
    defaults, with any field set in Settings replacing its default list."""
    try:
        src = get_source(key)
    except Exception as e:
        logger.warning("SharePoint header names for %s unavailable, using defaults: %s", key, e)
        return {f: list(v) for f, v in DEFAULT_HEADERS.get(FIXED_KEYS.get(key, ""), {}).items()}
    out = {f: list(v) for f, v in DEFAULT_HEADERS.get(src["file_type"], {}).items()}
    for field, names in (src.get("column_names") or {}).items():
        names = [str(n).strip() for n in (names if isinstance(names, list) else [names]) if str(n).strip()]
        if field in out and names:
            out[field] = names
    return out


def _update_status(key: str, **cols) -> None:
    """Write status columns for one file, and the copy held in memory."""
    bad = [c for c in cols if c not in _STATUS_COLUMNS]
    if bad:
        raise ValueError(f"Not status columns: {bad}")
    try:
        with get_prod_pricer_connection() as conn:
            _ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE sharepoint_sources SET {', '.join(f'{c} = %s' for c in cols)} WHERE source_key = %s",
                    (*cols.values(), key),
                )
            conn.commit()
    except Exception as e:
        logger.warning("SharePoint status update for %s failed: %s", key, e)
        return
    with _sources_lock:
        for row in _sources_cache["rows"] or []:
            if row["source_key"] == key:
                row.update(cols)


# ── Copies (memory and S3) ───────────────────────────────────────────────────

_copies: dict = {}
_copies_lock = threading.Lock()


def _s3():
    import boto3
    return boto3.client("s3", region_name="eu-west-1")


def _s3_key(key: str) -> str:
    return f"{_S3_PREFIX}{key}.json"


def _load_copy(src: dict, from_s3: bool = False):
    """The copy for src's current settings: memory, else S3, else None."""
    key, fp = src["source_key"], fingerprint(src)
    if not from_s3:
        with _copies_lock:
            held = _copies.get(key)
        if held is not None and held.fingerprint == fp:
            return held
    try:
        body = json.loads(_s3().get_object(Bucket=_S3_BUCKET, Key=_s3_key(key))["Body"].read())
    except Exception as e:
        if "NoSuchKey" not in str(e):
            logger.warning("SharePoint copy of %s could not be read from S3: %s", key, _short(e))
        return None
    if body.get("fingerprint") != fp:
        return None
    copy = Copy(body.get("values"), body.get("modified"), body.get("downloaded_at"), fp)
    with _copies_lock:
        current = _copies.get(key)
        if current is None or current.fingerprint != fp or _epoch(current.modified) <= _epoch(copy.modified):
            _copies[key] = copy
    return copy


def _save_copy(key: str, copy: Copy) -> None:
    _s3().put_object(
        Bucket=_S3_BUCKET, Key=_s3_key(key), ContentType="application/json",
        Body=json.dumps({"modified": copy.modified, "downloaded_at": copy.downloaded_at,
                         "fingerprint": copy.fingerprint, "values": copy.values}, default=str).encode(),
    )
    with _copies_lock:
        _copies[key] = copy


def copy_saved_at(key: str):
    """SharePoint's saved time of the newest copy any server holds, as far as
    this server knows (the file list is re-read every _SOURCES_TTL seconds).
    Lets a page tell that a worker elsewhere has saved a newer copy."""
    try:
        src = get_source(key)
    except Exception:
        return None
    held = _copies.get(key)
    times = [t for t in (_ts(src.get("copy_saved_at")), held.saved_at if held else None) if t]
    return max(times) if times else None


# ── Refresh jobs ─────────────────────────────────────────────────────────────

def _expire_stale_jobs(cur, key: str | None = None) -> None:
    cur.execute(f"""
        UPDATE sharepoint_refresh_jobs
        SET status = 'failed', finished_at = NOW(),
            message = 'Did not finish - the worker stopped before it was done'
        WHERE status IN ('queued', 'running')
          AND ((status = 'queued' AND requested_at < NOW() - INTERVAL '{QUEUED_STALE_SECONDS} seconds')
            OR (status = 'running' AND COALESCE(started_at, requested_at) < NOW() - INTERVAL '{RUNNING_STALE_SECONDS} seconds'))
          {"AND source_key = %s" if key else ""}
    """, (key,) if key else ())


def open_job(key: str, reason: str, file_saved_at=None, requested_by: str | None = None) -> tuple[str, bool]:
    """(job_id, created). Attaches to the file's open job if there is one."""
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        try:
            with conn.cursor() as cur:
                _expire_stale_jobs(cur, key)
                cur.execute("""
                    INSERT INTO sharepoint_refresh_jobs (job_id, source_key, status, reason, file_saved_at, requested_by)
                    VALUES (%s, %s, 'queued', %s, %s, %s)
                    ON CONFLICT (source_key) WHERE status IN ('queued', 'running') DO NOTHING
                    RETURNING job_id
                """, (uuid.uuid4().hex, key, reason, _ts(file_saved_at), requested_by))
                row = cur.fetchone()
                if row:
                    cur.execute("DELETE FROM sharepoint_refresh_jobs WHERE requested_at < NOW() - INTERVAL '30 days'")
                    job_id, created = row[0], True
                else:
                    cur.execute("SELECT job_id FROM sharepoint_refresh_jobs "
                                "WHERE source_key = %s AND status IN ('queued', 'running')", (key,))
                    job_id, created = cur.fetchone()[0], False
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if created:
        logger.info("SharePoint %s: refresh job %s created (%s)", key, job_id, reason)
    else:
        logger.info("SharePoint %s: attached to refresh job %s already open", key, job_id)
    return job_id, created


def get_job(job_id: str):
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            _expire_stale_jobs(cur)
            cur.execute("SELECT * FROM sharepoint_refresh_jobs WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
        conn.commit()
    return dict(row) if row else None


def recent_jobs(keys: list | None = None, per_file: int = 1) -> dict:
    """{source_key: [newest job first]} for the given files (all when None)."""
    if keys is not None and not keys:
        return {}
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            _expire_stale_jobs(cur)
            cur.execute("""
                SELECT * FROM (
                    SELECT j.*, ROW_NUMBER() OVER (PARTITION BY source_key ORDER BY requested_at DESC) AS n
                    FROM sharepoint_refresh_jobs j
                    WHERE %s IS NULL OR source_key = ANY(%s)
                ) t WHERE n <= %s ORDER BY source_key, requested_at DESC
            """, (None if keys is None else 1, keys or [], per_file))
            rows = [dict(r) for r in cur.fetchall()]
        conn.commit()
    out: dict = {}
    for r in rows:
        r.pop("n", None)
        out.setdefault(r["source_key"], []).append(r)
    return out


def _set_job(job_id: str, status: str, message: str | None = None) -> None:
    """Finish an open job. A finished job is never changed again, so a
    repeated delivery cannot overwrite its result."""
    with get_prod_pricer_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE sharepoint_refresh_jobs SET status = %s, finished_at = NOW(), message = %s "
                        "WHERE job_id = %s AND status IN ('queued', 'running')",
                        (status, (message or "")[:1000], job_id))
        conn.commit()


def _claim_job(job_id: str) -> bool:
    """queued -> running, in one statement. Only one worker can win."""
    with get_prod_pricer_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE sharepoint_refresh_jobs SET status = 'running', started_at = NOW() "
                        "WHERE job_id = %s AND status = 'queued'", (job_id,))
            won = cur.rowcount == 1
        conn.commit()
    return won


def _download(src: dict, client: SharePointClient) -> list:
    """The file's rows: the whole file is downloaded and the sheet or table
    read from it (see utils/workbook_values)."""
    data = client.get_file_content_by_url(src["url"])
    if src["read_kind"] == "table":
        return workbook_values.table_values(data, src["sheet_or_table"])
    return workbook_values.sheet_values(data, src["sheet_or_table"],
                                        src.get("max_col") or "Z", int(src.get("max_row") or 2000))


def run_job(job_id: str) -> None:
    """Do one refresh job: the worker. Safe to call twice for the same job."""
    job = get_job(job_id)
    if not job or job["status"] != "queued":
        logger.info("SharePoint job %s: already %s, nothing to do", job_id, job["status"] if job else "gone")
        return
    if not _claim_job(job_id):
        logger.info("SharePoint job %s: claimed by another worker, nothing to do", job_id)
        return
    try:
        src = get_source(job["source_key"], fresh=True)
    except KeyError as e:
        _set_job(job_id, "failed", str(e))
        return
    _run_claimed(job, src)


def _run_claimed(job: dict, src: dict) -> None:
    job_id, key = job["job_id"], src["source_key"]
    try:
        client = SharePointClient()
        try:
            saved = client.get_last_modified_by_url(src["url"], timeout=30)
        except Exception as e:
            logger.warning("SharePoint %s: last-saved check before download failed: %s", key, _short(e))
            saved = None
        now = _now()
        current = _load_copy(src, from_s3=True)
        if job["reason"] != "reread" and current and saved and current.saved_at == _ts(saved):
            _update_status(key, copy_saved_at=_ts(saved), last_saved_at=_ts(saved), last_checked_at=now,
                           last_error=None, last_error_at=None)
            _set_job(job_id, "done", "Already up to date - nothing downloaded")
            logger.info("SharePoint %s: job %s found the copy already up to date", key, job_id)
            return

        started = time.monotonic()
        try:
            values = _download(src, client)
        except Exception as e:
            # Graph often fails once and works on a second try (504s seen on the
            # Daily Buyers Sheet). One retry only: the job is retried at the
            # next check anyway.
            logger.warning("SharePoint %s: download failed, trying once more: %s", key, _short(e))
            time.sleep(3)
            values = _download(src, client)
        seconds = time.monotonic() - started
        if saved is None:
            try:
                saved = client.get_last_modified_by_url(src["url"], timeout=30)
            except Exception:
                pass

        newest = _load_copy(src, from_s3=True)
        if newest and newest.saved_at and saved and newest.saved_at > _ts(saved):
            copy, message = newest, "A newer copy was already saved - kept that one"
        else:
            copy = Copy(values, saved, time.time(), fingerprint(src))
            _save_copy(key, copy)
            message = f"Downloaded {len(values)} rows in {seconds:.1f}s"
        now = _now()
        _update_status(key, copy_saved_at=copy.saved_at, last_saved_at=_ts(saved), last_checked_at=now,
                       last_downloaded_at=now, last_read_seconds=round(seconds, 2),
                       last_row_count=len(values), last_error=None, last_error_at=None)
        _set_job(job_id, "done", message)
        logger.info("SharePoint %s: job %s - %s", key, job_id, message)
    except Exception as e:
        message = f"Download failed: {_short(e)}"
        logger.error("SharePoint %s: job %s %s", key, job_id, message)
        _update_status(key, last_error=message, last_error_at=_now())
        try:
            _set_job(job_id, "failed", message)
        except Exception as e2:
            logger.error("SharePoint %s: could not mark job %s failed: %s", key, job_id, e2)


def _run_job_safely(job_id: str) -> None:
    try:
        run_job(job_id)
    except Exception as e:
        logger.exception("SharePoint job %s crashed: %s", job_id, e)


def _dispatch(job_id: str) -> None:
    """Start a job in its own Lambda invocation (a thread when running locally).
    The function name comes from the environment Lambda sets, so this works
    from a background thread with no request to hand."""
    function_name = os.getenv("AWS_LAMBDA_FUNCTION_NAME")
    if os.getenv("LOCAL_ENV") == "1" or not function_name:
        threading.Thread(target=_run_job_safely, args=(job_id,), daemon=True,
                         name=f"sp-job-{job_id[:8]}").start()
        return
    try:
        from routers.core import BASE_URL, worker_secret
        from utils.lambda_http import LambdaRequestsManager
        LambdaRequestsManager().simulate_http(
            aws_context=SimpleNamespace(function_name=function_name),
            path=BASE_URL + "/api/sharepoint/refresh-run",
            method="POST",
            data={"job_id": job_id, "secret": worker_secret()},
        )
    except Exception as e:
        logger.error("SharePoint job %s could not be started: %s", job_id, e)
        _set_job(job_id, "failed", f"Could not start the download: {_short(e)}")


def _wait_for_job(job_id: str, timeout: float):
    deadline = time.monotonic() + timeout
    job = get_job(job_id)
    while job and job["status"] in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(1)
        job = get_job(job_id)
    return job


# ── Reading ──────────────────────────────────────────────────────────────────

_key_locks: dict = {}
_key_locks_guard = threading.Lock()
_last_checked: dict = {}   # key -> epoch seconds this server last checked


def _key_lock(key: str) -> threading.Lock:
    with _key_locks_guard:
        return _key_locks.setdefault(key, threading.Lock())


def get_copy(key: str, check: str = "auto", wait: bool = False, wait_if_missing: bool = False,
             requested_by: str | None = None) -> Copy:
    """The file's copy. check='now' ignores the interval (Load Data, Refresh
    Data, Check Now). wait=True downloads a changed file inside this request;
    wait_if_missing=True does so only when there is no copy at all. Otherwise a
    changed or missing file is downloaded by the worker and the copy comes back
    with pending=True (values None when there is no copy yet).

    Never raises for SharePoint trouble: problems come back in .error."""
    src = get_source(key)
    if not src["enabled"]:
        return Copy(error=f"{src['name']} is switched off in Settings > SharePoint")
    copy = _load_copy(src)
    error = saved = None
    with _key_lock(key):
        last = max(_last_checked.get(key, 0.0), _epoch(src.get("last_checked_at")))
        due = check == "now" or copy is None or time.time() - last >= max(1, int(src["check_minutes"])) * 60
        if not due:
            return copy
        _last_checked[key] = time.time()
        try:
            saved = SharePointClient().get_last_modified_by_url(src["url"], timeout=20 if wait else CHECK_TIMEOUT)
        except Exception as e:
            error = f"Could not check {src['name']} for changes: {_short(e)}"
        now = _now()
        if error:
            logger.warning("SharePoint %s: %s", key, error)
            _update_status(key, last_checked_at=now, last_error=error, last_error_at=now)
        else:
            _update_status(key, last_checked_at=now, last_saved_at=_ts(saved))

    if copy is not None:
        if error:
            return copy.but(error=error)
        if copy.saved_at and copy.saved_at == _ts(saved):
            logger.info("SharePoint %s: not saved since the copy was taken (%s)", key, saved)
            return copy

    try:
        job_id, created = open_job(key, "missing" if copy is None else "changed", saved, requested_by)
        if wait or (copy is None and wait_if_missing):
            if created:
                run_job(job_id)
            job = _wait_for_job(job_id, WAIT_SECONDS)
            fresh = _load_copy(src, from_s3=True) or copy
            failed = job and job["status"] == "failed"
            if fresh is None:
                return Copy(error=(job or {}).get("message") or f"{src['name']} is still downloading")
            return fresh.but(error=f"{src['name']}: {job['message']}" if failed else None,
                             pending=bool(job and job["status"] in ("queued", "running")))
        if created:
            _dispatch(job_id)
    except Exception as e:
        logger.error("SharePoint %s: refresh could not be started: %s", key, e)
        return (copy or Copy()).but(error=f"{src['name']}: refresh could not be started: {_short(e)}")
    return (copy or Copy()).but(pending=True, error=error)


def check_due(keys: list | None = None) -> dict:
    """Run the get_copy check for every switched-on file (or the given ones),
    together. Used by the home page warmer and by open pages."""
    try:
        sources = [s for s in list_sources() if s["enabled"] and (keys is None or s["source_key"] in keys)]
    except Exception as e:
        logger.warning("SharePoint check of all files failed: %s", e)
        return {}
    if not sources:
        return {}

    def one(src):
        try:
            return src["source_key"], get_copy(src["source_key"])
        except Exception as e:
            logger.warning("SharePoint %s: check failed: %s", src["source_key"], e)
            return src["source_key"], Copy(error=_short(e))

    # At most 4 at once: each check writes its status through the 5-connection
    # prod pricer pool.
    with ThreadPoolExecutor(max_workers=min(4, len(sources))) as pool:
        return dict(pool.map(one, sources))


def status(keys: list | None = None) -> list:
    """What a page or Settings shows for each file: settings, status columns
    and the newest job. Read fresh from the database."""
    sources = [s for s in list_sources(fresh=True) if keys is None or s["source_key"] in keys]
    jobs = recent_jobs([s["source_key"] for s in sources], per_file=1)
    out = []
    for s in sources:
        job = (jobs.get(s["source_key"]) or [None])[0]
        out.append({**_serialise(s), "job": _serialise(job) if job else None})
    return out


def _serialise(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime.datetime):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = float(v)
        else:
            out[k] = v
    return out


# ── Settings > SharePoint: editing the list ──────────────────────────────────

def _clean(payload: dict, existing: dict | None) -> dict:
    """Checked and tidied settings for one file. Raises ValueError with a
    message for the page."""
    file_type = existing["file_type"] if existing else str(payload.get("file_type") or KPI_FILE_TYPE)
    if not existing and file_type != KPI_FILE_TYPE:
        raise ValueError("Only KPI Daily Metrics files can be added. The other files "
                         "need code to read their rows.")
    name = str(payload.get("name") or "").strip()
    if not 1 <= len(name) <= 100:
        raise ValueError("Name is required (up to 100 characters)")
    url = str(payload.get("url") or "").strip()
    if not re.match(r"^https://[^/]+\.sharepoint\.com/", url):
        raise ValueError("Link must be a SharePoint link starting https://...sharepoint.com/")
    read_kind = str(payload.get("read_kind") or "sheet")
    if read_kind not in ("sheet", "table"):
        raise ValueError("Read must be a sheet or a table")
    sheet = str(payload.get("sheet_or_table") or "").strip()
    if not 1 <= len(sheet) <= 100:
        raise ValueError("Sheet or table name is required")
    max_col = max_row = None
    if read_kind == "sheet":
        max_col = str(payload.get("max_col") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{1,3}", max_col):
            raise ValueError("Last column must be a column letter, e.g. G")
        try:
            max_row = int(payload.get("max_row"))
        except (TypeError, ValueError):
            raise ValueError("Last row must be a number")
        if not 2 <= max_row <= 100000:
            raise ValueError("Last row must be between 2 and 100,000")
    try:
        check_minutes = int(payload.get("check_minutes"))
    except (TypeError, ValueError):
        raise ValueError("Check interval must be a number of minutes")
    if not 1 <= check_minutes <= 1440:
        raise ValueError("Check interval must be between 1 and 1,440 minutes")
    try:
        sort_order = int(payload.get("sort_order") or 0)
    except (TypeError, ValueError):
        sort_order = 0

    raw_names = payload.get("column_names") or {}
    if not isinstance(raw_names, dict):
        raise ValueError("Column names are not in the right form")
    date_header = None
    if file_type == KPI_FILE_TYPE:
        date_header = str(payload.get("date_header") or "").strip()
        if not 1 <= len(date_header) <= 100:
            raise ValueError("Date column header is required")
        column_names = {}
        for header, metric in raw_names.items():
            header, metric = str(header).strip(), str(metric).strip()
            if header and metric:
                if len(metric) > 150:
                    raise ValueError(f"Metric name for '{header}' is too long")
                column_names[header] = metric
    else:
        column_names = {}
        allowed = DEFAULT_HEADERS[file_type]
        for field, names in raw_names.items():
            if field not in allowed:
                raise ValueError(f"Unknown field: {field}")
            names = [str(n).strip() for n in (names if isinstance(names, list) else str(names).split(","))]
            names = [n for n in names if n]
            if names:
                column_names[field] = names
    return {
        "name": name, "url": url, "read_kind": read_kind, "sheet_or_table": sheet,
        "max_col": max_col, "max_row": max_row, "date_header": date_header,
        "column_names": column_names, "check_minutes": check_minutes,
        "enabled": bool(payload.get("enabled", True)), "sort_order": sort_order,
        "file_type": file_type,
    }


def save_source(payload: dict, user: str) -> str:
    """Add a file (no source_key) or update one. Returns its key."""
    key = str(payload.get("source_key") or "").strip()
    existing = None
    if key:
        existing = get_source(key, fresh=True)
    values = _clean(payload, existing)
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        try:
            with conn.cursor() as cur:
                if existing:
                    cur.execute(f"""
                        UPDATE sharepoint_sources
                        SET {', '.join(f'{c} = %s' for c in _SETTING_COLUMNS)}, updated_by = %s, updated_at = NOW()
                        WHERE source_key = %s
                    """, (*[json.dumps(values[c]) if c == "column_names" else values[c] for c in _SETTING_COLUMNS],
                          user, key))
                else:
                    base = re.sub(r"[^a-z0-9]+", "_", values["name"].lower()).strip("_")[:40] or "file"
                    cur.execute("SELECT source_key FROM sharepoint_sources WHERE source_key LIKE %s", (base + "%",))
                    taken = {r[0] for r in cur.fetchall()}
                    key = base
                    n = 2
                    while key in taken:
                        key = f"{base}_{n}"
                        n += 1
                    cur.execute(f"""
                        INSERT INTO sharepoint_sources (source_key, file_type, {', '.join(_SETTING_COLUMNS)}, updated_by)
                        VALUES (%s, %s, {', '.join(['%s'] * len(_SETTING_COLUMNS))}, %s)
                    """, (key, values["file_type"],
                          *[json.dumps(values[c]) if c == "column_names" else values[c] for c in _SETTING_COLUMNS],
                          user))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    list_sources(fresh=True)
    logger.info("SharePoint file %s %s by %s", key, "updated" if existing else "added", user)
    return key


def delete_source(key: str, user: str) -> None:
    if key in FIXED_KEYS:
        raise ValueError("This file is used by another page and cannot be deleted. Switch it off instead.")
    get_source(key, fresh=True)
    with get_prod_pricer_connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sharepoint_refresh_jobs WHERE source_key = %s", (key,))
                cur.execute("DELETE FROM sharepoint_sources WHERE source_key = %s", (key,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    try:
        _s3().delete_object(Bucket=_S3_BUCKET, Key=_s3_key(key))
    except Exception as e:
        logger.warning("SharePoint copy of %s could not be deleted from S3: %s", key, e)
    with _copies_lock:
        _copies.pop(key, None)
    list_sources(fresh=True)
    logger.info("SharePoint file %s deleted by %s", key, user)


def request_refresh(key: str, user: str, reread: bool = False) -> dict:
    """Check Now (download only if saved since the copy) or Re-read Now
    (download whatever). Both go through a job."""
    if reread:
        job_id, created = open_job(key, "reread", None, user)
        if created:
            _dispatch(job_id)
        return {"job_id": job_id, "started": created, "pending": True}
    copy = get_copy(key, check="now", requested_by=user)
    return {"pending": copy.pending, "error": copy.error}


def test_access(url: str, read_kind: str, name: str) -> dict:
    """Whether the app can reach a file, without reading any cells: the
    last-saved check, then the workbook's sheet and table names."""
    url = (url or "").strip()
    if not re.match(r"^https://[^/]+\.sharepoint\.com/", url):
        return {"ok": False, "message": "Link must be a SharePoint link starting https://...sharepoint.com/"}
    client = SharePointClient()
    started = time.monotonic()
    try:
        info = client.get_file_info_by_url(url, timeout=15)
    except Exception as e:
        text = _short(e)
        if "403" in text or "401" in text:
            text = "SharePoint refused access - the app registration may not have access to this site. " + text
        elif "404" in text:
            text = "File not found - check the link. " + text
        return {"ok": False, "message": text}
    check_seconds_taken = time.monotonic() - started
    result = {
        "ok": True,
        "file_name": info.get("name"),
        "size_mb": round((info.get("size") or 0) / 1e6, 1),
        "saved_at": info.get("lastModifiedDateTime"),
        "check_seconds": round(check_seconds_taken, 2),
    }
    try:
        data = client.get_file_content_by_url(url, timeout=60)
        sheets, tables = workbook_values.sheet_names(data), workbook_values.table_names(data)
    except Exception as e:
        result["message"] = f"File found, but its sheets could not be listed: {_short(e)}"
        return result
    result["sheets"], result["tables"] = sheets, tables
    wanted = (name or "").strip()
    if wanted:
        names = tables if read_kind == "table" else sheets
        result["found"] = wanted in names
        result["message"] = (f"{'Table' if read_kind == 'table' else 'Sheet'} '{wanted}' "
                             f"{'found' if result['found'] else 'NOT found'}")
    return result
