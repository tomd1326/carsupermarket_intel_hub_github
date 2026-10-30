"""Cars Bought: supplier invoices read into vehicle purchase records.

Two ways in:
  1. Upload an invoice or sheet (CSV, XLSX, PDF, or a photo). The file is
     turned into plain text and Cortex picks out the vehicles, prices and
     fees, because every supplier lays its invoice out differently. Scanned
     PDFs and photos have no text, so AWS Textract reads them first (it
     handles handwriting); the Lambda role needs textract:AnalyzeDocument.
  2. Pick a supplier the Supplier Stock Evaluator already holds (Enterprise,
     Motability, Purchase Pro ...) and paste VRMs. Mileage, list price and the
     CAP details come from the evaluator's own tables.

The buyer checks the rows, fills in the custom fields, and submits. One
submit is one purchase_batches row with a purchase_vehicles row per car.
"""

import datetime
import io
import json
import logging
import os
import re
from decimal import Decimal, InvalidOperation

import psycopg2.extras

from utils.ddl_once import once_per_database

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 4 * 1024 * 1024     # base64 adds a third; Lambda takes 6 MB
MAX_TEXT_CHARS = 60000                 # what is sent to Cortex for one file
MIN_PDF_TEXT_CHARS = 40                # less than this on a PDF means a scan
MAX_SCAN_PAGES = 10                    # Textract is charged per page

# Fields the buyer fills in. Editable on the page (stored in settings as
# JSON), these are only what a new install starts with.
DEFAULT_CUSTOM_FIELDS = [
    {"key": "rrp", "label": "RRP", "type": "money"},
    {"key": "est_prep_cost", "label": "Estimated Prep Cost", "type": "money"},
]
CUSTOM_FIELD_TYPES = ("money", "number", "text")

# The Buyer dropdown lists the users given this role on the Users page.
BUYER_ROLE = "buyer"

_VRM_CLEAN = re.compile(r"[^A-Z0-9]")


def clean_vrm(value) -> str:
    return _VRM_CLEAN.sub("", str(value or "").upper())


def parse_vrm_list(text: str) -> list:
    """VRMs from pasted text: one per line, or separated by commas/tabs.
    Duplicates dropped, order kept."""
    out, seen = [], set()
    for part in re.split(r"[\n,;\t]+", text or ""):
        vrm = clean_vrm(part)
        if vrm and vrm not in seen:
            seen.add(vrm)
            out.append(vrm)
    return out


