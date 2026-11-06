"""Apex Missing Finance page: orders whose finance commission or subsidy is
missing in Apex, from PRODUCT_SALES_PERFORMANCE.MISSING_FINANCE_AMOUNTS
(uk_dbt_product_data missing_finance_amounts__p).

Built the same way as the Stock List (pages.py): the columns, filter buttons
and hard filters are set in the page's Settings popup and stored in kpi_layout,
and checked with the Stock List's own validators. On top of those, cell colour
rules colour a cell when its value matches."""
import re
import threading
import time

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
import logging

from utils import email_client, page_alerts
from utils.db_utils import get_database_connection, read_user_details
from utils.snowflake_client import SnowflakeClient

from .core import (BASE_URL, templates, _track, require_page, require_page_api, is_admin, page_name, _uk_now,
                   _ENV, ADMIN_USERNAME)
from . import kpi
from .buying_requirements import _sf_cache_is_fresh
from .pages import (_validate_stock_columns, _validate_stock_filters, _validate_stock_hard_filters,
                    _stock_hard_filter_sql, _STOCK_MAX_FILTERS)

logger = logging.getLogger(__name__)

router = APIRouter()

PAGE_KEY = 'apex_missing_finance'
_AMF_TABLE = "PRODUCT_SALES_PERFORMANCE.MISSING_FINANCE_AMOUNTS"
# Always fetched.
_AMF_FIXED_COLUMNS = ["VRM"]
# Shown as "last built": this table's last rebuild, in UK time. Its UPDATED_AT
# cannot say this: a fixed order drops out of the report, so the newest row can
# be days older than the data.
_AMF_LAST_BUILT_SQL = ("SELECT CONVERT_TIMEZONE('Europe/London', LAST_ALTERED)::TIMESTAMP_NTZ "
                       f"FROM {kpi._VEHICLE_DB}.INFORMATION_SCHEMA.TABLES "
                       "WHERE TABLE_SCHEMA = 'PRODUCT_SALES_PERFORMANCE' AND TABLE_NAME = 'MISSING_FINANCE_AMOUNTS'")
_AMF_MAX_RULES = 30
_AMF_RULE_OPS = {"equals", "not_equals", "contains", "is_blank", "is_not_blank", "less_than", "greater_than"}
_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")

# Used until the columns are first saved in Settings.
AMF_DEFAULT_COLUMNS = [
    {"column": "VRM", "label": "VRM", "format": "text", "decimals": 0, "width": 75},
    {"column": "CUSTOMER_LAST_NAME", "label": "Customer", "format": "text", "decimals": 0, "width": 110},
    {"column": "INVOICE_STATUS", "label": "Status", "format": "status", "decimals": 0, "width": 95},
    {"column": "ORDER_ACKNOWLEDGED_AT", "label": "Acknowledged At", "format": "date", "decimals": 0, "width": 95},
    {"column": "ASSIGNED_TO_SITE_NAME", "label": "Assignee Site", "format": "text", "decimals": 0, "width": 110},
    {"column": "ASSIGNED_TO_USER_NAME", "label": "Assignee", "format": "text", "decimals": 0, "width": 120},
    {"column": "SALES_ADVISOR_SITE_NAME", "label": "Sales Advisor Site", "format": "text", "decimals": 0, "width": 110},
    {"column": "SALES_ADVISOR_USER_NAME", "label": "Sales Advisor", "format": "text", "decimals": 0, "width": 120},
    {"column": "INVOICE_URL", "label": "Invoice", "format": "link", "decimals": 0, "width": 62},
    {"column": "ORDER_URL", "label": "Order", "format": "link", "decimals": 0, "width": 58},
    {"column": "VEHICLE_URL", "label": "Vehicle", "format": "link", "decimals": 0, "width": 62},
    {"column": "ISSUE", "label": "Issue", "format": "text", "decimals": 0, "width": 130},
    {"column": "FINANCE_COMPANY_NAME", "label": "Finance Company", "format": "text", "decimals": 0, "width": 140},
    {"column": "FINANCE_TYPE", "label": "Finance Type", "format": "text", "decimals": 0, "width": 70},
    {"column": "FINANCE_APR", "label": "APR", "format": "number", "decimals": 1, "width": 50},
    {"column": "FINANCE_PRODUCT_TYPE", "label": "Rate/0%", "format": "text", "decimals": 0, "width": 65},
    {"column": "FINANCE_COMMISSION_AMOUNT", "label": "Commission", "format": "gbp", "decimals": 2, "width": 85},
    {"column": "FINANCE_SUBSIDY_AMOUNT", "label": "Subsidy", "format": "gbp", "decimals": 2, "width": 85},
]

