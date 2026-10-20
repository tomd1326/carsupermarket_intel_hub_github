"""Home page (with background cache warming) and stock list page."""
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
import logging

from utils.snowflake_client import SnowflakeClient

from .core import (BASE_URL, templates, _track, get_current_user, require_page,
                   require_page_api, get_user_pages, is_admin, user_details)
from .page_registry import PAGES

logger = logging.getLogger(__name__)

router = APIRouter()
from . import kpi
from . import orders_deliveries
from .buying_requirements import _get_sf_rows, _get_dbs_purchases, _get_cb_purchases, _sf_cache_is_fresh


# NEW
@router.get("/home", response_class=HTMLResponse)
def home(request: Request, user: str = Depends(get_current_user)):
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    def _warm_caches():
        def _sf():
            try:
                _get_sf_rows()
            except Exception as e:
                logger.warning(f"Home cache warm (Snowflake) failed: {e}")
        def _dbs():
            try:
                _get_dbs_purchases()
            except Exception as e:
                logger.warning(f"Home cache warm (DBS) failed: {e}")
        def _cb():
            try:
                _get_cb_purchases()
            except Exception as e:
                logger.warning(f"Home cache warm (CB) failed: {e}")
        def _sp():
            try:
                kpi.warm_sharepoint_cache()
            except Exception as e:
                logger.warning(f"Home cache warm (SharePoint KPI) failed: {e}")
        def _od():
            try:
                orders_deliveries.warm_cache()
            except Exception as e:
                logger.warning(f"Home cache warm (Orders & Deliveries) failed: {e}")
        def _apis():
            # Build the CAP and Autotrader clients and get an AT access token
            # now. Both read credentials from AWS Secrets Manager in their
            # constructors, and both are singletons, so doing it here means a
            # manual vehicle check does not pay for the secret fetch or the auth
            # round trip on its first use.
            try:
                import requests as _requests
                from list_processor.cap_client import CAPClient
                from list_processor.autotrader_client import AutoTraderClient
                CAPClient()
                AutoTraderClient().get_access_token(_requests.Session())
            except Exception as e:
                logger.warning(f"Home cache warm (CAP/Autotrader credentials) failed: {e}")
        with ThreadPoolExecutor(max_workers=6) as ex:
            ex.submit(_sf); ex.submit(_dbs); ex.submit(_cb); ex.submit(_sp); ex.submit(_od)
            ex.submit(_apis)
    threading.Thread(target=_warm_caches, daemon=True).start()
    _track(request, 'page_view', '/home')
    # Only the pages this user may open. /home itself is deliberately not in the
    # registry — everyone who is logged in needs to land somewhere, including a
    # user granted nothing at all.
    allowed = get_user_pages(user)
    return templates.TemplateResponse("home.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "pages": [p for p in PAGES if p.key in allowed],
    })


@router.get("/my-account", response_class=HTMLResponse)
def my_account(request: Request, user: str = Depends(get_current_user)):
    """The signed-in user's own details, opened from the email menu at the top
    right. Like /home it is not in the page registry: everyone who is logged in
    can open their own account."""
    if not user:
        return RedirectResponse(url=BASE_URL + "/login", status_code=303)
    _track(request, 'page_view', '/my-account')
    return templates.TemplateResponse("my_account.html", {
        "request": request, "user": user, "BASE_URL": BASE_URL,
        "details": user_details(user),
    })


# NEW
@router.get("/stock-list", response_class=HTMLResponse)
def stock_list(request: Request, user: str = Depends(require_page('stock_list'))):
    _track(request, 'page_view', '/stock-list')
    return templates.TemplateResponse("stock_list.html", {"request": request, "user": user, "BASE_URL": BASE_URL,
                                                          "is_admin": is_admin(user)})


# ── Stock List columns ───────────────────────────────────────────────────────
# Which columns show, in what order, with what heading and format, is set in
# the page's Settings popup and stored in kpi_layout under 'stock_list_columns'.
# The table itself is fixed: current_stock (uk_dbt_product_data
# current_stock__p). Column names are checked against INFORMATION_SCHEMA
# before they go into the SQL.