def money(value):
    """A money value as a 2dp Decimal, or None. Accepts '1,234.50', '£9,350'."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("£", "").strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def to_int(value):
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except ValueError:
        return None


def to_date(value):
    """ISO (yyyy-mm-dd) or UK (dd/mm/yyyy, dd/mm/yy) date, or None."""
    if not value:
        return None
    if isinstance(value, datetime.date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d.%m.%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# ── Reading the uploaded file ────────────────────────────────────────────────

class ScanNeeded(Exception):
    """A PDF with no text layer: it has to be read by OCR."""


def file_kind(filename: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in (".csv", ".txt"):
        return "csv"
    if ext in (".xlsx", ".xls", ".xlsm"):
        return "excel"
    if ext == ".pdf":
        return "pdf"
    if ext in (".jpg", ".jpeg", ".png"):
        return "image"
    raise ValueError("Only CSV, XLSX, PDF, JPG and PNG files can be read")


def file_to_text(filename: str, data: bytes) -> str:
    """The file as plain text for Cortex. Sheets keep every cell (no header
    row assumed), one line per row, cells separated by ' | '. Raises
    ScanNeeded for a PDF with no text, or a photo."""
    kind = file_kind(filename)
    if kind == "image":
        raise ScanNeeded()
    if kind == "pdf":
        from pypdf import PdfReader
        pages = [p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages]
        text = "\n\n".join(f"--- Page {i + 1} ---\n{t}" for i, t in enumerate(pages) if t.strip())
        if len(text.strip()) < MIN_PDF_TEXT_CHARS:
            raise ScanNeeded()
        return text[:MAX_TEXT_CHARS]

    import pandas as pd
    if kind == "csv":
        sheets = {"CSV": pd.read_csv(io.BytesIO(data), header=None, dtype=str,
                                     keep_default_na=False, encoding_errors="replace")}
    else:
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=str)
    parts = []
    for name, df in sheets.items():
        df = df.fillna("").astype(str)
        lines = []
        for row in df.itertuples(index=False):
            cells = [c.strip() for c in row]
            if any(cells):
                lines.append(" | ".join(cells).rstrip(" |"))
        if lines:
            parts.append(f"--- Sheet {name} ---\n" + "\n".join(lines))
    text = "\n\n".join(parts)
    if not text.strip():
        raise ValueError("The file is empty")
    return text[:MAX_TEXT_CHARS]


def textract_text(blocks: list) -> str:
    """Plain text from a Textract AnalyzeDocument (FORMS) reply: the form's
    label/value pairs first ("Purchase Price: 19500 00"), because on a
    handwritten form the reading order alone can split a label from what was
    written next to it, then every line in reading order."""
    by_id = {b["Id"]: b for b in blocks}

    def words(block):
        out = []
        for rel in block.get("Relationships", []):
            if rel["Type"] == "CHILD":
                for cid in rel["Ids"]:
                    c = by_id.get(cid, {})
                    if c.get("BlockType") == "WORD":
                        out.append(c.get("Text", ""))
                    elif c.get("BlockType") == "SELECTION_ELEMENT" and c.get("SelectionStatus") == "SELECTED":
                        out.append("[X]")
        return " ".join(out)

    pairs = []
    for b in blocks:
        if b.get("BlockType") == "KEY_VALUE_SET" and "KEY" in b.get("EntityTypes", []):
            value = ""
            for rel in b.get("Relationships", []):
                if rel["Type"] == "VALUE":
                    value = " ".join(words(by_id[v]) for v in rel["Ids"] if v in by_id)
            key = words(b)
            if key and value.strip():
                pairs.append(f"{key}: {value}")
    lines = [b.get("Text", "") for b in blocks if b.get("BlockType") == "LINE"]
    parts = []
    if pairs:
        parts.append("Form fields:\n" + "\n".join(pairs))
    if lines:
        parts.append("All text:\n" + "\n".join(lines))
    return "\n\n".join(parts)


def ocr_scan(filename: str, data: bytes) -> str:
    """Text of a scanned PDF or a photo via AWS Textract, one page at a time
    (the synchronous call takes one page). Blank pages are skipped."""
    import boto3
    client = boto3.client("textract", region_name=os.getenv("AWS_REGION") or "eu-west-1")
    if file_kind(filename) == "image":
        pages = [data]
    else:
        from pypdf import PdfReader, PdfWriter
        reader = PdfReader(io.BytesIO(data))
        if len(reader.pages) > MAX_SCAN_PAGES:
            raise ValueError(f"Scans of more than {MAX_SCAN_PAGES} pages are not read")
        pages = []
        for page in reader.pages:
            writer = PdfWriter()
            writer.add_page(page)
            buf = io.BytesIO()
            writer.write(buf)
            pages.append(buf.getvalue())
    parts = []
    for i, page in enumerate(pages):
        resp = client.analyze_document(Document={"Bytes": page}, FeatureTypes=["FORMS"])
        text = textract_text(resp.get("Blocks", []))
        if text.strip():
            parts.append(f"--- Page {i + 1} ---\n{text}")
    text = "\n\n".join(parts)
    if not text.strip():
        raise ValueError("No text could be read from the scan")
    return text[:MAX_TEXT_CHARS]


# ── Picking the vehicles out with Cortex ─────────────────────────────────────

EXTRACT_PROMPT = """You read UK used-car purchase invoices and supplier sheets for CarSupermarket.com (Motor Depot Ltd), who are the BUYER.
Return ONLY a JSON object, no other text, in exactly this shape:
{
  "supplier_name": string or null,      // the company or person SELLING the cars (never CarSupermarket / Motor Depot)
  "invoice_number": string or null,
  "invoice_date": "yyyy-mm-dd" or null,
  "invoice_net": number or null,        // the invoice's own printed totals, if shown
  "invoice_vat": number or null,
  "invoice_gross": number or null,
  "vehicles": [
    {
      "vrm": string,                    // registration, no spaces
      "mileage": integer or null,
      "vin": string or null,
      "first_registered": "yyyy-mm-dd" or null,
      "description": string or null,    // make / model / derivative as written
      "colour": string or null,
      "vehicle_net": number or null,    // price of the car itself
      "vehicle_vat": number or null,
      "vehicle_gross": number or null,
      "fees": [ {"description": string, "net": number or null, "vat": number or null, "gross": number or null} ]
    }
  ],
  "notes": string or null               // anything unclear, e.g. a figure you could not read
}
Rules:
- One entry per vehicle. Buyer's fees, auction fees, assured/inspection charges, transport and delivery that belong to a vehicle go in that vehicle's "fees", never as vehicles.
- A fee charged once for the whole invoice (not tied to one car) goes in the fees of the FIRST vehicle, with "(whole invoice)" added to its description.
- Margin / non-VAT vehicles: VAT is 0 and gross = net. If only a gross price is shown with no VAT, put it in both net and gross and set VAT to 0.
- Numbers are plain numbers: no currency signs or thousands separators.
- Dates on these documents are UK format (day first).
- Handwritten or scanned forms: the seller may be a private person (e.g. the "Sold By" name); prices written in separate pounds and pence boxes ("19500 | 00") mean 19500.00.
- Never invent a value: use null if it is not on the document.
"""


def _parse_json_reply(text: str) -> dict:
    """The JSON object in a model reply, even if wrapped in ``` fences."""
    s = (text or "").strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Cortex did not return any rows")
    return json.loads(s[start:end + 1])


def extract_with_ai(text: str, filename: str) -> dict:
    from utils.cortex import cortex_complete
    data = cortex_complete(
        [{"role": "system", "content": EXTRACT_PROMPT},
         {"role": "user", "content": f"File name: {filename}\n\n{text}"}],
        max_tokens=8000, temperature=0)
    return normalise_extract(_parse_json_reply(data["choices"][0]["messages"]))


def _num(v):
    m = money(v)
    return float(m) if m is not None else None


def normalise_extract(raw: dict) -> dict:
    """Clean the model's reply into the row shape the page uses: VRMs cleaned,
    numbers as floats, fees totalled per vehicle (the list is kept too)."""
    vehicles = []
    for v in raw.get("vehicles") or []:
        vrm = clean_vrm(v.get("vrm"))
        if not vrm:
            continue
        fees = [{"description": str(f.get("description") or "").strip(),
                 "net": _num(f.get("net")), "vat": _num(f.get("vat")), "gross": _num(f.get("gross"))}
                for f in (v.get("fees") or [])]
        reg = to_date(v.get("first_registered"))
        vehicles.append({
            "vrm": vrm,
            "mileage": to_int(v.get("mileage")),
            "vin": (str(v.get("vin")).strip().upper() or None) if v.get("vin") else None,
            "first_registered": reg.isoformat() if reg else None,
            "supplier_description": (v.get("description") or None),
            "colour": v.get("colour") or None,
            "vehicle_net": _num(v.get("vehicle_net")),
            "vehicle_vat": _num(v.get("vehicle_vat")),
            "vehicle_gross": _num(v.get("vehicle_gross")),
            "fees": fees,
            "fees_net": round(sum(f["net"] or 0 for f in fees), 2),
            "fees_vat": round(sum(f["vat"] or 0 for f in fees), 2),
            "fees_gross": round(sum(f["gross"] or 0 for f in fees), 2),
        })
    date = to_date(raw.get("invoice_date"))
    return {
        "supplier_name": raw.get("supplier_name") or None,
        "invoice_number": raw.get("invoice_number") or None,
        "invoice_date": date.isoformat() if date else None,
        "invoice_net": _num(raw.get("invoice_net")),
        "invoice_vat": _num(raw.get("invoice_vat")),
        "invoice_gross": _num(raw.get("invoice_gross")),
        "notes": raw.get("notes") or None,
        "vehicles": vehicles,
    }


def totals_check(vehicles: list, invoice_net, invoice_vat, invoice_gross) -> list:
    """[{field, rows_total, invoice_total, matches}] comparing what the rows
    add up to (vehicle + fees) against the invoice's printed totals. Only
    for totals the invoice actually shows. 1p tolerance for rounding."""
    out = []
    for field, invoice in (("net", invoice_net), ("vat", invoice_vat), ("gross", invoice_gross)):
        if invoice is None:
            continue
        rows = sum((money(v.get(f"vehicle_{field}")) or Decimal("0")) + (money(v.get(f"fees_{field}")) or Decimal("0"))
                   for v in vehicles)
        out.append({"field": field, "rows_total": float(rows), "invoice_total": float(money(invoice)),
                    "matches": abs(rows - money(invoice)) <= Decimal("0.01")})
    return out


# ── Supplier Stock Evaluator data ────────────────────────────────────────────

def list_suppliers(conn) -> list:
    with conn.cursor() as cur:
        cur.execute("SELECT supplier_id, supplier_name FROM suppliers ORDER BY supplier_name")
        return [{"supplier_id": r[0], "supplier_name": r[1]} for r in cur.fetchall()]


def evaluator_lookup(conn, vrms: list, supplier_id=None) -> dict:
    """{vrm: {...}} for VRMs the evaluator has seen: the latest listing (from
    the chosen supplier if given, else any supplier), the CAP identity, and
    the latest valuation. VRMs it has never seen are left out."""
    if not vrms:
        return {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            WITH listing AS (
                SELECT DISTINCT ON (l.vrm) l.vrm, l.supplier_id, s.supplier_name, l.imported_at,
                       l.mileage, l.asking_price
                FROM supplier_vehicle_listings l
                JOIN suppliers s ON s.supplier_id = l.supplier_id
                WHERE l.vrm IN %s AND (%s IS NULL OR l.supplier_id = %s)
                ORDER BY l.vrm, l.imported_at DESC
            ),
            valuation AS (
                SELECT DISTINCT ON (vrm) vrm, fetched_at, at_retail_valuation, cap_live_clean, cap_monthly_clean
                FROM vehicle_valuations
                WHERE vrm IN %s
                ORDER BY vrm, fetched_at DESC
            )
            SELECT v.vrm, v.cap_capid, v.cap_manufacturer, v.cap_model, v.cap_derivative,
                   v.at_first_registration_date, v.at_vin,
                   l.supplier_id, l.supplier_name, l.imported_at, l.mileage, l.asking_price,
                   val.at_retail_valuation, COALESCE(val.cap_live_clean, val.cap_monthly_clean) AS cap_clean
            FROM vehicles v
            LEFT JOIN listing l ON l.vrm = v.vrm
            LEFT JOIN valuation val ON val.vrm = v.vrm
            WHERE v.vrm IN %s
        """, (tuple(vrms), supplier_id, supplier_id, tuple(vrms), tuple(vrms)))
        rows = cur.fetchall()
    out = {}
    for r in rows:
        if r["supplier_id"] is None and r["cap_capid"] is None:
            continue
        reg = r["at_first_registration_date"]
        out[r["vrm"]] = {
            "cap_id": r["cap_capid"], "make": r["cap_manufacturer"], "model": r["cap_model"],
            "derivative": r["cap_derivative"], "vin": r["at_vin"],
            "first_registered": reg.isoformat() if isinstance(reg, datetime.date) else (str(reg) if reg else None),
            "evaluator_supplier": r["supplier_name"],
            "evaluator_seen_at": r["imported_at"].isoformat(timespec="minutes") if r["imported_at"] else None,
            "evaluator_mileage": r["mileage"],
            "evaluator_list_price": float(r["asking_price"]) if r["asking_price"] is not None else None,
            "at_retail_valuation": float(r["at_retail_valuation"]) if r["at_retail_valuation"] is not None else None,
            "cap_clean": float(r["cap_clean"]) if r["cap_clean"] is not None else None,
        }
    return out


def cap_lookup(vrms_mileage: list) -> dict:
    """{vrm: {cap_id, make, model, derivative, cap_clean, note}} from the CAP
    VRM service, for vehicles the evaluator has no CAP data for."""
    from concurrent.futures import ThreadPoolExecutor
    from clients.cap_client import CAPClient
    from clients.structs import VehicleRow
    cap = CAPClient()

    def one(item):
        vrm, mileage = item
        try:
            d = cap.vrm_valuation(VehicleRow(vrm=vrm, mileage=mileage or 0))
        except Exception as e:
            return vrm, {"note": f"CAP lookup failed: {e}"}
        return vrm, {"cap_id": to_int(d.get("capid")), "make": d.get("capman"), "model": d.get("capmod"),
                     "derivative": d.get("capder"), "cap_clean": _num(d.get("clean")) if mileage else None,
                     "note": d.get("note")}

    if not vrms_mileage:
        return {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(one, vrms_mileage))


# ── Custom fields (stored in settings) ───────────────────────────────────────

def read_custom_fields(settings: dict) -> list:
    try:
        fields = json.loads(settings.get("cars_bought_custom_fields") or "null")
    except (TypeError, ValueError):
        fields = None
    return fields if isinstance(fields, list) and fields else list(DEFAULT_CUSTOM_FIELDS)


def clean_custom_fields(fields: list) -> list:
    """Validate a list sent from the page: label required, key made from the
    label when new (and never changed after, so stored values stay matched)."""
    out, keys = [], set()
    for f in fields or []:
        label = str(f.get("label") or "").strip()[:60]
        if not label:
            continue
        key = re.sub(r"[^a-z0-9]+", "_", str(f.get("key") or label).lower()).strip("_")[:40] or "field"
        while key in keys:
            key += "_2"
        keys.add(key)
        ftype = f.get("type") if f.get("type") in CUSTOM_FIELD_TYPES else "text"
        out.append({"key": key, "label": label, "type": ftype, "required": bool(f.get("required"))})
    return out


# ── Purchase database ────────────────────────────────────────────────────────

@once_per_database
def ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS purchase_batches (
                batch_id SERIAL PRIMARY KEY,
                supplier_name VARCHAR(200) NOT NULL,
                supplier_id INTEGER,
                invoice_number VARCHAR(100),
                purchase_date DATE NOT NULL,
                source VARCHAR(20) NOT NULL,
                filename VARCHAR(255),
                s3_key TEXT,
                invoice_net NUMERIC(12, 2),
                invoice_vat NUMERIC(12, 2),
                invoice_gross NUMERIC(12, 2),
                notes TEXT,
                submitted_by VARCHAR(100) NOT NULL,
                submitted_at TIMESTAMP NOT NULL
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS purchase_vehicles (
                id SERIAL PRIMARY KEY,
                batch_id INTEGER NOT NULL REFERENCES purchase_batches (batch_id) ON DELETE CASCADE,
                vrm VARCHAR(20) NOT NULL,
                mileage INTEGER,
                vin VARCHAR(30),
                first_registered DATE,
                colour VARCHAR(50),
                supplier_description TEXT,
                cap_id INTEGER,
                make VARCHAR(100),
                model VARCHAR(150),
                derivative VARCHAR(255),
                vehicle_net NUMERIC(12, 2),
                vehicle_vat NUMERIC(12, 2),
                vehicle_gross NUMERIC(12, 2),
                fees_net NUMERIC(12, 2),
                fees_vat NUMERIC(12, 2),
                fees_gross NUMERIC(12, 2),
                fees_detail JSONB,
                from_evaluator BOOLEAN NOT NULL DEFAULT FALSE,
                evaluator_list_price NUMERIC(12, 2),
                at_retail_valuation NUMERIC(12, 2),
                cap_clean NUMERIC(12, 2),
                custom JSONB
            )""")
        cur.execute("ALTER TABLE purchase_batches ADD COLUMN IF NOT EXISTS buyer VARCHAR(100)")
        cur.execute("CREATE INDEX IF NOT EXISTS purchase_vehicles_vrm ON purchase_vehicles (vrm)")
        cur.execute("CREATE INDEX IF NOT EXISTS purchase_batches_date ON purchase_batches (purchase_date)")
    conn.commit()


def already_bought(conn, vrms: list) -> dict:
    """{vrm: 'dd/mm/yyyy from Supplier'} for VRMs already in the database."""
    if not vrms:
        return {}
    ensure_tables(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (v.vrm) v.vrm, b.purchase_date, b.supplier_name
            FROM purchase_vehicles v JOIN purchase_batches b ON b.batch_id = v.batch_id
            WHERE v.vrm IN %s ORDER BY v.vrm, b.purchase_date DESC""", (tuple(vrms),))
        return {r[0]: f"{r[1]:%d/%m/%Y} from {r[2]}" for r in cur.fetchall()}


VEHICLE_COLS = ("vrm", "mileage", "vin", "first_registered", "colour", "supplier_description",
                "cap_id", "make", "model", "derivative",
                "vehicle_net", "vehicle_vat", "vehicle_gross", "fees_net", "fees_vat", "fees_gross",
                "fees_detail", "from_evaluator", "evaluator_list_price", "at_retail_valuation", "cap_clean",
                "custom")


def save_batch(conn, batch: dict, vehicles: list, user: str, now: datetime.datetime) -> int:
    ensure_tables(conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO purchase_batches (buyer, supplier_name, supplier_id, invoice_number, purchase_date, source,
                filename, s3_key, invoice_net, invoice_vat, invoice_gross, notes, submitted_by, submitted_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING batch_id""",
            (batch["buyer"], batch["supplier_name"], batch.get("supplier_id"), batch.get("invoice_number"), batch["purchase_date"],
             batch["source"], batch.get("filename"), batch.get("s3_key"), money(batch.get("invoice_net")),
             money(batch.get("invoice_vat")), money(batch.get("invoice_gross")), batch.get("notes"), user, now))
        batch_id = cur.fetchone()[0]
        rows = []
        for v in vehicles:
            rows.append((batch_id, v["vrm"], to_int(v.get("mileage")), v.get("vin"), to_date(v.get("first_registered")),
                         v.get("colour"), v.get("supplier_description"), to_int(v.get("cap_id")),
                         v.get("make"), v.get("model"), v.get("derivative"),
                         money(v.get("vehicle_net")), money(v.get("vehicle_vat")), money(v.get("vehicle_gross")),
                         money(v.get("fees_net")), money(v.get("fees_vat")), money(v.get("fees_gross")),
                         json.dumps(v.get("fees") or []), bool(v.get("from_evaluator")),
                         money(v.get("evaluator_list_price")), money(v.get("at_retail_valuation")),
                         money(v.get("cap_clean")), json.dumps(v.get("custom") or {})))
        psycopg2.extras.execute_values(
            cur, f"INSERT INTO purchase_vehicles (batch_id, {', '.join(VEHICLE_COLS)}) VALUES %s", rows)
    conn.commit()
    return batch_id


def list_batches(conn, limit: int = 200) -> list:
    ensure_tables(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT b.batch_id, b.supplier_name, b.invoice_number, b.purchase_date, b.source, b.filename,
                   b.invoice_gross, b.submitted_by, b.submitted_at, COUNT(v.id),
                   COALESCE(SUM(COALESCE(v.vehicle_gross, 0) + COALESCE(v.fees_gross, 0)), 0), b.buyer
            FROM purchase_batches b LEFT JOIN purchase_vehicles v ON v.batch_id = b.batch_id
            GROUP BY b.batch_id ORDER BY b.purchase_date DESC, b.batch_id DESC LIMIT %s""", (limit,))
        rows = cur.fetchall()
    conn.commit()
    return [{"batch_id": r[0], "supplier_name": r[1], "invoice_number": r[2], "purchase_date": r[3].isoformat(),
             "source": r[4], "filename": r[5], "invoice_gross": float(r[6]) if r[6] is not None else None,
             "submitted_by": r[7], "submitted_at": r[8].isoformat(timespec="minutes"), "vehicles": r[9],
             "rows_gross": float(r[10]), "buyer": r[11]} for r in rows]


def read_vehicles(conn, batch_id=None, date_from=None, date_to=None) -> list:
    """Vehicle rows with their batch's details, for one batch or a date range."""
    ensure_tables(conn)
    where, params = [], []
    if batch_id is not None:
        where.append("b.batch_id = %s")
        params.append(batch_id)
    if date_from:
        where.append("b.purchase_date >= %s")
        params.append(date_from)
    if date_to:
        where.append("b.purchase_date <= %s")
        params.append(date_to)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"""
            SELECT b.batch_id, b.buyer, b.supplier_name, b.invoice_number, b.purchase_date, b.submitted_by, b.submitted_at,
                   {', '.join('v.' + c for c in VEHICLE_COLS)}
            FROM purchase_vehicles v JOIN purchase_batches b ON b.batch_id = v.batch_id
            {('WHERE ' + ' AND '.join(where)) if where else ''}
            ORDER BY b.purchase_date DESC, b.batch_id DESC, v.id""", params)
        rows = [dict(r) for r in cur.fetchall()]
    conn.commit()
    return rows


def delete_batch(conn, batch_id: int) -> bool:
    ensure_tables(conn)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM purchase_batches WHERE batch_id = %s", (batch_id,))
        deleted = cur.rowcount > 0
    conn.commit()
    return deleted


def csv_rows(rows: list, custom_fields: list) -> tuple:
    """(header, rows) for the CSV extract: one line per vehicle."""
    header = ["Batch", "Buyer", "Supplier", "Invoice Number", "Purchase Date", "VRM", "Mileage", "VIN", "First Registered",
              "Colour", "CAP ID", "Make", "Model", "Derivative", "Supplier Description",
              "Vehicle Net", "Vehicle VAT", "Vehicle Gross", "Fees Net", "Fees VAT", "Fees Gross",
              "Total Net", "Total VAT", "Total Gross", "Fees Detail",
              "From Evaluator", "Evaluator List Price", "AT Retail Valuation", "CAP Clean"] + \
             [f["label"] for f in custom_fields] + ["Submitted By", "Submitted At"]
    out = []
    for r in rows:
        def n(k):
            return float(r[k]) if r.get(k) is not None else ""

        def tot(part):
            if r.get(f"vehicle_{part}") is None and r.get(f"fees_{part}") is None:
                return ""
            return round(float(r.get(f"vehicle_{part}") or 0) + float(r.get(f"fees_{part}") or 0), 2)
        fees = r.get("fees_detail") or []
        custom = r.get("custom") or {}
        out.append([r["batch_id"], r["buyer"] or "", r["supplier_name"], r["invoice_number"] or "", r["purchase_date"].strftime("%d/%m/%Y"),
                    r["vrm"], r["mileage"] if r["mileage"] is not None else "", r["vin"] or "",
                    r["first_registered"].strftime("%d/%m/%Y") if r["first_registered"] else "",
                    r["colour"] or "", r["cap_id"] or "", r["make"] or "", r["model"] or "", r["derivative"] or "",
                    r["supplier_description"] or "",
                    n("vehicle_net"), n("vehicle_vat"), n("vehicle_gross"), n("fees_net"), n("fees_vat"), n("fees_gross"),
                    tot("net"), tot("vat"), tot("gross"),
                    "; ".join(f"{f.get('description')}: {f.get('gross') if f.get('gross') is not None else f.get('net')}" for f in fees),
                    "Yes" if r["from_evaluator"] else "No", n("evaluator_list_price"), n("at_retail_valuation"), n("cap_clean")] +
                   [custom.get(f["key"], "") for f in custom_fields] +
                   [r["submitted_by"], r["submitted_at"].strftime("%d/%m/%Y %H:%M")])
    return header, out