# Used until the cell colour rules are first saved in Settings.
AMF_DEFAULT_CELL_RULES = [
    {"column": "INVOICE_STATUS", "operator": "equals", "value": "STATUS_PAID", "colour": "#f8c9d0"},
    {"column": "ISSUE", "operator": "is_not_blank", "value": "", "colour": "#fff3b0"},
    {"column": "FINANCE_APR", "operator": "equals", "value": "0", "colour": "#fde2b8"},
]

# Used until the hard filters are first saved in Settings: orders from 2026 on.
AMF_DEFAULT_HARD_FILTERS = [
    {"column": "ORDER_ACKNOWLEDGED_AT", "operator": "greater_than", "value": "2026-01-01"},
]
# The Stock List comparisons plus less / greater than, compared in Snowflake's
# own type for the column (a date such as 2026-01-01, or a number).
_AMF_RANGE_OPS = {"less_than": "<", "greater_than": ">"}

_amf_cache: dict ={"key": None, "columns": None, "rows": None, "cached_at": 0.0, "last_built": None}
_amf_cache_lock = threading.Lock()


def _columns_saved() -> list:
    return kpi._read_kpi_layout_value('amf_columns') or AMF_DEFAULT_COLUMNS


def _filters_saved() -> list:
    return kpi._read_kpi_layout_value('amf_filters') or []


# Table text size in pixels, and whether each column is sized to its longest
# value at that size (off = the widths set per column are used).
AMF_DEFAULT_APPEARANCE = {"font_size": 12, "auto_width": True}


def _appearance_saved() -> dict:
    saved = kpi._read_kpi_layout_value('amf_appearance')
    return {**AMF_DEFAULT_APPEARANCE, **(saved if isinstance(saved, dict) else {})}


def _validate_appearance(a) -> str | None:
    if not isinstance(a, dict) or set(a) != {"font_size", "auto_width"}:
        return "Bad appearance settings"
    if not isinstance(a["font_size"], int) or not 8 <= a["font_size"] <= 24:
        return "Font size must be a whole number from 8 to 24"
    if not isinstance(a["auto_width"], bool):
        return "Bad fit column widths setting"
    return None


def _hard_filters_saved() -> list:
    saved = kpi._read_kpi_layout_value('amf_hard_filters')
    return AMF_DEFAULT_HARD_FILTERS if saved is None else saved


def _validate_hard_filters(filters, live: set) -> str | None:
    """The Stock List check, with less / greater than allowed as well."""
    if not isinstance(filters, list) or len(filters) > _STOCK_MAX_FILTERS:
        return f"Choose at most {_STOCK_MAX_FILTERS} hard filters"
    stock_like = [f for f in filters if not (isinstance(f, dict) and f.get("operator") in _AMF_RANGE_OPS)]
    for f in filters:
        if f not in stock_like:
            if set(f) != {"column", "operator", "value"} or f["column"] not in live:
                return f"Hard filter column is not in the table: {f.get('column')}"
            if not isinstance(f["value"], str) or not f["value"].strip() or len(f["value"]) > 200:
                return "Enter a value of at most 200 characters for each hard filter"
    return _validate_stock_hard_filters(stock_like, live)


