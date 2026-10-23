"""DSE (Daily Spend & Enquiries) page: a month of daily figures laid out like
the Daily Spend and Enquiries spreadsheet, one row per metric.

The page starts empty. Each row is added in the page's Settings popup (admin
only) by choosing a COMPUTE_ or PRODUCT_ table and a column, and stored in
kpi_layout under 'dse_layout'. The reading reuses the KPI data source code
(utils/kpi_data_sources.py): its table and column lists, checks and SQL.
See utils/dse_metrics.py for the row fields and the month maths."""
import datetime
import json
import logging
import threading
import time

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse

from utils import dse_metrics as dm
from utils import kpi_data_sources as kds
from utils.snowflake_client import SnowflakeClient

from .core import BASE_URL, templates, _track, _uk_now, require_page, require_page_api, is_admin
from . import kpi
from .buying_requirements import _sf_cache_is_fresh

logger = logging.getLogger(__name__)

router = APIRouter()

PAGE_KEY = 'dse'
_LAYOUT_KEY = 'dse_layout'
_SETTINGS_KEY = 'dse_settings'
_NAMES_MAX = 2000
_CACHE_RANGES = 6

# (start, end) -> {"key", "result", "cached_at"}
_dse_cache: dict = {}
_dse_cache_lock = threading.Lock()


def _rows_saved() -> list:
    saved = kpi._read_kpi_layout_value(_LAYOUT_KEY)
    return saved if isinstance(saved, list) else []


def _settings_saved() -> dict:
    return dm.settings_with_defaults(kpi._read_kpi_layout_value(_SETTINGS_KEY))


def _clear_cache() -> None:
    with _dse_cache_lock:
        _dse_cache.clear()


@router.get("/dse", response_class=HTMLResponse)
def dse_page(request: Request, user: str = Depends(require_page(PAGE_KEY))):
    _track(request, 'page_view', '/dse')
    return templates.TemplateResponse("dse.html", {"request": request, "user": user,
                                                   "BASE_URL": BASE_URL, "is_admin": is_admin(user)})


def _read_group(rows: list, start: datetime.date, end: datetime.date) -> tuple[dict, dict]:
    """({row id: {date: value}}, {row id: error}) for rows sharing one table
    and shape, read in one query."""
    first = rows[0]
    entry = dm.to_entry(first)
    start, end = dm.fetch_range(start, end)
    metrics = list(dict.fromkeys(dm.metric_key(r) for r in rows))
    sql, params = kds.fetch_sql({**entry, "table": f"{kpi._VEHICLE_DB}.{entry['table']}"}, metrics, start, end)
    with SnowflakeClient() as sf:
        result, names = sf.execute(sql, params)
    if first["shape"] == "wide":
        dupes = dm.duplicate_dates(result)
        if dupes:
            shown = ", ".join(d.isoformat() for d in dupes[:3]) + (" ..." if len(dupes) > 3 else "")
            msg = (f"{first['table']} has more than one row for some dates ({shown}), "
                   "so it is not a one-row-per-date table")
            return {}, {r["id"]: msg for r in rows}
    by_date = kds.rows_to_by_date(entry, metrics, result, names)
    out = {}
    for r in rows:
        key = dm.metric_key(r)
        out[r["id"]] = {d: values.get(key) for d, values in by_date.items()}
    return out, {}


def _build(start: datetime.date, end: datetime.date, rows: list, settings: dict) -> dict:
    """Every row's values for the date range. A row that fails its checks or its
    read gets an error of its own; the other rows still show."""
    errors, groups = {}, {}
    columns_by_table = {}
    for r in rows:
        try:
            if r.get("table") not in columns_by_table:
                columns_by_table[r.get("table")] = kpi._source_columns(r.get("table"))
            err = dm.validate_row(r, columns_by_table[r.get("table")])
        except Exception as e:
            err = f"{r.get('label', '')}: {e}"
        if err:
            errors[r["id"]] = err
            continue
        groups.setdefault(dm.group_key(r), []).append(r)

    daily = {}
    for group in groups.values():
        try:
            values, group_errors = _read_group(group, start, end)
            daily.update(values)
            errors.update(group_errors)
        except Exception as e:
            logger.error(f"DSE read of {group[0]['table']} failed: {e}")
            errors.update({r["id"]: f"Could not read {group[0]['table']}: {e}" for r in group})

    out_rows = []
    for r in rows:
        item = {"id": r["id"], "error": errors.get(r["id"])}
        if r["id"] in daily:
            item.update(dm.row_values(daily[r["id"]], start, end, r["agg"], settings["compare_mode"]))
        out_rows.append(item)
    return {"start": start.isoformat(), "end": end.isoformat(),
            "days": [d.isoformat() for d in dm.range_days(start, end)], "values": out_rows}