_STOCK_TABLE = "PRODUCT_VEHICLE_JOURNEY.CURRENT_STOCK"
# Always fetched, shown or not: the Status and Missing filters, summary counts,
# search, the interest-free row colour and the "Apex data updated" time read them.
_STOCK_FIXED_COLUMNS = ["VEHICLE_STATUS", "PRICE_BAND",
                        "VRM", "MAKE_MODEL", "CAP_DERIVATIVE", "VEHICLE_UPDATED_AT",
                        "CAP_LIVE_CLEAN_LATEST", "AUTOTRADER_LATEST_RETAIL_VALUATION"]
_STOCK_FORMATS = {"text", "number", "gbp", "pct", "date", "link", "status"}
_STOCK_MAX_COLUMNS = 100
# Filters added in Settings ('stock_list_filters' in kpi_layout): one button
# group per column, a button for each value found in the data.
_STOCK_MAX_FILTERS = 20
_STOCK_HARD_FILTER_OPS = {"equals", "not_equals", "contains", "is_blank", "is_not_blank"}

# Used until the columns are first saved in Settings. Matches the page as it was
# before the columns became editable.
STOCK_DEFAULT_COLUMNS = [
    {"column": "MAKE_MODEL_DERIVATIVE", "label": "Make / Model / Derivative", "format": "text", "decimals": 0, "width": 280},
    {"column": "PRICE_BAND", "label": "Price Band", "format": "text", "decimals": 0, "width": 65},
    {"column": "VRM", "label": "VRM", "format": "text", "decimals": 0, "width": 75},
    {"column": "VEHICLE_STATUS", "label": "Status", "format": "status", "decimals": 0, "width": 68},
    {"column": "VEHICLE_URL", "label": "Apex", "format": "link", "decimals": 0, "width": 46},
    {"column": "AUTOTRADER_PORTAL_LINK", "label": "Portal", "format": "link", "decimals": 0, "width": 52},
    {"column": "FUEL_TYPE", "label": "Fuel", "format": "text", "decimals": 0, "width": 65},
    {"column": "VEHICLE_LOCATION", "label": "Location", "format": "text", "decimals": 0, "width": 110},
    {"column": "SUPPLIER_NAME", "label": "Supplier", "format": "text", "decimals": 0, "width": 72},
    {"column": "DATE_FIRST_REGISTERED", "label": "DFR", "format": "date", "decimals": 0, "width": 80},
    {"column": "MILEAGE", "label": "Mileage", "format": "number", "decimals": 0, "width": 62},
    {"column": "DAYS_IN_STOCK", "label": "DIS", "format": "number", "decimals": 0, "width": 40},
    {"column": "DAYS_ADVERTISED", "label": "DTS", "format": "number", "decimals": 0, "width": 40},
    {"column": "PURCHASE_PRICE_INC_VAT", "label": "Purchase Price", "format": "gbp", "decimals": 0, "width": 90},
    {"column": "CAP_LIVE_CLEAN_LATEST", "label": "CAP Live Clean", "format": "gbp", "decimals": 0, "width": 90},
    {"column": "PURCHASE_PRICE_CAP_LIVE_CLEAN_PERCENT", "label": "CAP %", "format": "pct", "decimals": 1, "width": 55},
    {"column": "SELLING_PRICE_INC_VAT", "label": "Sell Price", "format": "gbp", "decimals": 0, "width": 80},
    {"column": "AUTOTRADER_LATEST_RETAIL_VALUATION", "label": "AT PTM", "format": "gbp", "decimals": 0, "width": 80},
    {"column": "SELLING_PRICE_AUTOTRADER_PERCENT", "label": "AT PTM %", "format": "pct", "decimals": 1, "width": 58},
    {"column": "AUTOTRADER_LATEST_RETAIL_RATING", "label": "AT Rating", "format": "number", "decimals": 0, "width": 48},
    {"column": "GPU_M", "label": "GPU-M", "format": "gbp", "decimals": 0, "width": 68},
]

# One cached result per set of columns. Refresh Data skips it.
_stock_cache: dict = {"key": None, "columns": None, "rows": None, "cached_at": 0.0}
_stock_cache_lock = threading.Lock()


def _stock_columns_saved() -> list:
    return kpi._read_kpi_layout_value('stock_list_columns') or STOCK_DEFAULT_COLUMNS