def _hard_filter_sql(filters: list) -> tuple[list, list]:
    clauses, params = _stock_hard_filter_sql([f for f in filters if f["operator"] not in _AMF_RANGE_OPS])
    for f in filters:
        if f["operator"] in _AMF_RANGE_OPS:
            clauses.append(f"{f['column']} {_AMF_RANGE_OPS[f['operator']]} %s")
            params.append(f["value"])
    return clauses, params


def _cell_rules_saved() -> list:
    saved = kpi._read_kpi_layout_value('amf_cell_rules')
    return AMF_DEFAULT_CELL_RULES if saved is None else saved


def _validate_cell_rules(rules, live: set) -> str | None:
    if not isinstance(rules, list) or len(rules) > _AMF_MAX_RULES:
        return f"Choose at most {_AMF_MAX_RULES} colour rules"
    for r in rules:
        if not isinstance(r, dict) or set(r) != {"column", "operator", "value", "colour"}:
            return "Bad colour rule"
        column, operator, value, colour = r["column"], r["operator"], r["value"], r["colour"]
        if not isinstance(column, str) or column not in live:
            return f"Colour rule column is not in the table: {column}"
        if operator not in _AMF_RULE_OPS:
            return f"Bad colour rule comparison: {operator}"
        if not isinstance(value, str) or len(value) > 200:
            return "Colour rule values must be text of at most 200 characters"
        if operator not in {"is_blank", "is_not_blank"} and not value:
            return "Enter a value for each colour rule"
        if operator in {"less_than", "greater_than"}:
            try:
                float(value)
            except ValueError:
                return f"Less than / greater than need a number: {value}"
        if not isinstance(colour, str) or not _COLOUR.fullmatch(colour):
            return f"Bad colour: {colour}"
    return None


def _live_columns() -> list:
    return kpi._vehicle_table_columns(_AMF_TABLE)


@router.get("/reports/apex-missing-finance", response_class=HTMLResponse)
def apex_missing_finance(request: Request, user: str = Depends(require_page(PAGE_KEY))):
    _track(request, 'page_view', '/reports/apex-missing-finance')
    return templates.TemplateResponse("apex_missing_finance.html", {"request": request, "user": user,
                                                                    "BASE_URL": BASE_URL, "is_admin": is_admin(user)})


@router.get("/reports/apex-missing-finance/columns")
def amf_columns(request: Request, user: str = Depends(require_page_api(PAGE_KEY))):
    """The saved settings and every column of the table, for the Settings popup."""
    try:
        live = _live_columns()
        result = {"status": "ok", "view": _columns_saved(), "filters": _filters_saved(),
                  "hard_filters": _hard_filters_saved(), "cell_rules": _cell_rules_saved(), "table": _AMF_TABLE,
                  "appearance": _appearance_saved(),
                  "columns": [{"column": c, "type": t} for c, t in live]}
    except Exception as e:
        logger.error(f"Apex missing finance columns failed: {e}")
        return {"status": "error", "message": str(e)}
    return result


