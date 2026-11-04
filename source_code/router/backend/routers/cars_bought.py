"""Cars Bought: read supplier invoices into vehicle purchase records.

See utils/cars_bought.py for how files are read and what is stored.
"""
import base64
import binascii
import csv
import io
import json
import logging
import mimetypes
import os
import re
import threading
import time
import uuid
from types import SimpleNamespace
from urllib.parse import quote

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from utils import cars_bought as cb
from utils import cars_bought_import as cb_import
from utils import cars_bought_inbox as inbox
from utils import supplier_store as ss
from utils import vehicle_metrics as vm
from utils import vehicle_store as vs
from utils.db_utils import (get_database_connection, get_vehicle_database_connection,
                            save_settings_partial, widen_settings_values, read_role_users, read_user_details)
from utils.sharepoint_client import SharePointClient
from utils.snowflake_client import SnowflakeClient
from .buying_requirements import _sf_cache_is_fresh
from .core import (BASE_URL, templates, _track, require_page, require_page_api, is_admin, _uk_now,
                   s3_bucketname, s3_bucket_region, _S3_ENV, check_worker_secret, worker_secret)
from . import kpi

logger = logging.getLogger(__name__)

router = APIRouter()

_PAGE = 'cars_bought'
_CUSTOM_FIELDS_KEY = 'cars_bought_custom_fields'
_SHEET_FIELDS_ADDED_KEY = 'cars_bought_sheet_fields_added'
_TABLE_COLUMNS_KEY = 'cars_bought_table_columns'
_TABLE_ROWS_KEY = 'cars_bought_table_rows'
_METRICS_KEY = 'cars_bought_metrics'
# '1' = the buyer is always the logged-in user (dropdown locked).
_LOCK_BUYER_KEY = 'cars_bought_lock_buyer'
# How invoices are read: column headings per field, and the words marking a fee line.
_COLUMN_NAMES_KEY = 'cars_bought_column_names'
_FEE_WORDS_KEY = 'cars_bought_fee_words'
# Where Supplier Names used to be kept: read once, into supplier_invoice_names.
_SUPPLIER_NAMES_KEY = 'cars_bought_supplier_names'
_SHARED_TABLES_DONE_KEY = 'cars_bought_shared_tables_done'
_FEE_VAT_RATE_KEY = 'cars_bought_fee_vat_rate'
_SUPPLIER_RULES_KEY = 'cars_bought_supplier_rules'
_OWN_NAMES_KEY = 'cars_bought_own_names'
_GRID_COLOUR_KEY = 'cars_bought_grid_colour'
DEFAULT_GRID_COLOUR = '#d0d0d0'
_MISSING_COLOUR_KEY = 'cars_bought_missing_colour'
DEFAULT_MISSING_COLOUR = '#f8d7da'
_NOT_USED_COLOUR_KEY = 'cars_bought_not_used_colour'
DEFAULT_NOT_USED_COLOUR = '#e9ecef'
# Oldest Supplier Stock Evaluator data (hours) used to fill in a car.
_EVALUATOR_HOURS_KEY = 'cars_bought_evaluator_max_hours'
# '0' = Look Up CAP & AT always calls live, ignoring figures already stored.
_LOOKUP_REUSE_KEY = 'cars_bought_lookup_reuse'
# The SharePoint folder invoices are picked up from. Off until switched on.
_INBOX_ENABLED_KEY = 'cars_bought_inbox_enabled'
_INBOX_FOLDER_KEY = 'cars_bought_inbox_folder'
_INBOX_DONE_FOLDER_KEY = 'cars_bought_inbox_done_folder'
_INBOX_MAX_FILES_KEY = 'cars_bought_inbox_max_files'
_INBOX_SINCE_KEY = 'cars_bought_inbox_since'


