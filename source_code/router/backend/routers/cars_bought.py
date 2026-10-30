"""Cars Bought: read supplier invoices into vehicle purchase records.

See utils/cars_bought.py for how files are read and what is stored.
"""
import base64
import binascii
import csv
import io
import json
import logging
import os
import threading
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from utils import cars_bought as cb
from utils.db_utils import (get_database_connection, get_vehicle_database_connection,
                            save_settings_partial, widen_settings_values, read_role_users, read_user_details)
from .core import (BASE_URL, templates, _track, require_page, require_page_api, is_admin, _uk_now,
                   s3_bucketname, s3_bucket_region, _S3_ENV)

logger = logging.getLogger(__name__)

router = APIRouter()

_PAGE = 'cars_bought'
_CUSTOM_FIELDS_KEY = 'cars_bought_custom_fields'
# '1' = the buyer is always the logged-in user (dropdown locked).
_LOCK_BUYER_KEY = 'cars_bought_lock_buyer'
# How invoices are read: column headings per field, and the words marking a fee line.
_COLUMN_NAMES_KEY = 'cars_bought_column_names'
_FEE_WORDS_KEY = 'cars_bought_fee_words'
_SUPPLIER_NAMES_KEY = 'cars_bought_supplier_names'
_FEE_VAT_RATE_KEY = 'cars_bought_fee_vat_rate'
# Oldest Supplier Stock Evaluator data (hours) used to fill in a car.
_EVALUATOR_HOURS_KEY = 'cars_bought_evaluator_max_hours'