@router.post("/reports/apex-missing-finance/settings")
def amf_settings_save(request: Request, payload: dict, user: str = Depends(require_page_api(PAGE_KEY))):
    """Columns, filters, hard filters and colour rules, all checked before any is written."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the Apex Missing Finance settings"}
    payload = payload or {}
    columns, filters = payload.get("columns"), payload.get("filters")
    hard_filters, cell_rules = payload.get("hard_filters"), payload.get("cell_rules")
    appearance = payload.get("appearance", _appearance_saved())
    try:
        live = {c for c, _ in _live_columns()}
        err = (_validate_stock_columns(columns) or _validate_stock_filters(filters)
               or _validate_hard_filters(hard_filters, live) or _validate_cell_rules(cell_rules, live)
               or _validate_appearance(appearance))
        if err:
            return {"status": "error", "message": err}
        kpi._write_kpi_layout_value('amf_appearance', appearance)
        kpi._write_kpi_layout_value('amf_columns', columns)
        kpi._write_kpi_layout_value('amf_filters', filters)
        kpi._write_kpi_layout_value('amf_hard_filters', hard_filters)
        kpi._write_kpi_layout_value('amf_cell_rules', cell_rules)
        with _amf_cache_lock:
            _amf_cache["key"] = None
    except Exception as e:
        logger.error(f"Apex missing finance settings save failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok"}


@router.get("/reports/apex-missing-finance/data")
def amf_data(request: Request, force: bool = False, user: str = Depends(require_page_api(PAGE_KEY))):
    """The saved columns plus the fixed ones, one row per finance amount.
    Saved columns not in the table are left out and named in `missing`."""
    try:
        data = _load_data(force)
    except Exception as e:
        logger.error(f"Apex missing finance data failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", **data}


def _load_data(force: bool = False) -> dict:
    """The page's data, from the cache while it is fresh. Raises with a readable message."""
    view = _columns_saved()
    filters = _filters_saved()
    hard_filters = _hard_filters_saved()
    cell_rules = _cell_rules_saved()
    appearance = _appearance_saved()
    live = {c for c, _ in _live_columns()}
    if not live:
        raise ValueError(f"Table {_AMF_TABLE} was not found")
    err = _validate_hard_filters(hard_filters, live)
    if err:
        raise ValueError(err)
    wanted = list(dict.fromkeys(_AMF_FIXED_COLUMNS + [c["column"] for c in view]
                                + [f["column"] for f in filters] + [r["column"] for r in cell_rules]))
    select = [c for c in wanted if c in live]
    missing = [c for c in dict.fromkeys([c["column"] for c in view] + [f["column"] for f in filters])
               if c not in live]
    filters = [f for f in filters if f["column"] in live]
    key = (tuple(select), tuple((f["column"], f["operator"], f["value"]) for f in hard_filters))
    with _amf_cache_lock:
        cache = dict(_amf_cache)
    if force or cache["key"] != key or not _sf_cache_is_fresh(cache["cached_at"]):
        with SnowflakeClient() as sf:
            clauses, params = _hard_filter_sql(hard_filters)
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            rows, _ = sf.execute(f"SELECT {', '.join(select)} FROM {kpi._VEHICLE_DB}.{_AMF_TABLE}{where}", params)
            built, _ = sf.execute(_AMF_LAST_BUILT_SQL, [])
        cache = {"key": key, "columns": select, "cached_at": time.time(),
                 "last_built": kpi._json_cell(built[0][0]) if built else None,
                 "rows": [[kpi._json_cell(v) for v in r] for r in rows]}
        with _amf_cache_lock:
            _amf_cache.update(cache)
    return {"view": view, "filters": filters, "cell_rules": cell_rules, "columns": cache["columns"],
            "rows": cache["rows"], "missing": missing, "cached_at": cache["cached_at"], "last_built": cache["last_built"],
            "appearance": appearance}


# ── Alert: set up in Settings > Alerts, sent by hand with the Send Alert button ──

_ALERT_KEY = page_alerts.KEY_PREFIX + PAGE_KEY
_PAGE_PATH = "/reports/apex-missing-finance"
_SITE_URLS = {"prod": "https://uk-intel-hub.aramis-group.eu", "dev": "https://dev-uk-intel-hub.aramis-group.eu"}


def _alert_saved() -> dict:
    return page_alerts.saved_config(kpi._read_kpi_layout_value(_ALERT_KEY), ADMIN_USERNAME)


def _global_style() -> dict:
    return page_alerts.saved_style(kpi._read_kpi_layout_value(page_alerts.STYLE_KEY))


def _people() -> dict:
    with get_database_connection() as conn:
        return read_user_details(conn)


def _page_url(request: Request) -> str:
    site = _SITE_URLS.get(_ENV) or str(request.base_url).rstrip("/")
    return site + BASE_URL + _PAGE_PATH