def _read_setting(conn, key: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = cur.fetchone()
    return row[0] if row else ''


def _custom_fields(conn) -> list:
    fields = cb.read_custom_fields({_CUSTOM_FIELDS_KEY: _read_setting(conn, _CUSTOM_FIELDS_KEY)})
    # The buying sheet's fields are added once, so a field deleted later stays deleted.
    if _read_setting(conn, _SHEET_FIELDS_ADDED_KEY) != '1':
        fields = cb.add_sheet_fields(fields)
        widen_settings_values(conn)
        save_settings_partial(conn, {_CUSTOM_FIELDS_KEY: json.dumps(fields), _SHEET_FIELDS_ADDED_KEY: '1'})
    return fields


def _metrics(conn) -> list:
    return _json_setting(conn, _METRICS_KEY, [])


def _table_settings(conn, fields: list) -> dict:
    """The All Vehicles Bought table's columns, metrics and how many rows show at once."""
    try:
        rows = int(_read_setting(conn, _TABLE_ROWS_KEY) or cb.DEFAULT_TABLE_ROWS)
    except ValueError:
        rows = cb.DEFAULT_TABLE_ROWS
    metrics = _metrics(conn)
    return {"table_columns": cb.read_table_columns(_json_setting(conn, _TABLE_COLUMNS_KEY, []), fields, metrics),
            "table_live": cb.table_live_columns(fields, metrics), "table_rows": rows, "metrics": metrics}


def _clean_colour(colour, default: str) -> str:
    return colour if re.fullmatch(r"#[0-9a-fA-F]{6}", str(colour or "")) else default


def _grid_colour(conn) -> str:
    return _clean_colour(_read_setting(conn, _GRID_COLOUR_KEY), DEFAULT_GRID_COLOUR)


def _missing_colour(conn) -> str:
    return _clean_colour(_read_setting(conn, _MISSING_COLOUR_KEY), DEFAULT_MISSING_COLOUR)


def _not_used_colour(conn) -> str:
    return _clean_colour(_read_setting(conn, _NOT_USED_COLOUR_KEY), DEFAULT_NOT_USED_COLOUR)


def _person_name(people: dict, username: str) -> str:
    d = people.get(username) or {}
    return f"{d.get('first_name') or ''} {d.get('last_name') or ''}".strip() or username


def _buyer_config(conn, user: str) -> dict:
    """The Buyer dropdown: every user with the Buyer role on the Users page,
    and whether it is locked to the logged-in user."""
    people = read_user_details(conn)
    return {"buyers": sorted({_person_name(people, u) for u in read_role_users(conn, cb.BUYER_ROLE)}),
            "lock_buyer": _read_setting(conn, _LOCK_BUYER_KEY) == '1',
            "me": _person_name(people, user)}


@router.get("/cars-bought", response_class=HTMLResponse)
def cars_bought_table_page(request: Request, user: str = Depends(require_page(_PAGE))):
    """All Cars Bought: the full table of cars submitted on this site."""
    _track(request, 'page_view', '/cars-bought')
    with get_database_connection() as conn:
        grid_colour = _grid_colour(conn)
        missing_colour = _missing_colour(conn)
        not_used_colour = _not_used_colour(conn)
    return templates.TemplateResponse("cars_bought_table.html", {"request": request, "user": user, "BASE_URL": BASE_URL,
                                                                 "is_admin": is_admin(user), "grid_colour": grid_colour,
                                                                 "missing_colour": missing_colour, "not_used_colour": not_used_colour})


@router.get("/cars-bought/add", response_class=HTMLResponse)
def cars_bought_page(request: Request, user: str = Depends(require_page(_PAGE))):
    _track(request, 'page_view', '/cars-bought/add')
    with get_database_connection() as conn:
        inbox_cfg = _inbox_settings(conn)
        grid_colour = _grid_colour(conn)
        missing_colour = _missing_colour(conn)
        not_used_colour = _not_used_colour(conn)
    return templates.TemplateResponse("cars_bought.html", {"request": request, "user": user, "BASE_URL": BASE_URL,
                                                           "is_admin": is_admin(user), "inbox_enabled": inbox_cfg["inbox_enabled"],
                                                           "inbox_folder": inbox_cfg["inbox_folder"],
                                                           "inbox_folder_url": inbox_cfg["inbox_folder_url"], "grid_colour": grid_colour,
                                                           "missing_colour": missing_colour, "not_used_colour": not_used_colour})


@router.get("/cars-bought/config")
def cars_bought_config(user: str = Depends(require_page_api(_PAGE))):
    """Suppliers and buyers for the dropdowns, and the custom fields."""
    with get_database_connection() as conn:
        fields = _custom_fields(conn)
        buyers = _buyer_config(conn, user)
        names, fee_words, fee_vat_rate, supplier_rules = _reading_settings(conn)
        hours = _max_age_hours(conn)
        lookup_reuse = _lookup_reuse(conn)
        inbox_cfg = _inbox_settings(conn)
        grid_colour = _grid_colour(conn)
        missing_colour = _missing_colour(conn)
        not_used_colour = _not_used_colour(conn)
        own_names = _own_names(conn)
        table = _table_settings(conn, fields)
    # Not caught: an empty list here would be saved back as empty by Settings.
    supplier_names = _supplier_names()
    try:
        with get_vehicle_database_connection() as conn:
            suppliers = ss.list_suppliers(conn)
    except Exception as e:
        logger.warning(f"Cars Bought: suppliers not read: {e}")
        suppliers = []
    return {"status": "ok", "suppliers": suppliers, "custom_fields": fields, **buyers, **table,
            "column_names": names, "column_labels": cb.COLUMN_FIELD_LABELS, "fee_words": fee_words,
            "supplier_names": supplier_names, "fee_vat_rate": fee_vat_rate, "supplier_rules": supplier_rules, "own_names": own_names,
            "evaluator_max_hours": hours, "lookup_reuse": lookup_reuse, "grid_colour": grid_colour,
            "missing_colour": missing_colour, "not_used_colour": not_used_colour, **inbox_cfg}


def _inbox_settings(conn) -> dict:
    try:
        max_files = int(_read_setting(conn, _INBOX_MAX_FILES_KEY) or inbox.DEFAULT_MAX_FILES)
    except ValueError:
        max_files = inbox.DEFAULT_MAX_FILES
    folder = _read_setting(conn, _INBOX_FOLDER_KEY) or inbox.DEFAULT_FOLDER
    return {"inbox_enabled": _read_setting(conn, _INBOX_ENABLED_KEY) == '1',
            "inbox_folder": folder, "inbox_folder_url": SharePointClient.folder_url(folder),
            "inbox_done_folder": _read_setting(conn, _INBOX_DONE_FOLDER_KEY) or inbox.DEFAULT_DONE_FOLDER,
            "inbox_max_files": max_files,
            "inbox_since": _read_setting(conn, _INBOX_SINCE_KEY)}


def _json_setting(conn, key: str, default):
    try:
        value = json.loads(_read_setting(conn, key) or 'null')
    except ValueError:
        value = None
    return value if isinstance(value, type(default)) else default


def _own_names(conn) -> list:
    return _json_setting(conn, _OWN_NAMES_KEY, list(cb.DEFAULT_OWN_NAMES))


def _supplier_ids() -> dict:
    """{lower-case name: id} of every supplier."""
    try:
        with get_vehicle_database_connection() as conn:
            return {str(s["supplier_name"]).strip().lower(): s["supplier_id"] for s in ss.list_suppliers(conn)}
    except Exception as e:
        logger.warning(f"Cars Bought: suppliers not read: {e}")
        return {}


def _connect_to_shared_tables() -> None:
    """Once per database: move what Cars Bought kept to itself into the shared
    tables. Supplier Names, the suppliers on earlier purchases, their cars, and
    the RRP and prep cost buyers gave (rows come newest first, and the newest wins)."""
    with get_database_connection() as conn:
        if _read_setting(conn, _SHARED_TABLES_DONE_KEY) == '1':
            return
        old_names = _json_setting(conn, _SUPPLIER_NAMES_KEY, [dict(s) for s in cb.DEFAULT_SUPPLIER_NAMES])
        unlinked = cb.batches_without_supplier(conn)
        purchases = cb.read_vehicles(conn)
    newest = {}
    for r in purchases:
        newest.setdefault(r["vrm"], r)
    with get_vehicle_database_connection() as conn:
        ss.save_invoice_names(conn, cb.usable_supplier_names(old_names))
        ids = {name: ss.find_or_add_supplier(conn, name) for name in unlinked}
        vs.ensure_vehicles_exist(conn, list(newest))
        conn.commit()
        vs.save_buyer_figures(conn, cb.buyer_figures_from_rows(newest.values()), "Cars Bought")
    with get_database_connection() as conn:
        cb.link_batches_to_suppliers(conn, ids)
        save_settings_partial(conn, {_SHARED_TABLES_DONE_KEY: '1'})


def _supplier_names() -> list:
    """The Supplier Names list: [{text on an invoice, supplier}]."""
    _connect_to_shared_tables()
    with get_vehicle_database_connection() as conn:
        return ss.read_invoice_names(conn)


def _reading_settings(conn) -> tuple:
    """(column names per field, fee words, VAT % on fees, supplier rules) used
    to read uploaded invoices. Supplier Names come from _supplier_names."""
    try:
        rate = float(_read_setting(conn, _FEE_VAT_RATE_KEY) or cb.DEFAULT_FEE_VAT_RATE)
    except ValueError:
        rate = cb.DEFAULT_FEE_VAT_RATE
    return (cb.read_column_names(_json_setting(conn, _COLUMN_NAMES_KEY, {})),
            _json_setting(conn, _FEE_WORDS_KEY, list(cb.DEFAULT_FEE_WORDS)),
            rate,
            _json_setting(conn, _SUPPLIER_RULES_KEY, [dict(r) for r in cb.DEFAULT_SUPPLIER_RULES]))


@router.post("/cars-bought/config")
def cars_bought_save_config(payload: dict, user: str = Depends(require_page_api(_PAGE))):
    """Admin only: custom fields, the buyer lock, and the column names and fee
    words used to read invoices."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change these settings"}
    payload = payload or {}
    fields = cb.clean_custom_fields(payload.get("custom_fields"))
    lock = '1' if payload.get("lock_buyer") else '0'
    names = cb.read_column_names({k: [str(x).strip()[:60] for x in v if str(x).strip()]
                                  for k, v in (payload.get("column_names") or {}).items() if isinstance(v, list)})
    words = [str(w).strip()[:30] for w in (payload.get("fee_words") or []) if str(w).strip()]
    try:
        supplier_names = cb.clean_supplier_names(payload.get("supplier_names"))
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    own_names = [str(n).strip()[:60] for n in (payload.get("own_names") or []) if str(n).strip()]
    supplier_rules = cb.clean_supplier_rules(payload.get("supplier_rules"))
    try:
        rate = float(payload.get("fee_vat_rate"))
    except (TypeError, ValueError):
        rate = cb.DEFAULT_FEE_VAT_RATE
    if not 0 <= rate <= 100:
        return {"status": "error", "message": "VAT on fees must be between 0 and 100%"}
    try:
        hours = float(payload.get("evaluator_max_hours"))
    except (TypeError, ValueError):
        hours = float(cb.DEFAULT_EVALUATOR_MAX_HOURS)
    if not 1 <= hours <= 720:
        return {"status": "error", "message": "Evaluator data age must be between 1 and 720 hours"}
    lookup_reuse = bool(payload.get("lookup_reuse"))
    try:
        folder = inbox.clean_folder(payload.get("inbox_folder") or inbox.DEFAULT_FOLDER)
        done_folder = inbox.clean_done_folder(payload.get("inbox_done_folder") or inbox.DEFAULT_DONE_FOLDER)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    max_files = cb.to_int(payload.get("inbox_max_files"))
    if max_files is None or not 1 <= max_files <= 100:
        return {"status": "error", "message": "Most Files Read Per Check must be between 1 and 100"}
    since = str(payload.get("inbox_since") or "").strip()[:10]
    grid_colour = _clean_colour(payload.get("grid_colour"), DEFAULT_GRID_COLOUR)
    missing_colour = _clean_colour(payload.get("missing_colour"), DEFAULT_MISSING_COLOUR)
    not_used_colour = _clean_colour(payload.get("not_used_colour"), DEFAULT_NOT_USED_COLOUR)
    rows_at_once = cb.to_int(payload.get("table_rows"))
    if rows_at_once is None or not 50 <= rows_at_once <= 5000:
        return {"status": "error", "message": "Rows Shown At Once must be between 50 and 5000"}
    _connect_to_shared_tables()
    with get_vehicle_database_connection() as conn:
        ss.save_invoice_names(conn, supplier_names)
        # Left out when the Apex supplier list could not be read, so nothing is cleared.
        ss.save_contacts(conn, cb.clean_supplier_contacts(payload.get("supplier_contacts")))
    ids = _supplier_ids()
    with get_database_connection() as conn:
        old_fields = _custom_fields(conn)
        metrics = _metrics(conn)
        table_columns = cb.add_new_field_columns(cb.clean_table_columns(payload.get("table_columns"), fields, metrics), old_fields, fields)
        widen_settings_values(conn)
        save_settings_partial(conn, {_TABLE_COLUMNS_KEY: json.dumps(table_columns), _TABLE_ROWS_KEY: rows_at_once})
        save_settings_partial(conn, {_CUSTOM_FIELDS_KEY: json.dumps(fields), _LOCK_BUYER_KEY: lock,
                                     _COLUMN_NAMES_KEY: json.dumps(names), _FEE_WORDS_KEY: json.dumps(words),
                                     _FEE_VAT_RATE_KEY: rate,
                                     _SUPPLIER_RULES_KEY: json.dumps(supplier_rules), _GRID_COLOUR_KEY: grid_colour,
                                     _MISSING_COLOUR_KEY: missing_colour, _NOT_USED_COLOUR_KEY: not_used_colour,
                                     _OWN_NAMES_KEY: json.dumps(own_names),
                                     _EVALUATOR_HOURS_KEY: hours, _LOOKUP_REUSE_KEY: '1' if lookup_reuse else '0',
                                     _INBOX_ENABLED_KEY: '1' if payload.get("inbox_enabled") else '0',
                                     _INBOX_FOLDER_KEY: folder, _INBOX_DONE_FOLDER_KEY: done_folder,
                                     _INBOX_MAX_FILES_KEY: max_files, _INBOX_SINCE_KEY: since})
        buyers = _buyer_config(conn, user)
        inbox_cfg = _inbox_settings(conn)
        inbox.rematch_suppliers(conn, supplier_names, ids)
    return {"status": "ok", "custom_fields": fields, **buyers, "own_names": own_names, "table_columns": table_columns,
            "table_live": cb.table_live_columns(fields, metrics), "table_rows": rows_at_once, "metrics": metrics,
            "column_names": names, "column_labels": cb.COLUMN_FIELD_LABELS, "fee_words": words,
            "supplier_names": supplier_names, "fee_vat_rate": rate, "supplier_rules": supplier_rules,
            "evaluator_max_hours": hours, "lookup_reuse": lookup_reuse, "grid_colour": grid_colour,
            "missing_colour": missing_colour, "not_used_colour": not_used_colour, **inbox_cfg}


@router.post("/cars-bought/supplier-names")
def cars_bought_add_supplier_name(payload: dict, user: str = Depends(require_page_api(_PAGE))):
    """Admin only: {text, supplier} from the Add Supplier popup. Adds one row to
    Supplier Names, or changes the row with that text, then looks the pending
    invoices up again."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can add a supplier"}
    row = {"text": (payload or {}).get("text"), "supplier": (payload or {}).get("supplier")}
    try:
        names = cb.clean_supplier_names(_supplier_names() + [row])
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    contact_id = cb.to_int((payload or {}).get("contact_id"))
    with get_vehicle_database_connection() as conn:
        ss.save_invoice_names(conn, names)
        if contact_id is not None:
            supplier_id = ss.find_or_add_supplier(conn, str(row["supplier"]))
            ss.save_contacts(conn, cb.clean_supplier_contacts(
                [{"supplier_id": supplier_id, "contact_id": contact_id, "contact_name": payload.get("contact_name")}]))
    ids = _supplier_ids()
    with get_database_connection() as conn:
        updated = inbox.rematch_suppliers(conn, names, ids)
    return {"status": "ok", "supplier_names": names, "updated": updated, "files": _inbox_list()}