@router.get("/dse/data")
def dse_data(request: Request, start: str | None = None, end: str | None = None, force: bool = False,
             user: str = Depends(require_page_api(PAGE_KEY))):
    """The saved rows and settings, and every row's figures for the date range.
    With no dates: the last Default Days days up to and including yesterday."""
    try:
        rows, settings = _rows_saved(), _settings_saved()
        yesterday = _uk_now().date() - datetime.timedelta(days=1)
        first, last, err = dm.parse_range(start, end, settings["default_days"], yesterday)
        if err:
            return {"status": "error", "message": err}
        key = json.dumps([rows, settings["compare_mode"]], sort_keys=True)
        with _dse_cache_lock:
            cached = _dse_cache.get((first, last))
        if force or not cached or cached["key"] != key or not _sf_cache_is_fresh(cached["cached_at"]):
            cached = {"key": key, "result": _build(first, last, rows, settings), "cached_at": time.time()}
            with _dse_cache_lock:
                _dse_cache[(first, last)] = cached
                for old in sorted(_dse_cache)[:-_CACHE_RANGES]:
                    _dse_cache.pop(old, None)
    except Exception as e:
        logger.error(f"DSE data failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "rows": rows, "settings": settings, "today": _uk_now().date().isoformat(),
            "cached_at": cached["cached_at"], **cached["result"]}


@router.get("/dse/tables")
def dse_tables(request: Request, user: str = Depends(require_page_api(PAGE_KEY))):
    """The tables a row can read from. Admin only; takes a few seconds."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the DSE rows"}
    try:
        return {"status": "ok", "tables": kpi._source_tables()}
    except Exception as e:
        logger.error(f"DSE tables read failed: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/dse/columns")
def dse_columns(request: Request, table: str, user: str = Depends(require_page_api(PAGE_KEY))):
    """One table's columns, read live so new columns show straight away. Admin only."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the DSE rows"}
    try:
        kpi._source_columns_cache.pop(table, None)
        return {"status": "ok", "columns": [{"column": c, "type": t} for c, t in kpi._source_columns(table).items()]}
    except Exception as e:
        logger.error(f"DSE columns read failed: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/dse/names")
def dse_names(request: Request, table: str, split_column: str, user: str = Depends(require_page_api(PAGE_KEY))):
    """The names in a one-row-per-name table's name column. Admin only."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the DSE rows"}
    try:
        columns = kpi._source_columns(table)
        if not columns:
            return {"status": "error", "message": f"Table {table} was not found"}
        if split_column not in columns:
            return {"status": "error", "message": f"{split_column} is not a column of {table}"}
        with SnowflakeClient() as sf:
            rows, _ = sf.execute(f"""
                SELECT DISTINCT {split_column}::VARCHAR
                FROM {kpi._VEHICLE_DB}.{table}
                WHERE {split_column} IS NOT NULL
                ORDER BY 1
                LIMIT {_NAMES_MAX}
            """)
        return {"status": "ok", "names": [str(r[0]) for r in rows]}
    except Exception as e:
        logger.error(f"DSE names read failed: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/dse/layout")
def dse_layout_save(request: Request, payload: dict, user: str = Depends(require_page_api(PAGE_KEY))):
    """The rows and the settings, all checked against the live tables before either is written."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the DSE rows"}
    payload = payload or {}
    rows, settings = payload.get("rows"), payload.get("settings")
    try:
        err = dm.validate_rows(rows) or dm.validate_settings(settings)
        if err:
            return {"status": "error", "message": err}
        for r in rows:
            err = dm.validate_row_shape(r)
            if err:
                return {"status": "error", "message": err}
        columns_by_table = {}
        for r in rows:
            if r["table"] not in columns_by_table:
                kpi._source_columns_cache.pop(r["table"], None)
                columns_by_table[r["table"]] = kpi._source_columns(r["table"])
            err = dm.validate_row(r, columns_by_table[r["table"]])
            if err:
                return {"status": "error", "message": err}
        saved_rows = [dm.clean_row(r) for r in rows]
        saved_settings = dm.settings_with_defaults(settings)
        kpi._write_kpi_layout_values({_LAYOUT_KEY: saved_rows, _SETTINGS_KEY: saved_settings})
        _clear_cache()
    except Exception as e:
        logger.error(f"DSE layout save failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "rows": saved_rows, "settings": saved_settings}