def _read_setting(conn, key: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = cur.fetchone()
    return row[0] if row else ''


def _custom_fields(conn) -> list:
    return cb.read_custom_fields({_CUSTOM_FIELDS_KEY: _read_setting(conn, _CUSTOM_FIELDS_KEY)})


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
def cars_bought_page(request: Request, user: str = Depends(require_page(_PAGE))):
    _track(request, 'page_view', '/cars-bought')
    return templates.TemplateResponse("cars_bought.html", {"request": request, "user": user, "BASE_URL": BASE_URL,
                                                           "is_admin": is_admin(user)})


@router.get("/cars-bought/config")
def cars_bought_config(user: str = Depends(require_page_api(_PAGE))):
    """Suppliers and buyers for the dropdowns, and the custom fields."""
    with get_database_connection() as conn:
        fields = _custom_fields(conn)
        buyers = _buyer_config(conn, user)
        names, fee_words, supplier_names, fee_vat_rate = _reading_settings(conn)
        hours = _max_age_hours(conn)
    try:
        with get_vehicle_database_connection() as conn:
            suppliers = cb.list_suppliers(conn)
    except Exception as e:
        logger.warning(f"Cars Bought: suppliers not read: {e}")
        suppliers = []
    return {"status": "ok", "suppliers": suppliers, "custom_fields": fields, **buyers,
            "column_names": names, "column_labels": cb.COLUMN_FIELD_LABELS, "fee_words": fee_words,
            "supplier_names": supplier_names, "fee_vat_rate": fee_vat_rate,
            "evaluator_max_hours": hours}


def _json_setting(conn, key: str, default):
    try:
        value = json.loads(_read_setting(conn, key) or 'null')
    except ValueError:
        value = None
    return value if isinstance(value, type(default)) else default


def _reading_settings(conn) -> tuple:
    """(column names per field, fee words, supplier names on invoices, VAT %
    on fees) used to read uploaded invoices."""
    try:
        rate = float(_read_setting(conn, _FEE_VAT_RATE_KEY) or cb.DEFAULT_FEE_VAT_RATE)
    except ValueError:
        rate = cb.DEFAULT_FEE_VAT_RATE
    return (cb.read_column_names(_json_setting(conn, _COLUMN_NAMES_KEY, {})),
            _json_setting(conn, _FEE_WORDS_KEY, list(cb.DEFAULT_FEE_WORDS)),
            _json_setting(conn, _SUPPLIER_NAMES_KEY, [dict(s) for s in cb.DEFAULT_SUPPLIER_NAMES]),
            rate)


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
    supplier_names = [{"text": str(s.get("text") or "").strip()[:100], "supplier": str(s.get("supplier") or "").strip()[:100]}
                      for s in (payload.get("supplier_names") or []) if isinstance(s, dict)]
    supplier_names = [s for s in supplier_names if s["text"] and s["supplier"]]
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
    with get_database_connection() as conn:
        widen_settings_values(conn)
        save_settings_partial(conn, {_CUSTOM_FIELDS_KEY: json.dumps(fields), _LOCK_BUYER_KEY: lock,
                                     _COLUMN_NAMES_KEY: json.dumps(names), _FEE_WORDS_KEY: json.dumps(words),
                                     _SUPPLIER_NAMES_KEY: json.dumps(supplier_names), _FEE_VAT_RATE_KEY: rate,
                                     _EVALUATOR_HOURS_KEY: hours})
        buyers = _buyer_config(conn, user)
    return {"status": "ok", "custom_fields": fields, **buyers,
            "column_names": names, "column_labels": cb.COLUMN_FIELD_LABELS, "fee_words": words,
            "supplier_names": supplier_names, "fee_vat_rate": rate, "evaluator_max_hours": hours}


def _max_age_hours(conn) -> float:
    try:
        return float(_read_setting(conn, _EVALUATOR_HOURS_KEY) or cb.DEFAULT_EVALUATOR_MAX_HOURS)
    except ValueError:
        return float(cb.DEFAULT_EVALUATOR_MAX_HOURS)


def _evaluator_supplier_id(name):
    """The Supplier Stock Evaluator supplier with this name, or None."""
    if not name:
        return None
    try:
        with get_vehicle_database_connection() as conn:
            for s in cb.list_suppliers(conn):
                if str(s["supplier_name"]).strip().lower() == str(name).strip().lower():
                    return s["supplier_id"]
    except Exception as e:
        logger.warning(f"Cars Bought: suppliers not read: {e}")
    return None


def _evaluator_data(vrms: list, supplier_id=None) -> dict:
    with get_database_connection() as conn:
        hours = _max_age_hours(conn)
    try:
        with get_vehicle_database_connection() as conn:
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
        names, fee_words, supplier_names, fee_vat_rate = _reading_settings(conn)
    try:
        result, read_by = cb.read_invoice(filename, data, names, fee_words, supplier_names, fee_vat_rate,
                                          on_scan=lambda: stage("scan"))
    except ValueError as e:
        return {"status": "error", "message": f"{filename}: {e}"}
    if not result["vehicles"]:
        return {"status": "error", "message": f"{filename}: no vehicles found"}
    stage("match")

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
    # A supplier the Supplier Stock Evaluator holds (e.g. Enterprise): its
    # stored data for these cars is used straight away. Others wait for the
    # Look Up CAP button.
    supplier_id = _evaluator_supplier_id(result.get("supplier_name"))
    result["vehicles"] = _enrich(result["vehicles"], supplier_id, use_evaluator=supplier_id is not None)
    return {"status": "ok", "read_by": read_by, "s3_key": s3_key, "filename": filename,
            "supplier_id": supplier_id, **result}


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


_LOOKUP_FIELDS = ("cap_id", "make", "model", "derivative", "cap_clean", "at_retail_valuation", "at_rating")


@router.post("/cars-bought/cap-lookup")
def cars_bought_cap_lookup(payload: dict, user: str = Depends(require_page_api(_PAGE))):
    """{vehicles: [{vrm, mileage}], mode} -> {results: {vrm: {...fields,
    source, age_hours, note}}}.
    mode 'auto' (Look Up CAP): the evaluator's stored CAP and Autotrader data
    when recent enough (any supplier), then live CAP for cars it has no CAP
    ID for. mode 'manual' (Manual Check): live CAP and live Autotrader for
    every car, for comparing. The page sends 20 (auto) or 5 (manual) at a
    time, so one request stays inside the 29 second limit."""
    mode = (payload or {}).get("mode") or "auto"
    items = [(cb.clean_vrm(v.get("vrm")), cb.to_int(v.get("mileage")))
             for v in ((payload or {}).get("vehicles") or [])[:25] if cb.clean_vrm(v.get("vrm"))]
    if not items:
        return {"status": "error", "message": "No VRMs to look up"}
    if mode == "manual":
        results = cb.manual_check(items[:8])
        for r in results.values():
            r["source"] = "manual"
        return {"status": "ok", "results": results}

    found = _evaluator_data([v for v, _ in items])
    results = {}
    for vrm, _ in items:
        ev = found.get(vrm)
        if ev:
            results[vrm] = {**{k: ev.get(k) for k in _LOOKUP_FIELDS}, "source": "evaluator",
                            "cap_source": "evaluator" if ev.get("cap_id") else None,
                            "age_hours": ev.get("evaluator_age_hours"), "evaluator_supplier": ev.get("evaluator_supplier")}
    need = [(v, m) for v, m in items if not (results.get(v) or {}).get("cap_id")]
    for vrm, c in cb.cap_lookup(need).items():
        r = results.setdefault(vrm, {})
        for k in ("cap_id", "make", "model", "derivative", "cap_clean"):
            if r.get(k) in (None, "") and c.get(k) not in (None, ""):
                r[k] = c[k]
        r["cap_source"] = "live"
        r.setdefault("source", "live")
        r["note"] = c.get("note") if not c.get("cap_id") else None
    return {"status": "ok", "results": results}


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
    vrms = [v["vrm"] for v in vehicles]
    dupes = sorted({x for x in vrms if vrms.count(x) > 1})
    if dupes:
        errors.append("VRM on more than one row: " + ", ".join(dupes))
    with get_database_connection() as conn:
        fields = _custom_fields(conn)
    for f in fields:
        if f.get("required"):
            blank = [v["vrm"] for v in vehicles if (v.get("custom") or {}).get(f["key"]) in (None, "")]
            if blank:
                errors.append(f"{f['label']} is missing for " + ", ".join(blank))
    if errors:
        return {"status": "error", "message": "; ".join(errors)}

    # CAP is looked up before Submit (the Look Up CAP button), not here: a
    # long batch looked up during Submit could pass the 29 second limit.
    cap_notes = {v["vrm"]: "no CAP ID" for v in vehicles if not cb.to_int(v.get("cap_id"))}

    batch_row = {"buyer": buyer[:100], "supplier_name": supplier[:200], "supplier_id": cb.to_int(batch.get("supplier_id")),
                 "invoice_number": (str(batch.get("invoice_number") or "").strip() or None),
                 "purchase_date": date, "source": "evaluator" if batch.get("source") == "evaluator" else "upload",
                 "filename": batch.get("filename"), "s3_key": batch.get("s3_key"),
                 "invoice_net": batch.get("invoice_net"), "invoice_vat": batch.get("invoice_vat"),
                 "invoice_gross": batch.get("invoice_gross"), "notes": batch.get("notes")}
    with get_database_connection() as conn:
        batch_id = cb.save_batch(conn, batch_row, vehicles, user, _uk_now())
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