_contacts_cache: dict = {}      # {"contacts", "cached_at"}, until the next dbt run


@router.get("/cars-bought/supplier-contacts")
def cars_bought_supplier_contacts(user: str = Depends(require_page_api(_PAGE))):
    """Admin only: our suppliers, and the names in contacts_suppliers__s to map them to."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can map suppliers"}
    held = _contacts_cache.get("held")
    if not held or not _sf_cache_is_fresh(held["cached_at"]):
        try:
            with SnowflakeClient() as sf:
                rows, _ = sf.execute(f"SELECT id, name FROM {kpi._VEHICLE_DB}.SOURCE_PURCHASES_B2B.CONTACTS_SUPPLIERS "
                                     "WHERE deleted_at IS NULL AND name IS NOT NULL")
        except Exception as e:
            logger.error(f"Cars Bought: Apex suppliers not read: {e}")
            return {"status": "error", "message": f"Could not read the Apex suppliers: {e}"}
        held = {"contacts": cb.contact_options(rows), "cached_at": time.time()}
        _contacts_cache["held"] = held
    with get_vehicle_database_connection() as conn:
        suppliers = ss.list_suppliers(conn)
    return {"status": "ok", "contacts": held["contacts"], "suppliers": suppliers}


def _max_age_hours(conn) -> float:
    try:
        return float(_read_setting(conn, _EVALUATOR_HOURS_KEY) or cb.DEFAULT_EVALUATOR_MAX_HOURS)
    except ValueError:
        return float(cb.DEFAULT_EVALUATOR_MAX_HOURS)


def _lookup_reuse(conn) -> bool:
    return _read_setting(conn, _LOOKUP_REUSE_KEY) != '0'


def _fill_buyer_figures(rows: list) -> None:
    """Put the buyer's current RRP and prep cost for each car in its custom
    values. For rows nobody has typed in yet: a saved figure replaces what is there."""
    try:
        with get_vehicle_database_connection() as conn:
            found = vs.fetch_buyer_figures(conn, [r["vrm"] for r in rows])
    except Exception as e:
        logger.warning(f"Cars Bought: buyer figures not read: {e}")
        return
    for r in rows:
        figures = found.get(r["vrm"]) or {}
        for key in vs.BUYER_FIGURES:
            if figures.get(key) is not None:
                r["custom"] = {**(r.get("custom") or {}), key: figures[key]}


def _connect_purchase(vehicles: list, user: str) -> None:
    """A submitted purchase's cars go in the shared tables. The purchase is
    already saved and keeps its own copy, so a failure here is only logged."""
    try:
        with get_vehicle_database_connection() as conn:
            vs.ensure_vehicles_exist(conn, [v["vrm"] for v in vehicles])
            conn.commit()
            vs.save_buyer_figures(conn, cb.buyer_figures_from_rows(vehicles), user)
    except Exception as e:
        logger.error(f"Cars Bought: purchase not connected to the shared tables: {e}")


def _evaluator_data(vrms: list, supplier_id=None) -> dict:
    with get_database_connection() as conn:
        hours = _max_age_hours(conn)
    try:
        with get_vehicle_database_connection() as conn:
            vs.ensure_colour_column(conn)
            return cb.evaluator_lookup(conn, vrms, supplier_id, hours)
    except Exception as e:
        logger.warning(f"Cars Bought: evaluator lookup failed: {e}")
        return {}


def _enrich(rows: list, supplier_id=None, use_evaluator: bool = True) -> list:
    """Add the evaluator's data (CAP, Autotrader and CAP valuations, list
    price), when it is no older than the Cars Bought setting, and any earlier
    purchase of the same VRM to each row. Rows keep what the invoice said;
    evaluator values only fill gaps. cap_source records where CAP came from."""
    vrms = [r["vrm"] for r in rows]
    found = _evaluator_data(vrms, supplier_id) if use_evaluator else {}
    with get_database_connection() as conn:
        bought = cb.already_bought(conn, vrms)
    for r in rows:
        ev = found.get(r["vrm"])
        r["from_evaluator"] = bool(ev and ev.get("evaluator_supplier"))
        if ev:
            if not r.get("cap_id") and ev.get("cap_id"):
                r["cap_source"] = "evaluator"
            for k, v in ev.items():
                if r.get(k) in (None, "") and v is not None:
                    r[k] = v
            if r.get("mileage") is None and ev.get("evaluator_mileage") is not None:
                r["mileage"] = ev["evaluator_mileage"]
        r["already_bought"] = bought.get(r["vrm"])
        r.setdefault("fees", [])
        r.setdefault("custom", {})
    _fill_buyer_figures(rows)
    return rows


# Reading an invoice (Textract, one call per page) can take longer than API Gateway's
# 29 second limit, so it runs in a thread and the page polls for the result.
# The result is also written to S3, as a poll can land on another container.
_jobs: dict = {}
_jobs_lock = threading.Lock()


def _job_key(job_id: str) -> str:
    return f"applications/pricing/uk-vehicle-list-evaluator/{_S3_ENV}/cars_bought/jobs/{job_id}.json"


def _finish_job(job_id: str, result: dict) -> None:
    with _jobs_lock:
        _jobs[job_id] = result
    try:
        import boto3
        boto3.client("s3", region_name=s3_bucket_region).put_object(
            Bucket=s3_bucketname, Key=_job_key(job_id), Body=json.dumps(result).encode(),
            ContentType="application/json")
    except Exception as e:
        logger.warning(f"Cars Bought: job {job_id} result not saved to S3: {e}")


def _run_extract(job_id: str, filename: str, data: bytes) -> None:
    def stage(name: str) -> None:
        # Which step the read is on, shown in the page's progress panel.
        with _jobs_lock:
            _jobs[job_id] = {"status": "running", "stage": name}
    try:
        result = _extract(filename, data, stage)
    except Exception as e:
        logger.error(f"Cars Bought: reading {filename} failed: {e}")
        result = {"status": "error", "message": f"{filename}: {type(e).__name__}: {e}"}
    _finish_job(job_id, result)


@router.post("/cars-bought/extract")
def cars_bought_extract(request: Request, payload: dict, user: str = Depends(require_page_api(_PAGE))):
    """{filename, data (base64)} -> {job_id}; poll /cars-bought/extract/{job_id}."""
    filename = str((payload or {}).get("filename") or "")[:200]
    try:
        data = base64.b64decode(str((payload or {}).get("data") or ""), validate=True)
    except (binascii.Error, ValueError):
        return {"status": "error", "message": f"{filename}: the file could not be read"}
    if not data or len(data) > cb.MAX_UPLOAD_BYTES:
        return {"status": "error", "message": f"{filename}: empty, or larger than 4 MB"}
    try:
        cb.file_kind(filename)
    except ValueError as e:
        return {"status": "error", "message": f"{filename}: {e}"}
    _track(request, 'cars_bought_extract', '/cars-bought', {'filename': filename})
    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "stage": "read"}
    threading.Thread(target=_run_extract, args=(job_id, filename, data), daemon=True).start()
    return {"status": "running", "job_id": job_id}


@router.get("/cars-bought/extract/{job_id}")
def cars_bought_extract_result(job_id: str, user: str = Depends(require_page_api(_PAGE))):
    if not job_id.isalnum():
        return {"status": "error", "message": "Bad job id"}
    with _jobs_lock:
        result = _jobs.get(job_id)
    if result is not None:
        return result
    # Another container is reading it: only the finished result is in S3.
    try:
        import boto3
        obj = boto3.client("s3", region_name=s3_bucket_region).get_object(Bucket=s3_bucketname, Key=_job_key(job_id))
        return json.loads(obj["Body"].read())
    except Exception:
        return {"status": "running"}


def _extract(filename: str, data: bytes, stage=lambda name: None) -> dict:
    """The invoice's header and vehicle rows, filled in from the evaluator.
    stage() is told each step: read, scan (Textract), match."""
    with get_database_connection() as conn:
        names, fee_words, fee_vat_rate, supplier_rules = _reading_settings(conn)
        own_names = _own_names(conn)
    supplier_names = _supplier_names()
    try:
        result, read_by = cb.read_invoice(filename, data, names, fee_words, supplier_names, fee_vat_rate,
                                          on_scan=lambda: stage("scan"), supplier_rules=supplier_rules)
    except ValueError as e:
        return {"status": "error", "message": f"{filename}: {e}"}
    if not result["vehicles"]:
        return {"status": "error", "message": f"{filename}: no vehicles found"}
    stage("match")
    result["supplier_suggestions"] = [] if result["supplier_matched"] else cb.suggest_suppliers(result["found_text"], own_names)

    # The original file is kept so the batch can be checked against it later.
    # Not saving it is reported but does not stop the read.
    ext = os.path.splitext(filename)[1].lower() or ".bin"
    s3_key = f"applications/pricing/uk-vehicle-list-evaluator/{_S3_ENV}/cars_bought/{_uk_now():%Y%m%d}/{uuid.uuid4().hex}{ext}"
    try:
        import boto3
        boto3.client("s3", region_name=s3_bucket_region).put_object(Bucket=s3_bucketname, Key=s3_key, Body=data)
    except Exception as e:
        logger.warning(f"Cars Bought: {filename} not saved to S3: {e}")
        s3_key = None
    # CAP and Autotrader come from Look Up CAP & AT, which the buyer presses,
    # not from here. Submit works out the batch's supplier id again, as the
    # buyer can change the name.
    supplier_id = _supplier_ids().get(str(result.get("supplier_name") or "").strip().lower())
    result["vehicles"] = _enrich(result["vehicles"], use_evaluator=False)
    return {"status": "ok", "read_by": read_by, "s3_key": s3_key, "filename": filename,
            "supplier_id": supplier_id, **result}


# ── Invoices from the SharePoint folder (see utils/cars_bought_inbox.py) ─────

def _dispatch_inbox_read(file_id: str) -> None:
    """Read one file in its own Lambda invocation (a thread when running
    locally): a thread is frozen once the response is sent, and nobody may be
    polling for this one."""
    function_name = os.getenv("AWS_LAMBDA_FUNCTION_NAME")
    if os.getenv("LOCAL_ENV") == "1" or not function_name:
        threading.Thread(target=_read_inbox_file, args=(file_id,), daemon=True).start()
        return
    try:
        from utils.lambda_http import LambdaRequestsManager
        LambdaRequestsManager().simulate_http(
            aws_context=SimpleNamespace(function_name=function_name),
            path=BASE_URL + "/api/cars-bought/inbox-run",
            method="POST",
            data={"file_id": file_id, "secret": worker_secret()},
        )
    except Exception as e:
        logger.error(f"Cars Bought: reading SharePoint file {file_id} could not be started: {e}")
        with get_database_connection() as conn:
            inbox.save_failed(conn, file_id, f"Could not start reading the file: {e}", _uk_now())


def _read_inbox_file(file_id: str) -> None:
    with get_database_connection() as conn:
        row = inbox.claim(conn, file_id, _uk_now())
        settings = _inbox_settings(conn)
    if not row:
        return
    filename = row["filename"]

    def stage(name: str) -> None:
        # Only for the page's progress bars, so a failure here must not stop the read.
        try:
            with get_database_connection() as conn:
                inbox.save_stage(conn, file_id, name)
        except Exception as e:
            logger.warning(f"Cars Bought: progress for {filename} not saved: {e}")

    try:
        cb.file_kind(filename)
        if (row["size_bytes"] or 0) > cb.MAX_UPLOAD_BYTES:
            raise ValueError("larger than 4 MB")
        client = SharePointClient()
        data = client.get_item_content(row["drive_id"], file_id)
        stage("read")
        result = _extract(filename, data, stage)
        stage("save")
    except Exception as e:
        logger.error(f"Cars Bought: reading SharePoint file {filename} failed: {e}")
        result = {"status": "error", "message": f"{filename}: {e}"}
    if result["status"] != "ok":
        with get_database_connection() as conn:
            inbox.save_failed(conn, file_id, result.get("message") or "The file could not be read", _uk_now())
        return
    with get_database_connection() as conn:
        inbox.save_read(conn, file_id, result, _uk_now())

    # Dev and prod have separate databases but read the same folder, so only
    # prod moves files: dev moving one would hide it from prod for good.
    moved_to, move_error = None, "Only the live site moves files"
    if row.get("moved_to"):
        moved_to, move_error = row["moved_to"], None        # read again: it has already moved
    elif _S3_ENV == 'prod':
        try:
            client.move_to_subfolder(row["drive_id"], file_id, settings["inbox_folder"],
                                     settings["inbox_done_folder"], filename)
            moved_to, move_error = settings["inbox_done_folder"], None
        except Exception as e:
            logger.warning(f"Cars Bought: SharePoint file {filename} not moved: {e}")
            denied = isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code == 403
            move_error = "The app has no permission to move files in SharePoint" if denied else str(e)
    with get_database_connection() as conn:
        inbox.save_move(conn, file_id, moved_to, move_error)


@router.post("/api/cars-bought/inbox-run")
def cars_bought_inbox_run(data: dict):
    """Internal worker route, invoked by our own Lambda. Reads one file; it
    never lists the folder, so a worker can never start another worker."""
    check_worker_secret(data.get("secret"))
    _read_inbox_file(str(data.get("file_id") or ""))
    return {"ok": True}


def _inbox_list() -> list:
    with get_database_connection() as conn:
        return [_json_row(r) for r in inbox.list_open(conn, _uk_now())]


@router.get("/cars-bought/inbox")
def cars_bought_inbox(user: str = Depends(require_page_api(_PAGE))):
    return {"status": "ok", "files": _inbox_list()}


@router.post("/cars-bought/inbox/check")
def cars_bought_inbox_check(user: str = Depends(require_page_api(_PAGE))):
    """List the folder and start reading any new files. One small SharePoint
    call when there is nothing new."""
    with get_database_connection() as conn:
        settings = _inbox_settings(conn)
    if not settings["inbox_enabled"]:
        return {"status": "ok", "found": 0, "files": _inbox_list()}
    try:
        drive_id, files = SharePointClient().list_folder(settings["inbox_folder"])
    except Exception as e:
        logger.error(f"Cars Bought: SharePoint folder {settings['inbox_folder']} not listed: {e}")
        return {"status": "error", "message": f"Could not read the invoice folder: {e}", "files": _inbox_list()}
    with get_database_connection() as conn:
        new, counts = inbox.add_new(conn, drive_id, files, settings["inbox_since"], settings["inbox_max_files"], _uk_now())
    for file_id in new:
        _dispatch_inbox_read(file_id)
    return {"status": "ok", "found": len(new), **counts, "files": _inbox_list()}


@router.get("/cars-bought/inbox/{file_id}")
def cars_bought_inbox_file(file_id: str, user: str = Depends(require_page_api(_PAGE))):
    """A pending invoice as read, ready for the buyer. Already Bought is worked
    out again, as a car may have been submitted since the file was read."""
    with get_database_connection() as conn:
        row = inbox.read_one(conn, file_id)
        if not row or row["status"] != "pending":
            return {"status": "error", "message": "This invoice is no longer pending"}
        result = row["result"]
        bought = cb.already_bought(conn, [v["vrm"] for v in result["vehicles"]])
        own_names = _own_names(conn)
    for v in result["vehicles"]:
        v["already_bought"] = bought.get(v["vrm"])
    _fill_buyer_figures(result["vehicles"])
    result["supplier_suggestions"] = ([] if result.get("supplier_matched")
                                      else cb.suggest_suppliers(result.get("found_text"), own_names))
    return {**result, "status": "ok"}


def _saved_copy(s3_key) -> bytes | None:
    """The copy of an invoice kept when it was read, or None if it cannot be had."""
    if not s3_key:
        return None
    try:
        import boto3
        return boto3.client("s3", region_name=s3_bucket_region).get_object(Bucket=s3_bucketname, Key=s3_key)["Body"].read()
    except Exception as e:
        logger.warning(f"Cars Bought: saved copy {s3_key} not read: {e}")
        return None


def _show_file(filename: str, data: bytes) -> Response:
    """A file for the browser to open in its own tab where it can (PDF, photo).
    A saved email is downloaded instead, so nothing in it can run on this site."""
    media_type = mimetypes.guess_type(filename or "")[0] or "application/octet-stream"
    how = "attachment" if cb.file_kind_or_none(filename) in ("html", "email") else "inline"
    return Response(content=data, media_type=media_type,
                    headers={"Content-Disposition": f"{how}; filename*=UTF-8''{quote(filename or 'invoice')}"})


@router.get("/cars-bought/inbox/{file_id}/view")
def cars_bought_inbox_view(file_id: str, user: str = Depends(require_page_api(_PAGE))):
    """The invoice file: the saved copy, else SharePoint (a file that could not
    be read has no saved copy). Its SharePoint ID still works after a move."""
    with get_database_connection() as conn:
        row = inbox.read_one(conn, file_id)
    if not row:
        raise HTTPException(status_code=404, detail="Invoice not found")
    data = _saved_copy((row["result"] or {}).get("s3_key"))
    if data is None:
        try:
            data = SharePointClient().get_item_content(row["drive_id"], file_id)
        except Exception as e:
            logger.error(f"Cars Bought: {row['filename']} not read from SharePoint: {e}")
            raise HTTPException(status_code=404, detail="The file could not be found in SharePoint")
    return _show_file(row["filename"], data)


@router.post("/cars-bought/inbox/{file_id}/retry")
def cars_bought_inbox_retry(file_id: str, user: str = Depends(require_page_api(_PAGE))):
    with get_database_connection() as conn:
        changed = inbox.retry(conn, file_id, _uk_now())
    if changed:
        _dispatch_inbox_read(file_id)
    return {"status": "ok", "files": _inbox_list()}


@router.post("/cars-bought/inbox/{file_id}/ignore")
def cars_bought_inbox_ignore(file_id: str, user: str = Depends(require_page_api(_PAGE))):
    with get_database_connection() as conn:
        inbox.ignore(conn, file_id, user, _uk_now())
    return {"status": "ok", "files": _inbox_list()}


@router.post("/cars-bought/from-evaluator")
def cars_bought_from_evaluator(payload: dict, user: str = Depends(require_page_api(_PAGE))):
    """{supplier_id, vrms (pasted text)} -> rows from the evaluator's data."""
    vrms = cb.parse_vrm_list(str((payload or {}).get("vrms") or ""))
    if not vrms:
        return {"status": "error", "message": "Paste at least one VRM"}
    if len(vrms) > 500:
        return {"status": "error", "message": "500 VRMs at most in one batch"}
    supplier_id = cb.to_int((payload or {}).get("supplier_id"))
    rows = _enrich([{"vrm": v} for v in vrms], supplier_id)
    # The evaluator's list price is the starting price; the buyer corrects it
    # if the agreed price was different.
    for r in rows:
        if r.get("vehicle_gross") is None and r.get("evaluator_list_price") is not None:
            r["vehicle_gross"] = r["evaluator_list_price"]
    missing = [r["vrm"] for r in rows if not r["from_evaluator"]]
    return {"status": "ok", "vehicles": rows, "not_found": missing}