def _validate_stock_columns(columns) -> str | None:
    if not isinstance(columns, list) or not columns or len(columns) > _STOCK_MAX_COLUMNS:
        return f"Choose between 1 and {_STOCK_MAX_COLUMNS} columns"
    seen = set()
    for col in columns:
        if not isinstance(col, dict) or set(col) - {"column", "label", "format", "decimals", "width"}:
            return "bad column"
        name = col.get("column")
        if not isinstance(name, str) or not re.fullmatch(kpi._IDENTIFIER, name) or name in seen:
            return f"bad column: {name}"
        seen.add(name)
        label = col.get("label", "")
        if not isinstance(label, str) or len(label) > 60:
            return "Column headings must be 60 characters or fewer"
        if col.get("format", "text") not in _STOCK_FORMATS:
            return f"bad format for {name}"
        decimals = col.get("decimals", 0)
        if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 4:
            return f"bad decimals for {name}"
        width = col.get("width", 90)
        if isinstance(width, bool) or not isinstance(width, int) or not 30 <= width <= 1000:
            return f"bad width for {name}"
    return None


def _stock_filters_saved() -> list:
    return kpi._read_kpi_layout_value('stock_list_filters') or []


def _stock_hard_filters_saved() -> list:
    return kpi._read_kpi_layout_value('stock_list_hard_filters') or []


def _validate_stock_hard_filters(filters, live: set) -> str | None:
    if not isinstance(filters, list) or len(filters) > _STOCK_MAX_FILTERS:
        return f"Choose at most {_STOCK_MAX_FILTERS} hard filters"
    for f in filters:
        if not isinstance(f, dict) or set(f) != {"column", "operator", "value"}:
            return "Bad hard filter"
        column, operator, value = f["column"], f["operator"], f["value"]
        if not isinstance(column, str) or column not in live:
            return f"Hard filter column is not in the table: {column}"
        if operator not in _STOCK_HARD_FILTER_OPS:
            return f"Bad hard filter comparison: {operator}"
        if not isinstance(value, str) or len(value) > 200:
            return "Hard filter values must be text of at most 200 characters"
        if operator not in {"is_blank", "is_not_blank"} and not value:
            return "Enter a value for each hard filter"
    return None


def _stock_hard_filter_sql(filters: list) -> tuple[list, list]:
    clauses, params = [], []
    for f in filters:
        column, operator, value = f["column"], f["operator"], f["value"]
        if operator == "is_blank":
            clauses.append(f"({column} IS NULL OR TO_VARCHAR({column}) = '')")
        elif operator == "is_not_blank":
            clauses.append(f"({column} IS NOT NULL AND TO_VARCHAR({column}) <> '')")
        elif operator == "contains":
            clauses.append(f"CONTAINS(TO_VARCHAR({column}), %s)")
            params.append(value)
        else:
            clauses.append(f"TO_VARCHAR({column}) {'=' if operator == 'equals' else '<>'} %s")
            params.append(value)
    return clauses, params


def _validate_stock_filters(filters) -> str | None:
    if not isinstance(filters, list) or len(filters) > _STOCK_MAX_FILTERS:
        return f"At most {_STOCK_MAX_FILTERS} filters"
    seen = set()
    for f in filters:
        if not isinstance(f, dict) or set(f) - {"column", "label", "description"}:
            return "bad filter"
        name = f.get("column")
        if not isinstance(name, str) or not re.fullmatch(kpi._IDENTIFIER, name):
            return "Choose a column for each filter"
        if name in seen:
            return f"{name} has more than one filter"
        seen.add(name)
        label = f.get("label", "")
        if not isinstance(label, str) or len(label) > 60:
            return "Filter titles must be 60 characters or fewer"
        description = f.get("description", "")
        if not isinstance(description, str) or len(description) > 300:
            return "Filter descriptions must be 300 characters or fewer"
    return None


