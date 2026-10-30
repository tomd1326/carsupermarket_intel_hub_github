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
    try:
        with get_vehicle_database_connection() as conn:
            suppliers = cb.list_suppliers(conn)
    except Exception as e:
        logger.warning(f"Cars Bought: suppliers not read: {e}")
        suppliers = []
    return {"status": "ok", "suppliers": suppliers, "custom_fields": fields, **buyers}


@router.post("/cars-bought/config")
def cars_bought_save_config(payload: dict, user: str = Depends(require_page_api(_PAGE))):
    """Admin only: custom fields and the buyer lock."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change these settings"}
    fields = cb.clean_custom_fields((payload or {}).get("custom_fields"))
    lock = '1' if (payload or {}).get("lock_buyer") else '0'
    with get_database_connection() as conn:
        widen_settings_values(conn)
        save_settings_partial(conn, {_CUSTOM_FIELDS_KEY: json.dumps(fields), _LOCK_BUYER_KEY: lock})
        buyers = _buyer_config(conn, user)
    return {"status": "ok", "custom_fields": fields, **buyers}


def _enrich(rows: list, supplier_id=None) -> list:
    """Add the evaluator's data (CAP, valuations, list price) and any earlier
    purchase of the same VRM to each row. Rows keep what the invoice said;
    evaluator values only fill gaps."""
    vrms = [r["vrm"] for r in rows]
    try:
        with get_vehicle_database_connection() as conn:
            found = cb.evaluator_lookup(conn, vrms, supplier_id)
    except Exception as e:
        logger.warning(f"Cars Bought: evaluator lookup failed: {e}")
        found = {}
    with get_database_connection() as conn:
        bought = cb.already_bought(conn, vrms)
    for r in rows:
        ev = found.get(r["vrm"])
        r["from_evaluator"] = bool(ev and ev.get("evaluator_supplier"))
        if ev:
            for k, v in ev.items():
                if r.get(k) in (None, "") and v is not None:
                    r[k] = v
            if r.get("mileage") is None and ev.get("evaluator_mileage") is not None:
                r["mileage"] = ev["evaluator_mileage"]
        r["already_bought"] = bought.get(r["vrm"])
        r.setdefault("fees", [])
        r.setdefault("custom", {})
    return rows


@router.post("/cars-bought/extract")
def cars_bought_extract(request: Request, payload: dict, user: str = Depends(require_page_api(_PAGE))):
    """{filename, data (base64)} -> the invoice's header and vehicle rows."""
    filename = str((payload or {}).get("filename") or "")[:200]
    try:
        data = base64.b64decode(str((payload or {}).get("data") or ""), validate=True)
    except (binascii.Error, ValueError):
        return {"status": "error", "message": f"{filename}: the file could not be read"}
    if not data or len(data) > cb.MAX_UPLOAD_BYTES:
        return {"status": "error", "message": f"{filename}: empty, or larger than 4 MB"}
    _track(request, 'cars_bought_extract', '/cars-bought', {'filename': filename})
    read_by = "text"
    try:
        try:
            text = cb.file_to_text(filename, data)
        except cb.ScanNeeded:
            text = cb.ocr_scan(filename, data)
            read_by = "scan"
        result = cb.extract_with_ai(text, filename)
    except ValueError as e:
        return {"status": "error", "message": f"{filename}: {e}"}
    except Exception as e:
        logger.error(f"Cars Bought: reading {filename} failed: {e}")
        return {"status": "error", "message": f"{filename}: {type(e).__name__}: {e}"}
    if not result["vehicles"]:
        return {"status": "error", "message": f"{filename}: no vehicles found"}

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
    result["vehicles"] = _enrich(result["vehicles"])
    return {"status": "ok", "read_by": read_by, "s3_key": s3_key, "filename": filename, **result}


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


@router.post("/cars-bought/submit")
def cars_bought_submit(request: Request, payload: dict, user: str = Depends(require_page_api(_PAGE))):
    """{batch: {...}, vehicles: [...]} -> stored. CAP is looked up for any row
    still missing a CAP ID before saving."""
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

    need_cap = [(v["vrm"], cb.to_int(v.get("mileage"))) for v in vehicles if not cb.to_int(v.get("cap_id"))]
    cap_notes = {}
    if need_cap:
        for vrm, c in cb.cap_lookup(need_cap).items():
            v = next(x for x in vehicles if x["vrm"] == vrm)
            for k in ("cap_id", "make", "model", "derivative", "cap_clean"):
                if v.get(k) in (None, "") and c.get(k) not in (None, ""):
                    v[k] = c[k]
            if not c.get("cap_id"):
                cap_notes[vrm] = c.get("note") or "not found"

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