@router.post("/cars-bought/cap-lookup")
def cars_bought_cap_lookup(payload: dict, user: str = Depends(require_page_api(_PAGE))):
    """{vehicles: [{vrm, mileage}]} -> {results: {vrm: {cap_id, make, model,
    derivative, cap_clean, at_retail_valuation, at_rating, note, from_store}}}.
    Figures stored within the data age setting are reused, unless the setting
    is off; the rest are called live and stored for every page. The page sends
    one car per request (8 at once), so a slow car stays inside the 29 second
    limit on its own."""
    items = [(cb.clean_vrm(v.get("vrm")), cb.to_int(v.get("mileage")))
             for v in ((payload or {}).get("vehicles") or [])[:8] if cb.clean_vrm(v.get("vrm"))]
    if not items:
        return {"status": "error", "message": "No VRMs to look up"}
    with get_database_connection() as conn:
        hours = _max_age_hours(conn) if _lookup_reuse(conn) else None
    stored = _stored_lookups(items, hours) if hours else {}

    def store(vrm, mileage, cap, at):
        from .assess_vehicle import _ensure_columns
        try:
            with get_vehicle_database_connection() as conn:
                _ensure_columns(conn)
                vs.save_manual_check(conn, vrm, mileage, user, cap, at)
        except Exception as e:
            logger.error(f"Cars Bought: lookup for {vrm} not stored: {e}")

    live = cb.manual_check([i for i in items if i[0] not in stored], store)
    return {"status": "ok", "results": {**live, **stored}}