@router.get("/stock-list/columns")
def stock_list_columns(request: Request, user: str = Depends(require_page_api('stock_list'))):
    """The saved columns and filters, and every column of current_stock,
    for the Settings popup."""
    try:
        live = kpi._vehicle_table_columns(_STOCK_TABLE)
        saved = _stock_columns_saved()
        filters = _stock_filters_saved()
        hard_filters = _stock_hard_filters_saved()
    except Exception as e:
        logger.error(f"Stock list columns failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "view": saved, "filters": filters, "hard_filters": hard_filters, "table": _STOCK_TABLE,
            "columns": [{"column": c, "type": t} for c, t in live]}


@router.post("/stock-list/hard-filters")
def stock_list_hard_filters_save(request: Request, payload: dict, user: str = Depends(require_page_api('stock_list'))):
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the Stock List hard filters"}
    filters = (payload or {}).get("hard_filters")
    try:
        live = {c for c, _ in kpi._vehicle_table_columns(_STOCK_TABLE)}
        err = _validate_stock_hard_filters(filters, live)
        if err:
            return {"status": "error", "message": err}
        kpi._write_kpi_layout_value('stock_list_hard_filters', filters)
        with _stock_cache_lock:
            _stock_cache["key"] = None
    except Exception as e:
        logger.error(f"Stock list hard filters save failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "hard_filters": filters}


@router.post("/stock-list/filters")
def stock_list_filters_save(request: Request, payload: dict, user: str = Depends(require_page_api('stock_list'))):
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the Stock List filters"}
    filters = (payload or {}).get("filters")
    err = _validate_stock_filters(filters)
    if err:
        return {"status": "error", "message": err}
    try:
        kpi._write_kpi_layout_value('stock_list_filters', filters)
    except Exception as e:
        logger.error(f"Stock list filters save failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "filters": filters}


@router.post("/stock-list/columns")
def stock_list_columns_save(request: Request, payload: dict, user: str = Depends(require_page_api('stock_list'))):
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the Stock List columns"}
    columns = (payload or {}).get("columns")
    err = _validate_stock_columns(columns)
    if err:
        return {"status": "error", "message": err}
    try:
        kpi._write_kpi_layout_value('stock_list_columns', columns)
    except Exception as e:
        logger.error(f"Stock list columns save failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "view": columns}


@router.get("/stock-list/data")
def stock_list_data(request: Request, force: bool = False, user: str = Depends(require_page_api('stock_list'))):
    """The saved columns plus the fixed ones the filters need, one row per vehicle.
    Saved columns that are not in the table (e.g. before a dbt run adds them)
    are left out and named in `missing` rather than failing the page."""
    try:
        view = _stock_columns_saved()
        filters = _stock_filters_saved()
        hard_filters = _stock_hard_filters_saved()
        live = {c for c, _ in kpi._vehicle_table_columns(_STOCK_TABLE)}
        if not live:
            return {"status": "error", "message": f"Table {_STOCK_TABLE} was not found"}
        err = _validate_stock_hard_filters(hard_filters, live)
        if err:
            return {"status": "error", "message": err}
        wanted = list(dict.fromkeys(_STOCK_FIXED_COLUMNS + [c["column"] for c in view]
                                    + [f["column"] for f in filters]))
        select = [c for c in wanted if c in live]
        missing = [c for c in dict.fromkeys([c["column"] for c in view] + [f["column"] for f in filters])
                   if c not in live]
        filters = [f for f in filters if f["column"] in live]
        key = (tuple(select), tuple((f["column"], f["operator"], f["value"]) for f in hard_filters))
        with _stock_cache_lock:
            cache = dict(_stock_cache)
        if force or cache["key"] != key or not _sf_cache_is_fresh(cache["cached_at"]):
            with SnowflakeClient() as sf:
                clauses, params = _stock_hard_filter_sql(hard_filters)
                where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
                rows, _ = sf.execute(f"SELECT {', '.join(select)} FROM {kpi._VEHICLE_DB}.{_STOCK_TABLE}{where}", params)
            cache = {"key": key, "columns": select, "cached_at": time.time(),
                     "rows": [[kpi._json_cell(v) for v in r] for r in rows]}
            with _stock_cache_lock:
                _stock_cache.update(cache)
    except Exception as e:
        logger.error(f"Stock list data failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "view": view, "filters": filters, "columns": cache["columns"], "rows": cache["rows"],
            "missing": missing, "cached_at": cache["cached_at"]}