def _alert_email(request: Request, cfg: dict) -> tuple[str, str]:
    """Subject and HTML for the alert, from the same data the page shows, newest order first."""
    data = _load_data()
    col_idx = {c: i for i, c in enumerate(data["columns"])}
    columns = [c for c in data["view"] if c["column"] in col_idx]
    rows = data["rows"]
    if "ORDER_ACKNOWLEDGED_AT" in col_idx:
        i = col_idx["ORDER_ACKNOWLEDGED_AT"]
        rows = sorted(rows, key=lambda r: r[i] or "", reverse=True)
    style = page_alerts.effective_style(_global_style(), cfg["style"])
    values = {"count": f"{len(rows):,}", "date": _uk_now().strftime("%d/%m/%Y"), "page": page_name(PAGE_KEY),
              "last_built": page_alerts.uk_datetime_text(data["last_built"])}
    return page_alerts.build_email(cfg, style, values, _page_url(request), columns, col_idx, rows, data["cell_rules"])


@router.get("/reports/apex-missing-finance/alert")
def amf_alert(request: Request, user: str = Depends(require_page_api(PAGE_KEY))):
    """The saved alert and every user, for Settings > Alerts and the Send Alert popup."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can set up or send this alert"}
    try:
        people = _people()
        users = sorted(({"username": u, "name": f"{d['first_name']} {d['last_name']}".strip() or u, "email": d["email"]}
                        for u, d in people.items()), key=lambda p: p["name"].lower())
        return {"status": "ok", "alert": _alert_saved(), "users": users, "placeholders": list(page_alerts.PLACEHOLDERS),
                "global_style": _global_style(), "style_meta": page_alerts.style_meta()}
    except Exception as e:
        logger.error(f"Apex missing finance alert read failed: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/reports/apex-missing-finance/alert")
def amf_alert_save(request: Request, payload: dict, user: str = Depends(require_page_api(PAGE_KEY))):
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can set up this alert"}
    cfg = (payload or {}).get("alert")
    try:
        err = page_alerts.validate(cfg, set(_people()))
        if err:
            return {"status": "error", "message": err}
        kpi._write_kpi_layout_value(_ALERT_KEY, cfg)
        logger.info("Apex missing finance alert saved by %s", user)
    except Exception as e:
        logger.error(f"Apex missing finance alert save failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok"}


@router.post("/reports/apex-missing-finance/alert/test")
def amf_alert_test(request: Request, payload: dict, user: str = Depends(require_page_api(PAGE_KEY))):
    """The alert as set up in the popup (saved or not), sent only to the person testing it."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can test this alert"}
    cfg = (payload or {}).get("alert")
    try:
        people = _people()
        err = page_alerts.validate(cfg, set(people))
        if err:
            return {"status": "error", "message": err}
        me = people.get(user, {}).get("email")
        if not me:
            return {"status": "error", "message": "You have no email address on Settings > User Accounts"}
        subject, body = _alert_email(request, cfg)
        email_client.send_email(me, "Test: " + subject, body)
        logger.info("Apex missing finance alert test sent to %s", me)
    except Exception as e:
        logger.error(f"Apex missing finance alert test failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "sent_to": [me]}


@router.post("/reports/apex-missing-finance/alert/send")
def amf_alert_send(request: Request, user: str = Depends(require_page_api(PAGE_KEY))):
    """The saved alert, to the saved recipients who have an email address."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can send this alert"}
    try:
        cfg = _alert_saved()
        people = _people()
        to = sorted({people[u]["email"] for u in cfg["users"] if people.get(u, {}).get("email")})
        if not to:
            return {"status": "error", "message": "Nobody on the alert has an email address. Tick people in Settings > Alerts."}
        subject, body = _alert_email(request, cfg)
        email_client.send_email(to, subject, body)
        _track(request, 'alert_sent', _PAGE_PATH)
        logger.info("Apex missing finance alert sent by %s to %d people", user, len(to))
    except Exception as e:
        logger.error(f"Apex missing finance alert send failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "sent_to": to}