def _stored_lookups(items: list, hours: float) -> dict:
    pairs = [(vrm, mileage) for vrm, mileage in items if mileage]
    try:
        with get_vehicle_database_connection() as conn:
            cached = vs.fetch_cached_valuations(conn, pairs, ttl_hours=hours)
            known = vs.fetch_known_vehicles(conn, [vrm for vrm, _ in pairs])
    except Exception as e:
        logger.warning(f"Cars Bought: stored lookups not read: {e}")
        return {}
    return cb.stored_results(items, cached, known)


@router.post("/cars-bought/submit")
def cars_bought_submit(request: Request, payload: dict, user: str = Depends(require_page_api(_PAGE))):
    """{batch: {...}, vehicles: [...]} -> stored. CAP is looked up first, with
    the page's Look Up CAP button."""
    batch = (payload or {}).get("batch") or {}
    vehicles = (payload or {}).get("vehicles") or []
    supplier = str(batch.get("supplier_name") or "").strip()
    date = cb.to_date(batch.get("purchase_date"))
    errors = []
    with get_database_connection() as conn:
        bc = _buyer_config(conn, user)
    # Locked: the buyer is whoever is logged in, whatever the page sent.
    buyer = bc["me"] if bc["lock_buyer"] else str(batch.get("buyer") or "").strip()
    if not buyer:
        errors.append("Buyer is missing")
    elif not bc["lock_buyer"] and buyer not in bc["buyers"]:
        errors.append(f"{buyer} is not in the Buyer list")
    if not supplier:
        errors.append("Supplier name is missing")
    if not date:
        errors.append("Purchase date is missing")
    if not vehicles:
        errors.append("There are no vehicles")
    for v in vehicles:
        v["vrm"] = cb.clean_vrm(v.get("vrm"))
    if any(not v["vrm"] for v in vehicles):
        errors.append("A row has no VRM")
    inbox_file_id = str(batch.get("inbox_file_id") or "")
    if inbox_file_id:
        with get_database_connection() as conn:
            pending = inbox.read_one(conn, inbox_file_id)
        if not pending or pending["status"] != "pending":
            errors.append("This invoice has already been submitted or ignored")
    vrms = [v["vrm"] for v in vehicles]
    dupes = sorted({x for x in vrms if vrms.count(x) > 1})
    if dupes:
        errors.append("VRM on more than one row: " + ", ".join(dupes))
    with get_database_connection() as conn:
        fields = _custom_fields(conn)
    for f in fields:
        # Not for this supplier: not asked for, and nothing typed before the supplier was changed is kept.
        if not cb.field_applies(f, supplier):
            for v in vehicles:
                (v.get("custom") or {}).pop(f["key"], None)
            continue
        if f.get("required"):
            blank = [v["vrm"] for v in vehicles if (v.get("custom") or {}).get(f["key"]) in (None, "")]
            if blank:
                errors.append(f"{f['label']} is missing for " + ", ".join(blank))
    if errors:
        return {"status": "error", "message": "; ".join(errors)}

    # CAP is looked up before Submit (the Look Up CAP button), not here: a
    # long batch looked up during Submit could pass the 29 second limit.
    cap_notes = {v["vrm"]: "no CAP ID" for v in vehicles if not cb.to_int(v.get("cap_id"))}

    # From the name on the form, as the buyer may have changed it since the file was read.
    _connect_to_shared_tables()
    with get_vehicle_database_connection() as conn:
        supplier_id = ss.find_or_add_supplier(conn, supplier[:200])
    batch_row = {"buyer": buyer[:100], "supplier_name": supplier[:200], "supplier_id": supplier_id,
                 "invoice_number": (str(batch.get("invoice_number") or "").strip() or None),
                 "purchase_date": date, "source": batch.get("source") if batch.get("source") in ("evaluator", "sharepoint") else "upload",
                 "filename": batch.get("filename"), "s3_key": batch.get("s3_key"),
                 "invoice_net": batch.get("invoice_net"), "invoice_vat": batch.get("invoice_vat"),
                 "invoice_gross": batch.get("invoice_gross"), "notes": batch.get("notes")}
    with get_database_connection() as conn:
        batch_id = cb.save_batch(conn, batch_row, vehicles, user, _uk_now())
        if inbox_file_id:
            inbox.mark_submitted(conn, inbox_file_id, batch_id, user, _uk_now())
    _connect_purchase(vehicles, user)
    _track(request, 'cars_bought_submit', '/cars-bought', {'batch_id': batch_id, 'vehicles': len(vehicles)})
    return {"status": "ok", "batch_id": batch_id, "cap_not_found": cap_notes}


@router.get("/cars-bought/batches")
def cars_bought_batches(user: str = Depends(require_page_api(_PAGE))):
    with get_database_connection() as conn:
        return {"status": "ok", "batches": cb.list_batches(conn)}


def _json_row(r: dict) -> dict:
    out = {}
    for k, v in r.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif v is not None and type(v).__name__ == "Decimal":
            out[k] = float(v)
        else:
            out[k] = v
    return out


@router.get("/cars-bought/batches/{batch_id}")
def cars_bought_batch(batch_id: int, user: str = Depends(require_page_api(_PAGE))):
    with get_database_connection() as conn:
        rows = cb.read_vehicles(conn, batch_id=batch_id)
    return {"status": "ok", "vehicles": [_json_row(r) for r in rows]}


@router.get("/cars-bought/vehicles")
def cars_bought_vehicles(user: str = Depends(require_page_api(_PAGE))):
    """Every bought car, for the All Vehicles Bought table."""
    with get_database_connection() as conn:
        rows = cb.read_vehicles(conn)
    return {"status": "ok", "vehicles": [_json_row(r) for r in cb.table_rows(rows)]}


@router.post("/cars-bought/import-csv")
def cars_bought_import_csv(payload: dict, user: str = Depends(require_page_api(_PAGE))):
    """Admin only: the old Cars Bought sheet as a CSV. {filename, data (base64),
    mapping, save}. Without save, only says what importing would do. With it,
    saves purchases until the time runs out and says how many are left: the page
    asks again, and what is already saved is skipped, so nothing goes in twice."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can import"}
    filename = str((payload or {}).get("filename") or "")[:200]
    try:
        data = base64.b64decode(str((payload or {}).get("data") or ""), validate=True)
    except (binascii.Error, ValueError):
        return {"status": "error", "message": f"{filename}: the file could not be read"}
    if not data or len(data) > cb.MAX_UPLOAD_BYTES:
        return {"status": "error", "message": f"{filename}: empty, or larger than 4 MB"}
    try:
        headings, rows = cb_import.read_csv(data)
    except ValueError as e:
        return {"status": "error", "message": f"{filename}: {e}"}
    with get_database_connection() as conn:
        fields = _custom_fields(conn)
        stored = cb_import.stored_purchases(conn)
    mapping = (payload or {}).get("mapping")
    if not isinstance(mapping, dict):
        mapping = cb_import.suggest_mapping(headings, fields)
    planned = cb_import.plan(headings, rows, mapping, fields, stored)
    known = _supplier_ids()
    if not payload.get("save"):
        names = {b["supplier_name"].lower(): b["supplier_name"] for b in planned["batches"]}
        return {"status": "ok", "headings": headings, "first_row": rows[0] if rows else [], "mapping": mapping,
                "options": cb_import.field_options(fields), "missing": planned["missing"], "rows": planned["rows"],
                "cars": planned["cars"], "batches": len(planned["batches"]), "already_here": planned["already_here"],
                "bought_on_another_date": planned["bought_on_another_date"],
                "new_suppliers": sorted(n for low, n in names.items() if low not in known),
                "problems": planned["problems"][:200], "problems_total": len(planned["problems"])}
    if planned["missing"]:
        return {"status": "error", "message": "Choose a column for: " + ", ".join(planned["missing"])}

    # Well inside the 29 second limit, with room for the file to be read first.
    _connect_to_shared_tables()
    stop_at = time.monotonic() + 15
    saved_batches = saved_cars = 0
    for batch in planned["batches"]:
        if time.monotonic() > stop_at:
            break
        with get_vehicle_database_connection() as conn:
            supplier_id = ss.find_or_add_supplier(conn, batch["supplier_name"])
            # Stubs only: an old RRP or prep cost must not replace a buyer's current figure.
            vs.ensure_vehicles_exist(conn, [v["vrm"] for v in batch["vehicles"]])
            conn.commit()
        with get_database_connection() as conn:
            cb.save_batch(conn, {**{k: v for k, v in batch.items() if k != "vehicles"}, "supplier_id": supplier_id,
                                 "source": cb_import.SOURCE, "filename": filename}, batch["vehicles"], user, _uk_now())
        saved_batches += 1
        saved_cars += len(batch["vehicles"])
    return {"status": "ok", "saved_cars": saved_cars, "batches_left": len(planned["batches"]) - saved_batches}


@router.post("/cars-bought/vehicles/{vehicle_id}/custom")
def cars_bought_vehicle_custom(vehicle_id: int, payload: dict, user: str = Depends(require_page_api(_PAGE))):
    """{key, value}: a buyer filling in one custom field in the table."""
    key = str((payload or {}).get("key") or "")
    with get_database_connection() as conn:
        field = next((f for f in _custom_fields(conn) if f.get("key") == key), None)
        if not field:
            return {"status": "error", "message": "That field no longer exists"}
        try:
            value = cb.clean_custom_value(field, (payload or {}).get("value"))
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        vrm = cb.update_vehicle_custom(conn, vehicle_id, key, value)
    if not vrm:
        return {"status": "error", "message": "Vehicle not found"}
    if key in vs.BUYER_FIGURES:
        try:
            with get_vehicle_database_connection() as conn:
                vs.save_buyer_figures(conn, [{"vrm": vrm, key: value}], user)
        except Exception as e:
            logger.error(f"Cars Bought: {key} for {vrm} not saved to the shared figures: {e}")
    return {"status": "ok", "value": value}


# ── Vehicle metrics (see utils/vehicle_metrics.py) ───────────────────────────
# Nothing is read from Snowflake until a metric has been set up and switched on.

_metric_cache: dict = {}    # metric id -> {"key", "figures", "cached_at"}
_metric_cache_lock = threading.Lock()


def _metric_s3_key(metric_id: str) -> str:
    return f"applications/pricing/uk-vehicle-list-evaluator/{_S3_ENV}/cars_bought/metrics/{metric_id}.json"


def _read_figures(metric: dict) -> list:
    """[match figures, fallback figures], each {key: [figure, rows]}."""
    out = []
    with SnowflakeClient() as sf:
        for pairs in (metric["match"], metric["fallback"]):
            if not pairs:
                out.append({})
                continue
            sql, params = vm.read_sql(metric, pairs, kpi._VEHICLE_DB)
            rows, _ = sf.execute(sql, params)
            out.append(vm.rows_to_figures(rows))
    return out


def _metric_figures(metric: dict, force: bool = False) -> tuple:
    """(figures as _read_figures, when read). Kept in memory and in S3 until the
    next dbt run, so a fresh Lambda does not pay for the query again. Changing
    the metric changes the key, so its old figures are not used."""
    key = json.dumps(metric, sort_keys=True)
    with _metric_cache_lock:
        held = _metric_cache.get(metric["id"])
    if not force:
        if not held or held["key"] != key:
            try:
                import boto3
                obj = boto3.client("s3", region_name=s3_bucket_region).get_object(Bucket=s3_bucketname, Key=_metric_s3_key(metric["id"]))
                held = json.loads(obj["Body"].read())
            except Exception:
                held = None
        if held and held["key"] == key and _sf_cache_is_fresh(held["cached_at"]):
            with _metric_cache_lock:
                _metric_cache[metric["id"]] = held
            return held["figures"], held["cached_at"]
    err = vm.validate(metric, kpi._source_columns(metric["table"]))
    if err:
        raise ValueError(err)
    held = {"key": key, "figures": _read_figures(metric), "cached_at": time.time()}
    with _metric_cache_lock:
        _metric_cache[metric["id"]] = held
    try:
        import boto3
        boto3.client("s3", region_name=s3_bucket_region).put_object(
            Bucket=s3_bucketname, Key=_metric_s3_key(metric["id"]), Body=json.dumps(held).encode(), ContentType="application/json")
    except Exception as e:
        logger.warning(f"Cars Bought: metric {metric['id']} not saved to S3: {e}")
    return held["figures"], held["cached_at"]


def _metric_admin_error(user: str) -> dict | None:
    return None if is_admin(user) else {"status": "error", "message": "Only the admin can change the metrics"}


@router.get("/cars-bought/metric-values/{metric_id}")
def cars_bought_metric_values(metric_id: str, force: bool = False, user: str = Depends(require_page_api(_PAGE))):
    """One metric's figure for every bought car: {vehicle id: {value, rows,
    matched, level}}. One request per metric, so a slow one holds up no other
    and each stays inside the 29 second limit."""
    with get_database_connection() as conn:
        metric = next((m for m in _metrics(conn) if m["id"] == metric_id), None)
        rows = cb.read_vehicles(conn) if metric and metric.get("enabled") else []
    if not metric or not metric.get("enabled"):
        return {"status": "error", "message": "That metric is not switched on"}
    if not rows:
        return {"status": "ok", "values": {}}       # no bought cars: nothing to ask Snowflake for
    try:
        (figures, fallback), cached_at = _metric_figures(metric, force and is_admin(user))
    except Exception as e:
        logger.error(f"Cars Bought: metric {metric['label']} not read: {e}")
        return {"status": "error", "message": f"{metric['label']}: {e}"}
    values = {}
    for r in rows:
        hit = vm.figure_for(r, metric, figures, fallback)
        if hit:
            values[str(r["vehicle_id"])] = hit
    return {"status": "ok", "values": values, "cached_at": cached_at}


@router.get("/cars-bought/metric-tables")
def cars_bought_metric_tables(user: str = Depends(require_page_api(_PAGE))):
    """The tables a metric can read from: only their names. Admin only; takes a few seconds."""
    err = _metric_admin_error(user)
    if err:
        return err
    try:
        return {"status": "ok", "tables": kpi._source_tables()}
    except Exception as e:
        logger.error(f"Cars Bought: metric tables not read: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/cars-bought/metric-columns")
def cars_bought_metric_columns(table: str, user: str = Depends(require_page_api(_PAGE))):
    """One table's columns and their types: no rows are read. Admin only."""
    err = _metric_admin_error(user)
    if err:
        return err
    try:
        kpi._source_columns_cache.pop(table, None)
        return {"status": "ok", "columns": vm.column_choices(kpi._source_columns(table))}
    except Exception as e:
        logger.error(f"Cars Bought: metric columns of {table} not read: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/cars-bought/metrics")
def cars_bought_metric_save(payload: dict, user: str = Depends(require_page_api(_PAGE))):
    """Admin only: add or change one metric, checked against the live table. No
    figures are read. A new one also goes on the table's columns."""
    err = _metric_admin_error(user)
    if err:
        return err
    metric = (payload or {}).get("metric")
    try:
        err = vm.validate_shape(metric) or vm.validate(metric, kpi._source_columns(metric["table"]))
    except Exception as e:
        logger.error(f"Cars Bought: metric not checked: {e}")
        return {"status": "error", "message": str(e)}
    if err:
        return {"status": "error", "message": err}
    saved = vm.clean(metric)
    with get_database_connection() as conn:
        metrics = _metrics(conn)
        if not any(m["id"] == saved["id"] for m in metrics) and len(metrics) >= vm.METRICS_MAX:
            return {"status": "error", "message": f"At most {vm.METRICS_MAX} metrics"}
        at = next((i for i, m in enumerate(metrics) if m["id"] == saved["id"]), None)
        if at is None:
            metrics.append(saved)
        else:
            metrics[at] = saved
        return _save_metrics(conn, metrics)


@router.delete("/cars-bought/metrics/{metric_id}")
def cars_bought_metric_delete(metric_id: str, user: str = Depends(require_page_api(_PAGE))):
    err = _metric_admin_error(user)
    if err:
        return err
    with get_database_connection() as conn:
        return _save_metrics(conn, [m for m in _metrics(conn) if m["id"] != metric_id])


def _save_metrics(conn, metrics: list) -> dict:
    fields = _custom_fields(conn)
    current = cb.read_table_columns(_json_setting(conn, _TABLE_COLUMNS_KEY, []), fields, _metrics(conn))
    columns = cb.table_columns_after_metric_save(current, fields, metrics)
    widen_settings_values(conn)
    save_settings_partial(conn, {_METRICS_KEY: json.dumps(metrics), _TABLE_COLUMNS_KEY: json.dumps(columns)})
    return {"status": "ok", "metrics": metrics, "table_columns": columns, "table_live": cb.table_live_columns(fields, metrics)}


@router.post("/cars-bought/metrics/test")
def cars_bought_metric_test(payload: dict, user: str = Depends(require_page_api(_PAGE))):
    """Admin only: run the metric as set up in the form, without saving it, and
    say what it finds. The only place Snowflake is read before a metric is on."""
    err = _metric_admin_error(user)
    if err:
        return err
    metric = (payload or {}).get("metric")
    try:
        err = vm.validate_shape(metric) or vm.validate(metric, kpi._source_columns(metric["table"]))
        if err:
            return {"status": "error", "message": err}
        metric = vm.clean(metric)
        figures, fallback = _read_figures(metric)
    except Exception as e:
        logger.error(f"Cars Bought: metric test failed: {e}")
        return {"status": "error", "message": str(e)}
    with get_database_connection() as conn:
        rows = cb.read_vehicles(conn)
    hits = [vm.figure_for(r, metric, figures, fallback) for r in rows]
    sample = sorted(figures.items(), key=lambda kv: -kv[1][1])[:5]
    return {"status": "ok", "groups": len(figures), "rows": sum(f[1] for f in figures.values()),
            "fallback_groups": len(fallback), "vehicles": len(rows),
            "matched": sum(1 for h in hits if h and h["level"] == "match"),
            "matched_fallback": sum(1 for h in hits if h and h["level"] == "fallback"),
            "sample": [{"key": k, "value": v[0], "rows": v[1]} for k, v in sample]}


@router.get("/cars-bought/batches/{batch_id}/view")
def cars_bought_batch_view(batch_id: int, user: str = Depends(require_page_api(_PAGE))):
    """The file a purchase was read from."""
    with get_database_connection() as conn:
        filename, s3_key = cb.batch_file(conn, batch_id)
    data = _saved_copy(s3_key)
    if data is None:
        raise HTTPException(status_code=404, detail="The file for this purchase was not saved")
    return _show_file(filename, data)


@router.delete("/cars-bought/batches/{batch_id}")
def cars_bought_delete(batch_id: int, user: str = Depends(require_page_api(_PAGE))):
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can delete a batch"}
    with get_database_connection() as conn:
        if cb.delete_batch(conn, batch_id):
            return {"status": "ok"}
    return {"status": "error", "message": "Batch not found"}


@router.get("/cars-bought/export.csv")
def cars_bought_export(date_from: str = "", date_to: str = "", batch_id: int | None = None,
                       user: str = Depends(require_page_api(_PAGE))):
    with get_database_connection() as conn:
        fields = _custom_fields(conn)
        rows = cb.read_vehicles(conn, batch_id=batch_id, date_from=cb.to_date(date_from), date_to=cb.to_date(date_to))
    header, lines = cb.csv_rows(rows, fields)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(lines)
    name = f"cars_bought_batch_{batch_id}.csv" if batch_id else f"cars_bought_{_uk_now():%Y%m%d}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})
