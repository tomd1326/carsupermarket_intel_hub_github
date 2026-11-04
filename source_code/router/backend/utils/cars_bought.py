"""Cars Bought: supplier invoices read into vehicle purchase records.

Two ways in:
  1. Upload an invoice or sheet. No AI model is used (the website must not
     use Snowflake Cortex). PDFs, scans and photos go to AWS Textract
     AnalyzeExpense, which is built for invoices: it returns the line items
     and the invoice number, date and totals (the Lambda role needs
     textract:AnalyzeExpense). CSV/XLSX are read by matching their column
     headings. Both use the column names set on the page (Cars Bought
     Settings), so a new supplier layout is a settings change, not code.
  2. Pick a supplier the Supplier Stock Evaluator already holds (Enterprise,
     Motability, Purchase Pro ...) and paste VRMs. Mileage, list price and the
     CAP details come from the evaluator's own tables.
  3. Invoices saved to a SharePoint folder, read the same way as an upload and
     left pending for a buyer (utils/cars_bought_inbox.py).

The buyer checks the rows, fills in the custom fields, and submits. One
submit is one purchase_batches row with a purchase_vehicles row per car.
"""

import datetime
import email
import email.policy
import email.utils
import io
import json
import logging
import os
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html.parser import HTMLParser

import psycopg2.extras

from utils.ddl_once import once_per_database
from utils.vehicle_store import BUYER_FIGURES, round_mileage_to_nearest_thousand

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 4 * 1024 * 1024     # base64 adds a third; Lambda takes 6 MB
MAX_SCAN_PAGES = 10                    # Textract is charged per page

# Column headings (on an invoice or sheet) for each field, matched ignoring
# case and punctuation. Editable on the page; these are the starting lists,
# from the BCA, OPENLANE, SMC, ERAC and Motor Depot examples.
DEFAULT_COLUMN_NAMES = {
    "vrm": ["reg", "reg no", "reg number", "registration", "registration number", "vrm", "license", "licence",
            "registration no", "vehicle reg no"],
    "mileage": ["mileage", "miles", "odometer"],
    "vin": ["vin", "chassis", "chassis no", "chassis id", "chassis number"],
    "first_registered": ["dor", "date of reg", "reg date", "first reg", "1st reg", "date of first registration",
                         "date of first reg"],
    "description": ["description", "item description", "details", "vehicle", "version", "make model"],
    "vehicle_net": ["net", "net amount", "price excl vat", "price ex vat", "price exc vat", "price"],
    "vehicle_vat": ["vat", "vat amount", "vat standard rate", "total vat amount"],
    "vehicle_gross": ["gross", "total", "inclusive vat", "price inc vat", "purchase price", "gross amount"],
    "fee_net": ["transport", "service fee", "buyers fee", "buyer fee"],
    # Labels of the invoice's own totals (a totals row, or "Invoice Total 16,500.00").
    # Never used to match a table column.
    "invoice_net": ["subtotal", "total net amount", "total gbp excl vat", "total excl vat", "net total", "total net"],
    "invoice_vat": ["total vat amount", "total vat", "vat total"],
    "invoice_gross": ["invoice total", "total gbp", "total due", "grand total", "gross total", "total"],
    # Labels on a form with no table ("Vehicle price: 16,650.00"). The label must
    # be the whole label, and these are never used to match a table column.
    "form_vrm": ["vehicle registration", "registration", "vehicle reg"],
    "form_description": ["make", "model", "variant", "derivative"],
    "form_price": ["vehicle price", "car price", "sale price", "hammer price"],
    "form_vat": ["vehicle vat", "car vat"],
}
COLUMN_FIELD_LABELS = {
    "vrm": "VRM", "mileage": "Mileage", "vin": "VIN", "first_registered": "First Registered",
    "description": "Description", "vehicle_net": "Vehicle Net", "vehicle_vat": "Vehicle VAT",
    "vehicle_gross": "Vehicle Gross", "fee_net": "Fee Columns (Net)",
    "invoice_net": "Invoice Total Net", "invoice_vat": "Invoice Total VAT", "invoice_gross": "Invoice Total Gross",
    "form_vrm": "Form Label: VRM", "form_description": "Form Label: Vehicle Text",
    "form_price": "Form Label: Car Price", "form_vat": "Form Label: Car VAT",
}
_TOTAL_FIELDS = ("invoice_net", "invoice_vat", "invoice_gross")
_FORM_FIELDS = ("form_vrm", "form_description", "form_price", "form_vat")
_AMOUNT_FIELDS = ("vehicle_net", "vehicle_vat", "vehicle_gross", "fee_net")
# VAT % charged on a fee printed in its own column on a car's row (ERAC
# Service Fee). The row's VAT and total include the fee, so the fee's VAT is
# this % of the fee and the car's VAT is what is left. Editable on the page.
DEFAULT_FEE_VAT_RATE = 20.0
# A line whose description has one of these words is a fee, not a car.
DEFAULT_FEE_WORDS = ["fee", "fees", "charge", "indicator", "transport", "delivery"]
# Text found on a supplier's invoices -> the supplier name to use. The first
# that appears on the invoice wins. Editable on the page.
DEFAULT_SUPPLIER_NAMES = [
    {"text": "ERAC UK", "supplier": "Enterprise"},
    {"text": "Enterprise Rent-A-Car", "supplier": "Enterprise"},
    {"text": "OPENLANE", "supplier": "OPENLANE"},
    {"text": "British Car Auctions", "supplier": "BCA"},
    {"text": "Southern Motor Contracts", "supplier": "Southern Motor Contracts"},
    {"text": "The Fleet Auction Group", "supplier": "The Fleet Auction Group"},
]
# Our own names on an invoice (the customer's address): never suggested as a supplier.
DEFAULT_OWN_NAMES = ["Motor Depot", "CarSupermarket", "Car Supermarket", "Best Car Buyer"]
# How one supplier's invoices are read. priced_lines_are_fees: a line with
# amounts and no VRM under a car is a fee, whatever it says (BCA cuts its
# descriptions short: "BCA EV/Hybrid Assured Cha"). Editable on the page.
DEFAULT_SUPPLIER_RULES = [{"supplier": "BCA", "fee_words": [], "priced_lines_are_fees": True}]

# Fields the buyer fills in. Editable on the page (stored in settings as
# JSON), these are only what a new install starts with.
DEFAULT_CUSTOM_FIELDS = [
    {"key": "rrp", "label": "RRP", "type": "money"},
    {"key": "est_prep_cost", "label": "Estimated Prep Cost", "type": "money"},
]
CUSTOM_FIELD_TYPES = ("money", "number", "text", "yesno")
# The buying sheet's columns that no invoice holds, for buyers to fill in. Added
# once to whatever fields are already set up; after that they are edited on the page.
SHEET_CUSTOM_FIELDS = [
    {"key": "vendor", "label": "Vendor", "type": "text", "required": False},
    {"key": "notes", "label": "Notes", "type": "text", "required": False},
    {"key": "sat_nav", "label": "Sat Nav", "type": "yesno", "required": False},
    {"key": "spec", "label": "Spec", "type": "text", "required": False},
    {"key": "replaceable", "label": "Replaceable", "type": "yesno", "required": False},
    {"key": "rrp", "label": "RRP", "type": "money", "required": False},
]

# The All Vehicles Bought table. Columns are named in capitals (the shared column
# editor searches on capitals); each is the row's field of the same name in lower
# case, and a custom field is CUSTOM:<KEY>. (column, heading, format)
TABLE_FIELDS = [
    ("PURCHASE_DATE", "Date", "date"), ("BUYER", "Buyers", "text"), ("SUPPLIER_NAME", "Supplier", "text"),
    ("FIRST_REGISTERED", "DOR", "date"), ("VRM", "Reg", "text"), ("MAKE", "Make", "text"), ("MODEL", "Model", "text"),
    ("DERIVATIVE", "Variant", "text"), ("CAP_ID", "CAP ID", "text"), ("COLOUR", "Colour", "text"),
    ("MILEAGE", "Mileage", "number"), ("VIN", "VIN", "text"), ("INVOICE_NUMBER", "Invoice", "text"),
    ("SUPPLIER_DESCRIPTION", "Supplier Description", "text"), ("VAT_STATUS", "VAT Status", "text"),
    ("VEHICLE_NET", "Vehicle Net", "gbp"), ("VEHICLE_VAT", "Vehicle VAT", "gbp"), ("VEHICLE_GROSS", "Vehicle Gross", "gbp"),
    ("FEES_NET", "Fees Net", "gbp"), ("FEES_VAT", "Fees VAT", "gbp"), ("FEES_GROSS", "Fees Gross", "gbp"),
    ("PURCHASE_TOTAL", "Purchase Price", "gbp"), ("AT_RETAIL_VALUATION", "AT Valuation", "gbp"),
    ("CAP_CLEAN", "CAP Clean", "gbp"), ("SUBMITTED_BY", "Submitted By", "text"), ("SUBMITTED_AT", "Submitted", "date"),
]
TABLE_FORMATS = ("text", "number", "gbp", "pct", "date")
# The order of the buying sheet.
DEFAULT_TABLE_ORDER = ["PURCHASE_DATE", "BUYER", "SUPPLIER_NAME", "CUSTOM:VENDOR", "FIRST_REGISTERED", "VRM", "MAKE", "MODEL",
                       "DERIVATIVE", "CAP_ID", "CUSTOM:NOTES", "COLOUR", "MILEAGE", "CUSTOM:SAT_NAV", "CUSTOM:SPEC",
                       "CUSTOM:REPLACEABLE", "PURCHASE_TOTAL", "CUSTOM:RRP", "CUSTOM:EST_PREP_COST"]
DEFAULT_TABLE_ROWS = 500

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
    """A money value as a 2dp Decimal, or None. Accepts '1,234.50', '£9,350',
    and '19500 00' (pounds and pence in separate boxes on a paper form)."""
    if value is None or value == "":
        return None
    s = str(value).replace(",", "").replace("£", "").strip()
    s = re.sub(r"^(\d+) (\d{2})$", r"\1.\2", s)
    try:
        return Decimal(s).quantize(Decimal("0.01"))
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
    if ext in (".html", ".htm"):
        return "html"
    if ext in (".msg", ".eml"):
        return "email"
    raise ValueError("Only CSV, XLSX, PDF, JPG, PNG, HTML and saved emails (MSG, EML) can be read")


def file_kind_or_none(filename: str) -> str | None:
    try:
        return file_kind(filename)
    except ValueError:
        return None


class _HtmlText(HTMLParser):
    """An email's text: each table row or block on its own line, cells joined by
    a space so a label in one cell stays with its value in the next."""
    _BLOCKS = {"tr", "br", "p", "div", "li", "table", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self.skip += 1
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"):
            self.skip = max(0, self.skip - 1)
        elif tag in ("td", "th"):
            self.parts.append(" ")
        elif tag in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(" ".join(data.split()))


def html_text(data: bytes) -> str:
    parser = _HtmlText()
    parser.feed(data.decode("utf-8-sig", errors="replace"))
    return "\n".join(l.strip() for l in "".join(parser.parts).replace("\xa0", " ").splitlines() if l.strip())


def _msg_parts(data: bytes) -> tuple:
    """(html bytes, plain text, received date) of an Outlook .msg file."""
    import olefile
    try:
        ole = olefile.OleFileIO(io.BytesIO(data))
    except OSError as e:
        raise ValueError("This email file could not be opened") from e

    def stream(name):
        return ole.openstream(name).read() if ole.exists(name) else None

    with ole:
        html = stream("__substg1.0_10130102") or stream("__substg1.0_1013001F")
        unicode_text, ansi_text = stream("__substg1.0_1000001F"), stream("__substg1.0_1000001E")
        properties = stream("__properties_version1.0") or b""
    plain = unicode_text.decode("utf-16-le", errors="replace") if unicode_text else (ansi_text or b"").decode("cp1252", errors="replace")
    received = None
    # After a 32-byte header, each entry is 16 bytes: tag, flags, value.
    for i in range(32, len(properties) - 15, 16):
        if properties[i:i + 4] == b"\x40\x00\x06\x0e":     # PR_MESSAGE_DELIVERY_TIME, a Windows file time
            ticks = int.from_bytes(properties[i + 8:i + 16], "little")
            received = datetime.datetime(1601, 1, 1) + datetime.timedelta(microseconds=ticks // 10)
    return html, plain, received


def _eml_parts(data: bytes) -> tuple:
    """(html bytes, plain text, received date) of a saved .eml file."""
    message = email.message_from_bytes(data, policy=email.policy.default)
    html, plain = message.get_body(("html",)), message.get_body(("plain",))
    try:
        received = email.utils.parsedate_to_datetime(message["date"])
    except (TypeError, ValueError):
        received = None
    return html.get_content().encode("utf-8") if html else None, plain.get_content() if plain else "", received


def email_texts(filename: str, data: bytes) -> list:
    """A saved email's body as text, HTML version first, to try in turn. The date
    received goes last, so a date printed in the email itself is used first."""
    html, plain, received = (_msg_parts if filename.lower().endswith(".msg") else _eml_parts)(data)
    after = f"\nDate: {received:%d/%m/%Y}" if received else ""
    return [text + after for text in (html_text(html) if html else "", plain) if text.strip()]


# ── Matching column headings ─────────────────────────────────────────────────

_VRM = re.compile(r"\b([A-Z]{2}[0-9]{2} ?[A-Z]{3}|[A-Z][0-9]{1,3} ?[A-Z]{3}|[A-Z]{3} ?[0-9]{1,3}[A-Z])\b")
_VRM_FULL = re.compile(r"[A-Z]{2}[0-9]{2}[A-Z]{3}|[A-Z][0-9]{1,3}[A-Z]{3}|[A-Z]{3}[0-9]{1,3}[A-Z]")
_VIN = re.compile(r"\b([A-HJ-NPR-Z0-9]{17})\b")
_MILEAGE_IN_TEXT = re.compile(r"(?:mileage|odometer)\W*([\d,]{3,7})", re.I)
_REG_DATE_IN_TEXT = re.compile(r"(?:reg\.? ?date|registration date|1st reg|date of reg|dfr)\W*(\d{1,2}/\d{1,2}/\d{2,4})", re.I)


def norm_label(text) -> str:
    """'Price Excl. VAT' -> 'price excl vat'. % is kept, so 'VAT %' never
    matches 'VAT'."""
    return " ".join(re.sub(r"[^a-z0-9%]+", " ", str(text or "").lower()).split())


def read_column_names(saved) -> dict:
    names = {k: list(v) for k, v in DEFAULT_COLUMN_NAMES.items()}
    if isinstance(saved, dict):
        for k, v in saved.items():
            if k in names and isinstance(v, list):
                names[k] = [str(x) for x in v if str(x).strip()]
    return names


def match_field(label, names: dict):
    """The field a column heading belongs to, or None. An exact match wins;
    otherwise the longest name found as whole words inside the heading."""
    lab = norm_label(label)
    if not lab:
        return None
    names = {k: v for k, v in names.items() if k not in _TOTAL_FIELDS and k not in _FORM_FIELDS}
    for field, aliases in names.items():
        if lab in {norm_label(a) for a in aliases}:
            return field
    # 'VAT %' and 'VAT Rate' hold a percentage, not an amount.
    if "%" in lab or re.search(r"(^| )rate( |$)", lab):
        return None
    best, best_len = None, 0
    for field, aliases in names.items():
        for a in aliases:
            a = norm_label(a)
            if a and len(a) > best_len and re.search(rf"(^| ){re.escape(a)}( |$)", lab):
                best, best_len = field, len(a)
    return best


def find_vrm(text) -> str | None:
    m = _VRM.search(str(text or "").upper())
    return clean_vrm(m.group(1)) if m else None


def is_fee(text, fee_words: list) -> bool:
    low = norm_label(text)
    return any(re.search(rf"(^| ){re.escape(norm_label(w))}( |$)", low) for w in fee_words if w.strip())


def clean_supplier_rules(rules) -> list:
    out = []
    for r in rules or []:
        supplier = str((r or {}).get("supplier") or "").strip()[:100]
        if supplier:
            out.append({"supplier": supplier,
                        "fee_words": [str(w).strip()[:30] for w in (r.get("fee_words") or []) if str(w).strip()],
                        "priced_lines_are_fees": bool(r.get("priced_lines_are_fees"))})
    return out


def apply_supplier_rule(supplier, fee_words: list, rules) -> tuple:
    """(fee words, priced_lines_are_fees) for this supplier's invoice."""
    name = str(supplier or "").strip().lower()
    for r in rules or []:
        if name and str(r.get("supplier") or "").strip().lower() == name:
            return list(fee_words) + list(r.get("fee_words") or []), bool(r.get("priced_lines_are_fees"))
    return fee_words, False


def _fill_from_text(v: dict, text: str) -> None:
    """Mileage, VIN and registration date written inside a description."""
    if v.get("mileage") is None:
        m = _MILEAGE_IN_TEXT.search(text)
        if m:
            v["mileage"] = m.group(1)
    if not v.get("vin"):
        m = _VIN.search(text.upper())
        if m:
            v["vin"] = m.group(1)
    if not v.get("first_registered"):
        m = _REG_DATE_IN_TEXT.search(text)
        if m:
            v["first_registered"] = m.group(1)


def _assemble(lines: list, fee_words: list, priced_lines_are_fees: bool = False, names: dict = None) -> list:
    """Lines [{fields..., text}] in invoice order -> vehicles with their fees.
    A line with a fee word is a fee: it goes on the car whose VRM it names,
    else the car before it, else the first car. Two lines for the same VRM
    (e.g. a details line and a price line) are merged, but a second priced line
    for a car is a fee on it (Fleet Auction: "Buyer's Premium (WV75SZD)"). With
    priced_lines_are_fees, a priced line with no VRM under a car is a fee too,
    unless it is one of the invoice's totals."""
    def priced(row):
        return any(row.get(k) not in (None, "") for k in ("vehicle_net", "vehicle_vat", "vehicle_gross"))

    vehicles, order, fees = {}, [], []
    for line in lines:
        text = line.get("text") or ""
        vrm = clean_vrm(line.get("vrm")) if line.get("vrm") else None
        if vrm and not _VRM_FULL.fullmatch(vrm):
            vrm = None
        if is_fee(line.get("description") or text, fee_words):
            fees.append({"vrm": vrm or find_vrm(text), "after": order[-1] if order else None, "line": line})
            continue
        vrm = vrm or find_vrm(text)
        if not vrm:
            if priced_lines_are_fees and order and not _total_field(text, names or {}) and priced(line):
                fees.append({"vrm": None, "after": order[-1], "line": line})
            continue
        v = vehicles.get(vrm)
        if v is not None and priced(v) and priced(line):
            fees.append({"vrm": vrm, "after": vrm, "line": line})
            continue
        if v is None:
            v = vehicles[vrm] = {"vrm": vrm, "fees": []}
            order.append(vrm)
        for k in ("mileage", "vin", "first_registered", "description", "vehicle_net", "vehicle_vat", "vehicle_gross"):
            if v.get(k) in (None, "") and line.get(k) not in (None, ""):
                v[k] = line[k]
        v["fees"].extend(line.get("fee_columns") or [])
        _fill_from_text(v, text)
    if not order:
        return []
    for f in fees:
        target = f["vrm"] if f["vrm"] in vehicles else (f["after"] or order[0])
        line = f["line"]
        net, vat, gross = line.get("vehicle_net"), line.get("vehicle_vat"), line.get("vehicle_gross")
        vehicles[target]["fees"].append({"description": (line.get("description") or line.get("text") or "Fee").strip(),
                                         "net": net, "vat": vat, "gross": gross})
    return [vehicles[k] for k in order]


# ── CSV / XLSX ───────────────────────────────────────────────────────────────

def parse_sheet(filename: str, data: bytes, names: dict, fee_words: list) -> dict:
    """Rows of a sheet. The heading row is the first (within 30 rows) that has
    a VRM column and at least one other known column."""
    if file_kind(filename) == "csv":
        # csv rather than pandas: title rows above the headings are often a
        # different width, which pandas refuses.
        import csv
        text = data.decode("utf-8-sig", errors="replace")
        all_sheets = [list(csv.reader(io.StringIO(text)))]
    else:
        import pandas as pd
        all_sheets = [df.fillna("").astype(str).values.tolist()
                      for df in pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=str).values()]
    lines = []
    for rows in all_sheets:
        for h, row in enumerate(rows[:30]):
            cols = {i: match_field(c, names) for i, c in enumerate(row)}
            found = [f for f in cols.values() if f]
            if "vrm" in found and len(found) >= 2:
                break
        else:
            continue
        for row in rows[h + 1:]:
            line = {"text": " ".join(c for c in row if c.strip()), "fee_columns": []}
            for i, field in cols.items():
                value = row[i].strip() if i < len(row) else ""
                if not field or not value:
                    continue
                if field == "fee_net":
                    if money(value):
                        line["fee_columns"].append({"description": row_heading(rows[h], i), "net": value, "in_row": True})
                elif field not in line:
                    line[field] = value
            if line.get("vrm") or line["text"]:
                lines.append(line)
    if not lines:
        raise ValueError("No heading row with a VRM column was found. Add the sheet's headings in Cars Bought Settings")
    return {"vehicles": _assemble(lines, fee_words), "found_text": found_text([l.get("text") or "" for l in lines])}


def row_heading(row: list, i: int) -> str:
    return str(row[i]).strip() if i < len(row) else "Fee"


# ── PDF / scan / photo: Textract AnalyzeExpense ──────────────────────────────

def _pages(filename: str, data: bytes) -> list:
    """One document per page: the synchronous Textract call takes one page."""
    if file_kind(filename) == "image":
        return [data]
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(io.BytesIO(data))
    if len(reader.pages) > MAX_SCAN_PAGES:
        raise ValueError(f"PDFs of more than {MAX_SCAN_PAGES} pages are not read")
    pages = []
    for page in reader.pages:
        writer = PdfWriter()
        writer.add_page(page)
        buf = io.BytesIO()
        writer.write(buf)
        pages.append(buf.getvalue())
    return pages


def analyze_expense(filename: str, data: bytes) -> list:
    import boto3
    client = boto3.client("textract", region_name=os.getenv("AWS_REGION") or "eu-west-1")
    return [client.analyze_expense(Document={"Bytes": p}) for p in _pages(filename, data)]


def _field(f: dict) -> tuple:
    """(type, label, value) of a Textract expense field."""
    return ((f.get("Type") or {}).get("Text") or "",
            (f.get("LabelDetection") or {}).get("Text") or "",
            ((f.get("ValueDetection") or {}).get("Text") or "").strip())


def parse_expense(responses: list, names: dict, fee_words: list, supplier_names: list = None,
                  supplier_rules: list = None) -> dict:
    """Vehicles and invoice details from Textract AnalyzeExpense replies."""
    summary, labelled, lines, all_text = {}, {}, [], []
    for resp in responses:
        for doc in resp.get("ExpenseDocuments", []):
            for f in doc.get("SummaryFields", []):
                ftype, label, value = _field(f)
                if not value:
                    continue
                all_text.append(f"{label} {value}")
                if ftype in ("TOTAL", "AMOUNT_DUE", "SUBTOTAL", "TAX"):
                    summary[ftype] = value                 # last page wins: totals sit at the end
                else:
                    summary.setdefault(ftype, value)
                field = match_field(label, names)
                if field and field not in labelled:
                    labelled[field] = value
            for group in doc.get("LineItemGroups", []):
                for item in group.get("LineItems", []):
                    line, values, row_text = {"fee_columns": []}, [], ""
                    for f in item.get("LineItemExpenseFields", []):
                        ftype, label, value = _field(f)
                        if not value:
                            continue
                        if ftype == "EXPENSE_ROW":
                            row_text = value
                            continue
                        values.append(value)
                        field = match_field(label, names) if label else None
                        if field == "fee_net":
                            if money(value):
                                line["fee_columns"].append({"description": label, "net": value, "in_row": True})
                        elif field and field not in line:
                            line[field] = value
                        elif ftype == "ITEM" and "description" not in line:
                            line["description"] = value
                        elif ftype == "PRICE" and not field and "vehicle_gross" not in line:
                            line["vehicle_gross"] = value
                    line["text"] = row_text or " ".join(values)
                    all_text.append(line["text"])
                    lines.append(line)
            for b in doc.get("Blocks", []) or []:
                if b.get("BlockType") == "LINE":
                    all_text.append(b.get("Text", ""))

    matched, supplier_text = match_supplier("\n".join(all_text), supplier_names)
    supplier = matched or summary.get("VENDOR_NAME")
    fee_words, priced_lines_are_fees = apply_supplier_rule(supplier, fee_words, supplier_rules)
    vehicles = _assemble(lines, fee_words, priced_lines_are_fees, names)
    if not vehicles:
        # A form with no table (e.g. a handwritten purchase invoice): one car
        # from the labelled fields.
        vrm = clean_vrm(labelled.get("vrm")) if labelled.get("vrm") else None
        if not vrm or not _VRM_FULL.fullmatch(vrm):
            vrm = next((x for x in (find_vrm(t) for t in all_text) if x), None)
        if vrm:
            v = {"vrm": vrm, "fees": [], **{k: labelled[k] for k in ("mileage", "vin", "first_registered", "description",
                                                                     "vehicle_net", "vehicle_vat", "vehicle_gross")
                                            if k in labelled}}
            v.setdefault("vehicle_gross", summary.get("TOTAL") or summary.get("AMOUNT_DUE"))
            _fill_from_text(v, " ".join(all_text))
            vehicles = [v]
    return {
        "supplier_name": supplier,
        "supplier_text": supplier_text,
        "supplier_matched": bool(matched),
        "found_text": found_text(all_text),
        "invoice_number": summary.get("INVOICE_RECEIPT_ID"),
        "invoice_date": summary.get("INVOICE_RECEIPT_DATE"),
        "invoice_net": summary.get("SUBTOTAL"),
        "invoice_vat": summary.get("TAX"),
        "invoice_gross": summary.get("TOTAL") or summary.get("AMOUNT_DUE"),
        "vehicles": vehicles,
    }


# ── PDF with a text layer: read the layout text, no Textract ─────────────────
# pypdf's layout mode keeps each printed row on one line, in column order.
# Character positions do NOT line up between the heading and the rows when
# fonts differ (BCA), so amounts are matched to headings by their order.

_MONEY_TOKEN = re.compile(r"(?<![\d,.])-?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}(?!\d)")
_DATE_TOKEN = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_MILEAGE_TOKEN = re.compile(r"^(?:\d{1,3}(?:,\d{3})+|\d{3,6})$")
_INVOICE_NO = re.compile(r"invoice\s*(?:no\.?|number|#)?\s*:?\s*([A-Z0-9/\-]*\d[A-Z0-9/\-]*)", re.I)
# Not a car's "Registration Date" or "Reg. Date".
_INVOICE_DATE = re.compile(r"(?<!registration )(?<!reg )(?<!reg\. )(?:invoice date|tax point date|tax point|document date|date)\s*:?\s*(\d{1,2}/\d{1,2}/\d{2,4})", re.I)
_FORM_VRM = re.compile(r"(?:reg(?:istration)?\.?\s*(?:no|number)\.?|vrm|licen[cs]e)\s*:?\s*([A-Z0-9]{2,4} ?[A-Z0-9]{3})\b", re.I)
_FORM_VEHICLE = re.compile(r"^\s*vehicle\s*:\s*(.+)$", re.I | re.M)


def pdf_layout_pages(data: bytes) -> list:
    from pypdf import PdfReader
    # Layout mode finds nothing when the page's text sits in an embedded form
    # (MAG), though plain mode reads it. Not a scan, so it must not go to Textract.
    pages = []
    for page in PdfReader(io.BytesIO(data)).pages:
        layout = page.extract_text(extraction_mode="layout") or ""
        pages.append(layout if layout.strip() else page.extract_text() or "")
    return pages


def _cells(line: str) -> list:
    """[start, end, text] for each run of text separated by 2+ spaces."""
    return [[m.start(), m.end(), m.group()] for m in re.finditer(r"\S+(?: \S+)*", line)]


def _header(lines: list, i: int, names: dict):
    """The table heading starting at line i, or None. A heading has no digits,
    at least two known columns and at least one amount column. A heading
    printed over two lines (ERAC: 'PRICE EXCL.' above 'VAT') is joined by
    putting each second-line word under the first-line heading it sits beneath."""
    line = lines[i]
    if not line.strip() or re.search(r"\d", line):
        return None
    cells = _cells(line)
    nxt = lines[i + 1] if i + 1 < len(lines) else ""
    if nxt.strip() and not re.search(r"\d", nxt):
        joined = [c[:] for c in cells]
        for s, e, t in _cells(nxt):
            for c in joined:
                if s <= c[1] + 1 and c[0] <= e + 1:
                    c[2] += " " + t
                    break
        if sum(1 for c in joined if match_field(c[2], names)) >= sum(1 for c in cells if match_field(c[2], names)):
            cells = joined
    cols = [{"label": c[2], "field": match_field(c[2], names),
             "pct": "%" in c[2] or bool(re.search(r"\brate\b", c[2], re.I)) and not match_field(c[2], names)}
            for c in cells]
    # Fleet Auction: Unit Price, Total, VAT, Total Inc. The last total is the
    # gross and the total before it the net; a price before that is not used.
    for field, before in (("vehicle_gross", "vehicle_net"), ("vehicle_net", None)):
        same = [c for c in cols if c["field"] == field]
        for c in same[:-1]:
            c["field"] = before
    fields = [c["field"] for c in cols if c["field"]]
    if len(fields) < 2 or not any(f in _AMOUNT_FIELDS for f in fields):
        return None
    return cols


def _map_amounts(tokens: list, cols: list) -> list:
    """[(column, amount)] for a row's amounts. Matched by order: to every
    amount-like column (percent columns included) when the counts agree,
    else to the amount columns alone. With extra amounts, the last ones are
    used. With too few, a column in the middle was left blank (OPENLANE's car
    has no VAT: Net 9,000.00, Gross 9,000.00), so the last amount goes to the
    last column and the others to the first columns."""
    with_pct = [c for c in cols if c["field"] in _AMOUNT_FIELDS or c["pct"]]
    amounts = [c for c in cols if c["field"] in _AMOUNT_FIELDS]
    if len(tokens) == len(with_pct):
        return list(zip(with_pct, tokens))
    if len(tokens) == len(amounts):
        return list(zip(amounts, tokens))
    if not tokens or not amounts:
        return []
    if len(tokens) > len(amounts):
        return list(zip(amounts, tokens[len(tokens) - len(amounts):]))
    return list(zip(amounts, tokens[:-1])) + [(amounts[-1], tokens[-1])]


def _total_field(label: str, names: dict):
    lab = norm_label(label)
    for f in _TOTAL_FIELDS:
        if lab in {norm_label(a) for a in names.get(f, [])}:
            return f
    best, best_len = None, 0
    for f in _TOTAL_FIELDS:
        for a in names.get(f, []):
            a = norm_label(a)
            if a and len(a) > best_len and re.search(rf"(^| ){re.escape(a)}( |$)", lab):
                best, best_len = f, len(a)
    return best


def match_supplier(text: str, supplier_names: list) -> tuple:
    """(supplier, the text that matched), or (None, None). The longest match
    wins, so the order of the list does not matter and a short text cannot
    take an invoice from a fuller one."""
    low = (text or "").lower()
    found = [s for s in supplier_names or [] if s.get("text") and s["text"].lower() in low]
    if not found:
        return None, None
    best = max(found, key=lambda s: len(s["text"]))
    return best.get("supplier"), best["text"]


def detect_supplier(text: str, supplier_names: list):
    return match_supplier(text, supplier_names)[0]


MIN_SUPPLIER_TEXT = 3
_COMPANY_WORD = re.compile(r"\b(ltd|limited|plc|llp|auctions?|motors?|cars?|group|trade|leasing|remarketing|rentals?)\b", re.I)
_NOT_A_NAME = re.compile(r"@|https?:|www\.|\d{4,}|^\d|^(registered|vat|company|invoice|account|sort code|iban|bank|tel|fax|phone|page|date|customer)\b", re.I)


def clean_supplier_names(rows) -> list:
    """The Supplier Names list: one row per text, the later row winning. Raises
    ValueError for a text so short it would match almost every invoice."""
    out = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        text, supplier = str(r.get("text") or "").strip()[:100], str(r.get("supplier") or "").strip()[:100]
        if not text and not supplier:
            continue
        if not text or not supplier:
            raise ValueError(f"Supplier Names: '{text or supplier}' needs both a text and a supplier")
        if len(text) < MIN_SUPPLIER_TEXT:
            raise ValueError(f"Supplier Names: '{text}' is too short, it would match too many invoices")
        out[text.lower()] = {"text": text, "supplier": supplier}
    return list(out.values())


def found_text(lines: list) -> str:
    """The text read from an invoice, kept so the supplier can be matched later
    and shown when it cannot be."""
    kept = [re.sub(r" {2,}", "  ", line.strip()) for line in lines if str(line).strip()]
    return "\n".join(kept)[:20000]


def contact_options(rows: list) -> list:
    """[{id, name, label}] for the Apex supplier dropdown, from (id, name) rows.
    A name used by more than one contact shows its id, so they can be told apart."""
    names = [str(r[1]).strip() for r in rows]
    out = [{"id": r[0], "name": str(r[1]).strip(),
            "label": str(r[1]).strip() + (f" (ID {r[0]})" if names.count(str(r[1]).strip()) > 1 else "")} for r in rows]
    return sorted(out, key=lambda c: c["label"].lower())


def clean_supplier_contacts(rows) -> list:
    """The mappings sent from the page. No contact chosen clears a supplier's mapping."""
    out = []
    for r in rows or []:
        supplier_id = to_int((r or {}).get("supplier_id"))
        if supplier_id is None:
            continue
        contact_id = to_int(r.get("contact_id"))
        out.append({"supplier_id": supplier_id, "contact_id": contact_id,
                    "contact_name": (str(r.get("contact_name") or "").strip()[:250] or None) if contact_id is not None else None})
    return out


def usable_supplier_names(rows) -> list:
    """An older saved list, keeping what clean_supplier_names accepts. One bad
    row must not stop the rest being moved across."""
    out = []
    for r in rows or []:
        try:
            out += clean_supplier_names([r])
        except (AttributeError, ValueError) as e:
            logger.warning(f"Cars Bought: supplier name {r} not kept: {e}")
    return clean_supplier_names(out)


def suggest_suppliers(text: str, own_names: list, limit: int = 5) -> list:
    """[{text, supplier}] that may name an unknown invoice's supplier: pieces
    of its text with a company word first (Ltd, Auctions...), then the first
    pieces on the page. Never our own names."""
    own = [n.lower() for n in own_names or [] if n.strip()]
    pieces = []
    for line in (text or "").splitlines():
        for cell in re.split(r"\s{2,}", line.strip()):
            name = cell.strip(" .,:;-\\")
            if (MIN_SUPPLIER_TEXT <= len(name) <= 50 and not _NOT_A_NAME.search(name)
                    and not any(o in name.lower() for o in own) and name not in pieces):
                pieces.append(name)
    chosen = [p for p in pieces if _COMPANY_WORD.search(p)] + [p for p in pieces[:8] if not _COMPANY_WORD.search(p)]
    out = []
    for name in chosen[:limit]:
        short = re.sub(r"[\s,.]*\b(ltd|limited|plc|llp)\b\.?$", "", name, flags=re.I)
        out.append({"text": name, "supplier": short.title() if short.isupper() else short})
    return out


def parse_layout(pages: list, names: dict, fee_words: list, supplier_names: list, supplier_rules: list = None) -> dict:
    """Vehicles and invoice details from a text PDF's layout text."""
    full = "\n".join(pages)
    supplier, supplier_text = match_supplier(full, supplier_names)
    fee_words, priced_lines_are_fees = apply_supplier_rule(supplier, fee_words, supplier_rules)
    lines = [l.rstrip() for l in full.replace("�", " ").replace("£", " ").splitlines()]
    cols, items, totals, prev, i = None, [], {}, None, 0
    while i < len(lines):
        line = lines[i]
        head = _header(lines, i, names)
        if head:
            cols, prev = head, None
            i += 2 if (i + 1 < len(lines) and lines[i + 1].strip() and not re.search(r"\d", lines[i + 1])) else 1
            continue
        i += 1
        if not cols or not line.strip():
            continue
        found = list(_MONEY_TOKEN.finditer(line))
        before = line[:found[0].start()] if found else line
        desc = " ".join(before.split())
        if found and any(c["pct"] for c in cols):
            desc = re.sub(r"\s+\d{1,2}(?:\.\d+)?$", "", desc)      # the VAT % printed before the amounts
        vrm = find_vrm(before)
        if found and not vrm and _total_field(desc, names):
            mapped = _map_amounts([m.group() for m in found], cols)
            if len(mapped) >= 2 and not totals:
                fees = sum((money(t) or Decimal("0")) for c, t in mapped if c["field"] == "fee_net")
                for c, t in mapped:
                    f = {"vehicle_net": "invoice_net", "vehicle_vat": "invoice_vat", "vehicle_gross": "invoice_gross"}.get(c["field"])
                    if f:
                        totals[f] = str((money(t) or Decimal("0")) + (fees if f == "invoice_net" else 0))
            prev = None
            continue
        if found and not vrm and totals and not is_fee(desc, fee_words):
            # Below the invoice's totals (a VAT summary, say): not a fee on a car.
            prev = None
            continue
        if not found and not vrm:
            if prev is not None:
                prev["text"] += " " + desc          # a description carried onto the next line
            continue
        item = {"text": desc, "description": desc, "fee_columns": []}
        if vrm:
            item["vrm"] = vrm
        for c, t in _map_amounts([m.group() for m in found], cols):
            if c["field"] == "fee_net":
                if money(t):
                    item["fee_columns"].append({"description": c["label"], "net": t, "in_row": True})
            elif c["field"] and c["field"] not in item:
                item[c["field"]] = t
        fields = {c["field"] for c in cols}
        if "mileage" in fields:
            miles = [w for w in before.split() if _MILEAGE_TOKEN.match(w) and not re.fullmatch(r"(19|20)\d\d", w)]
            if miles:
                item["mileage"] = miles[-1]
        if "first_registered" in fields:
            d = _DATE_TOKEN.search(before)
            if d:
                item["first_registered"] = d.group()
        items.append(item)
        prev = item

    vehicles = _assemble(items, fee_words, priced_lines_are_fees, names)
    if not vehicles:
        agreement = _sale_agreement(lines, names)
        if agreement:
            return agreement
        vehicles, row_totals = _priced_rows(lines, names, fee_words)
        if vehicles:
            totals = {**_form_totals(lines, names), **totals, **row_totals}
        else:
            vehicles = _form_vehicle(lines, names)
        if vehicles and not any(v.get("vehicle_gross") for v in vehicles):
            totals = {**_form_totals(lines, names), **totals}
            v = vehicles[0]
            _form_prices(v, lines, names, fee_words)
            v.setdefault("vehicle_net", totals.get("invoice_net"))
            v.setdefault("vehicle_vat", totals.get("invoice_vat"))
            v.setdefault("vehicle_gross", totals.get("invoice_gross"))
    if not totals:
        totals = _form_totals(lines, names)
    number = _INVOICE_NO.search(full)
    date = _INVOICE_DATE.search(full)
    return {"supplier_name": supplier, "supplier_text": supplier_text, "supplier_matched": bool(supplier),
            "found_text": found_text(lines),
            "invoice_number": number.group(1) if number else None,
            "invoice_date": date.group(1) if date else None,
            **totals, "vehicles": vehicles}


def _form_totals(lines: list, names: dict) -> dict:
    """Totals written as 'Label   amount' lines (SMC: 'Invoice Total 16,500.00')."""
    out = {}
    for line in lines:
        found = list(_MONEY_TOKEN.finditer(line))
        if len(found) != 1:
            continue
        f = _total_field(line[:found[0].start()], names)
        if f and f not in out:
            out[f] = found[0].group()
    return out


def _form_vehicle(lines: list, names: dict) -> list:
    """One car from a form with labels and no table (SMC: 'Reg No: HY72 ODU')."""
    vrm = _labelled_vrm(lines, names)
    if not vrm:
        return []
    v = {"vrm": vrm, "fees": []}
    _fill_form_details(v, lines, names)
    return [v]


def _labelled(lines: list) -> list:
    """[(label, value)] for every 'Label: value' on the lines. '|' or a wide gap
    before another label separates several on one line."""
    out = []
    for line in lines:
        for part in re.split(r"\||\s{2,}(?=[A-Za-z][^:|]{0,40}:)", line):
            label, colon, value = part.partition(":")
            if colon and label.strip() and value.strip():
                out.append((norm_label(label), value.strip()))
    return out


def _form_labels(names: dict, field: str) -> set:
    return {norm_label(a) for a in names.get(field, [])}


def _labelled_vrm(lines: list, names: dict) -> str | None:
    # 'VAT Reg No: 492 349 715' also has the label, so take the first that is a VRM.
    found = [m.group(1) for m in _FORM_VRM.finditer("\n".join(lines))]
    wanted = _form_labels(names or {}, "form_vrm")
    found += [value for label, value in _labelled(lines) if label in wanted]
    return next((clean_vrm(x) for x in found if _VRM_FULL.fullmatch(clean_vrm(x))), None)


def _fill_form_details(v: dict, lines: list, names: dict) -> None:
    text = "\n".join(lines)
    d = _FORM_VEHICLE.search(text)
    if d:
        # Up to the next wide gap: another label can sit on the same line.
        v["description"] = " ".join(re.split(r"\s{2,}", d.group(1).strip())[0].split())
    else:
        wanted = _form_labels(names or {}, "form_description")
        parts = [value for label, value in _labelled(lines) if label in wanted]
        if parts:
            v["description"] = " ".join(" ".join(parts).split())
    _fill_from_text(v, text)


def _form_prices(v: dict, lines: list, names: dict, fee_words: list) -> None:
    """A form's prices from labels: the car's price, and each amount whose label
    has a fee word ('Delivery cost: 185.00', 'Delivery VAT: 37.00'). A car with
    no VAT shown has the same gross as net (filled in by normalise_extract)."""
    prices, vats = _form_labels(names, "form_price"), _form_labels(names, "form_vat")
    fees = {}
    for label, value in _labelled(lines):
        found = _MONEY_TOKEN.search(value)
        if not found:
            continue
        amount = found.group()
        if label in prices:
            v.setdefault("vehicle_net", amount)
        elif label in vats:
            v.setdefault("vehicle_vat", amount)
        elif is_fee(label, fee_words) and not _total_field(label, names):
            words = label.split()
            name = " ".join(w for w in words if w not in ("vat", "cost", "net")) or label
            fee = fees.setdefault(name, {"description": name.title(), "net": None, "vat": None, "gross": None})
            fee["vat" if "vat" in words else "net"] = amount
    if v.get("vehicle_net") not in (None, ""):
        net = money(v["vehicle_net"])
        v.setdefault("vehicle_gross", str(net + (money(v.get("vehicle_vat")) or Decimal("0"))))
    for fee in fees.values():
        if fee["net"] is not None:
            fee["gross"] = str(money(fee["net"]) + (money(fee["vat"]) or Decimal("0")))
            v["fees"].append(fee)


def _priced_rows(lines: list, names: dict, fee_words: list) -> tuple:
    """(vehicles, totals) from a table the headings could not be matched on, using
    only the sums. A line ending in net, VAT and gross where net + VAT = gross is a
    priced row. A row with a VRM is a car and the rows after it are its fees; the
    row that is the sum of all the others is the invoice's total. The VRM can be
    printed above or below its prices, whichever the first row shows."""
    rows = []
    for i, line in enumerate(lines):
        found = list(_MONEY_TOKEN.finditer(line))
        amounts = [money(m.group()) for m in found[-3:]]
        if len(found) >= 3 and abs(amounts[0] + amounts[1] - amounts[2]) <= Decimal("0.01"):
            rows.append({"i": i, "text": line[:found[-3].start()], "net": amounts[0], "vat": amounts[1], "gross": amounts[2]})
    if not rows:
        return [], {}

    def block(k, above):
        row = rows[k]
        if above:
            start = rows[k - 1]["i"] + 1 if k else 0
            return lines[start:row["i"]] + [row["text"]]
        end = rows[k + 1]["i"] if k + 1 < len(rows) else len(lines)
        return [row["text"]] + lines[row["i"] + 1:end]

    def vrm_in(part):
        return _labelled_vrm(part, names) or find_vrm("\n".join(part))

    above = bool(vrm_in(block(0, True)))
    vehicles, totals = {}, {}
    last = rows[-1]
    others = rows[:-1]
    if others and abs(sum((r["net"] for r in others), Decimal("0")) - last["net"]) <= Decimal("0.01") and \
            abs(sum((r["gross"] for r in others), Decimal("0")) - last["gross"]) <= Decimal("0.01"):
        totals = {"invoice_net": str(last["net"]), "invoice_vat": str(last["vat"]), "invoice_gross": str(last["gross"])}
        rows = others
    current = None
    for k, row in enumerate(rows):
        part = block(k, above)
        vrm = vrm_in(part)
        description = re.sub(r"^\S*\d\S*\s+", "", " ".join(row["text"].split()))
        if vrm and vrm not in vehicles and not is_fee(description, fee_words):
            current = vehicles[vrm] = {"vrm": vrm, "fees": [], "vehicle_net": str(row["net"]),
                                       "vehicle_vat": str(row["vat"]), "vehicle_gross": str(row["gross"])}
            _fill_form_details(current, part, names)
        elif current and (row["net"] or row["vat"] or row["gross"]):
            current["fees"].append({"description": description or "Fee", "net": str(row["net"]),
                                    "vat": str(row["vat"]), "gross": str(row["gross"])})
    return list(vehicles.values()), totals if vehicles else {}


def _sale_agreement(lines: list, names: dict) -> dict | None:
    """A broker's sale agreement (Motorspace), or None if this is not one. The
    car is bought from the Selling Dealership at the Sales Price, with no VAT
    shown, so it is margin. The broker's Commission, its VAT and Total are a
    fee. The printed Total is only the fee, so it is not kept as the invoice's.
    These labels are fixed in code as they only mean this on this one form."""
    text = "\n".join(lines)

    def amount(label):
        m = re.search(rf"\b{label}\s*:\s*((?:\d{{1,3}}(?:,\d{{3}})+|\d+)\.\d{{2}})(?!\d)", text, re.I)
        return m.group(1) if m else None

    price, commission = amount(r"sales? price"), amount("commission")
    vehicles = _form_vehicle(lines, names) if price and commission else []
    if not vehicles:
        return None
    v = vehicles[0]
    v.update(vehicle_net=price, vehicle_gross=price)
    v["fees"].append({"description": "Commission", "net": commission, "vat": amount("vat"), "gross": amount("total")})
    seller = re.search(r"selling dealership\s*:\s*(\S.*?)(?:\s{2,}|$)", text, re.I | re.M)
    # The date sits on the line under its label.
    date = re.search(r"^\s*date\s*:.*\n\s*(\d{1,2}/\d{1,2}/\d{2,4})", text, re.I | re.M)
    # The supplier is named on the form itself, so it is not looked up in the list.
    return {"supplier_name": seller.group(1) if seller else None, "supplier_matched": bool(seller),
            "found_text": found_text(lines), "invoice_number": None,
            "invoice_date": date.group(1) if date else None,
            "notes": "Broker's sale agreement, not an invoice: the commission is shown as a fee",
            "vehicles": vehicles}


def read_invoice(filename: str, data: bytes, names: dict, fee_words: list, supplier_names: list,
                 fee_vat_rate: float = DEFAULT_FEE_VAT_RATE, on_scan=None, supplier_rules: list = None) -> tuple:
    """(rows in the page's shape, how it was read) for an uploaded file. Text
    PDFs are read from their own text; scans, photos and text PDFs where no
    car was found go to Textract (on_scan is called first, for the progress)."""
    kind = file_kind(filename)
    if kind in ("csv", "excel"):
        return normalise_extract(parse_sheet(filename, data, names, fee_words), fee_vat_rate), "sheet"
    if kind in ("html", "email"):
        # An email's text is never a scan, so it never goes to Textract.
        raw = {}
        for text in [html_text(data)] if kind == "html" else email_texts(filename, data):
            raw = parse_layout([text], names, fee_words, supplier_names, supplier_rules)
            if raw["vehicles"]:
                break
        return normalise_extract(raw, fee_vat_rate), "text"
    if kind == "pdf":
        pages = pdf_layout_pages(data)
        if len("".join(pages).split()) > 20:
            raw = parse_layout(pages, names, fee_words, supplier_names, supplier_rules)
            if raw["vehicles"]:
                return normalise_extract(raw, fee_vat_rate), "text"
    if on_scan:
        on_scan()
    raw = parse_expense(analyze_expense(filename, data), names, fee_words, supplier_names, supplier_rules)
    return normalise_extract(raw, fee_vat_rate), "textract"


def _num(v):
    m = money(v)
    return float(m) if m is not None else None


def split_row_fees(fees: list, vehicle_vat, vehicle_gross, rate: float) -> tuple:
    """Fees printed in their own column on a car's row ('in_row') have their
    VAT inside the row's VAT and total. Each such fee gets rate % VAT (never
    more than the VAT left, so a margin-scheme car whose row VAT is only the
    fee's stays right) and its gross; the car keeps the rest. Returns the
    car's (vat, gross)."""
    for f in fees:
        if not f.pop("in_row", False) or f["net"] is None:
            continue
        vat = (Decimal(str(f["net"])) * Decimal(str(rate)) / 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
        if vehicle_vat is not None:
            vat = min(vat, Decimal(str(vehicle_vat)))
            vehicle_vat = float(Decimal(str(vehicle_vat)) - vat)
        f["vat"] = float(vat)
        f["gross"] = float(Decimal(str(f["net"])) + vat)
        if vehicle_gross is not None:
            vehicle_gross = float(Decimal(str(vehicle_gross)) - Decimal(str(f["gross"])))
    return vehicle_vat, vehicle_gross


def vat_status(vehicle: dict) -> str:
    """A car with no VAT shown on it is margin. Its fees have their own VAT,
    which can be different, and do not count."""
    return "VAT Qualifying" if money(vehicle.get("vehicle_vat")) else "Margin"


def purchase_total(vehicle: dict) -> float:
    """What the car cost in all: the car, every fee and all VAT."""
    gross = money(vehicle.get("vehicle_gross"))
    if gross is None:
        gross = (money(vehicle.get("vehicle_net")) or Decimal("0")) + (money(vehicle.get("vehicle_vat")) or Decimal("0"))
    return float(gross + (money(vehicle.get("fees_gross")) or Decimal("0")))


def normalise_extract(raw: dict, fee_vat_rate: float = DEFAULT_FEE_VAT_RATE) -> dict:
    """Clean what was read into the row shape the page uses: VRMs cleaned,
    numbers as floats, fees totalled per vehicle (the list is kept too)."""
    vehicles = []
    for v in raw.get("vehicles") or []:
        vrm = clean_vrm(v.get("vrm"))
        if not vrm:
            continue
        fees = [{"description": str(f.get("description") or "").strip(),
                 "net": _num(f.get("net")), "vat": _num(f.get("vat")), "gross": _num(f.get("gross")),
                 "in_row": bool(f.get("in_row"))}
                for f in (v.get("fees") or [])]
        vehicle_vat, vehicle_gross = split_row_fees(fees, _num(v.get("vehicle_vat")), _num(v.get("vehicle_gross")),
                                                    fee_vat_rate)
        vehicle_net = _num(v.get("vehicle_net"))
        if not vehicle_vat:
            # Margin: no VAT on the car, so its net and gross are the same.
            vehicle_net = vehicle_gross if vehicle_net is None else vehicle_net
            vehicle_gross = vehicle_net if vehicle_gross is None else vehicle_gross
            vehicle_vat = 0.0 if vehicle_net is not None else vehicle_vat
        reg = to_date(v.get("first_registered"))
        vehicles.append({
            "vrm": vrm,
            "mileage": to_int(v.get("mileage")),
            "vin": (str(v.get("vin")).strip().upper() or None) if v.get("vin") else None,
            "first_registered": reg.isoformat() if reg else None,
            "supplier_description": (v.get("description") or None),
            "colour": v.get("colour") or None,
            "vehicle_net": vehicle_net,
            "vehicle_vat": vehicle_vat,
            "vehicle_gross": vehicle_gross,
            "fees": fees,
            "fees_net": round(sum(f["net"] or 0 for f in fees), 2),
            "fees_vat": round(sum(f["vat"] or 0 for f in fees), 2),
            "fees_gross": round(sum(f["gross"] or 0 for f in fees), 2),
        })
    date = to_date(raw.get("invoice_date"))
    return {
        "supplier_name": raw.get("supplier_name") or None,
        "supplier_text": raw.get("supplier_text") or None,
        "supplier_matched": bool(raw.get("supplier_matched")),
        "found_text": raw.get("found_text") or "",
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

DEFAULT_EVALUATOR_MAX_HOURS = 24


def evaluator_lookup(conn, vrms: list, supplier_id=None, max_age_hours=None) -> dict:
    """{vrm: {...}} for VRMs the evaluator has seen: the latest listing (from
    the chosen supplier if given, else any supplier), the CAP identity, and
    the latest valuation (CAP and Autotrader). With max_age_hours, only
    listings and valuations that new are used, and a VRM with neither is left
    out. VRMs it has never seen are left out. Times are stored in UTC."""
    if not vrms:
        return {}
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(hours=max_age_hours)) if max_age_hours else None
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            WITH listing AS (
                SELECT DISTINCT ON (l.vrm) l.vrm, l.supplier_id, s.supplier_name, l.imported_at,
                       l.mileage, l.asking_price
                FROM supplier_vehicle_listings l
                JOIN suppliers s ON s.supplier_id = l.supplier_id
                WHERE l.vrm IN %s AND (%s IS NULL OR l.supplier_id = %s)
                  AND (%s::timestamp IS NULL OR l.imported_at >= %s::timestamp)
                ORDER BY l.vrm, l.imported_at DESC
            ),
            valuation AS (
                SELECT DISTINCT ON (vrm) vrm, fetched_at, at_retail_valuation, at_rating,
                       cap_live_clean, cap_monthly_clean
                FROM vehicle_valuations
                WHERE vrm IN %s AND (%s::timestamp IS NULL OR fetched_at >= %s::timestamp)
                ORDER BY vrm, fetched_at DESC
            )
            SELECT v.vrm, v.cap_capid, v.cap_manufacturer, v.cap_model, v.cap_derivative,
                   v.at_first_registration_date, v.at_vin,
                   l.supplier_id, l.supplier_name, l.imported_at, l.mileage, l.asking_price,
                   val.fetched_at, val.at_retail_valuation, val.at_rating,
                   COALESCE(val.cap_live_clean, val.cap_monthly_clean) AS cap_clean
            FROM vehicles v
            LEFT JOIN listing l ON l.vrm = v.vrm
            LEFT JOIN valuation val ON val.vrm = v.vrm
            WHERE v.vrm IN %s
        """, (tuple(vrms), supplier_id, supplier_id, cutoff, cutoff, tuple(vrms), cutoff, cutoff, tuple(vrms)))
        rows = cur.fetchall()
    now = datetime.datetime.utcnow()
    out = {}
    for r in rows:
        if cutoff is not None and r["imported_at"] is None and r["fetched_at"] is None:
            continue          # nothing recent enough
        if r["supplier_id"] is None and r["cap_capid"] is None:
            continue
        times = [t.replace(tzinfo=None) for t in (r["imported_at"], r["fetched_at"]) if t]
        reg = r["at_first_registration_date"]
        out[r["vrm"]] = {
            "evaluator_age_hours": round((now - max(times)).total_seconds() / 3600, 1) if times else None,
            "at_rating": float(r["at_rating"]) if r["at_rating"] is not None else None,
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


def stored_results(vrms_mileage: list, cached: dict, known: dict) -> dict:
    """The same results as manual_check, from what the shared store already
    holds: fetch_cached_valuations and fetch_known_vehicles. Only a car with a
    CAP ID, a CAP value and an Autotrader value, as a car missing one still
    needs the live call."""
    out = {}
    for vrm, mileage in vrms_mileage:
        if not mileage:
            continue
        val = cached.get((vrm, round_mileage_to_nearest_thousand(mileage)))
        car = known.get(vrm)
        if not val or not car or not car.get("cap_capid"):
            continue
        clean = val.get("cap_live_clean") if val.get("cap_live_clean") is not None else val.get("cap_monthly_clean")
        if clean is None or val.get("at_retail_valuation") is None:
            continue
        out[vrm] = {"cap_id": car["cap_capid"], "make": car.get("cap_manufacturer"), "model": car.get("cap_model"),
                    "derivative": car.get("cap_derivative"), "cap_clean": _num(clean),
                    "at_retail_valuation": _num(val["at_retail_valuation"]), "at_rating": _num(val.get("at_rating")),
                    "note": None, "from_store": True}
    return out


def manual_check(vrms_mileage: list, on_result=None) -> dict:
    """{vrm: {cap_id, make, model, derivative, cap_clean, at_retail_valuation,
    at_rating, note}} from live CAP and Autotrader, using the Assess Vehicle
    page's own lookups. Autotrader needs a mileage. on_result(vrm, mileage, cap,
    at) gets the full answers of each car that has a mileage and where neither
    call failed, for storing."""
    from concurrent.futures import ThreadPoolExecutor
    from routers.assess_vehicle import _at_lookup, _cap_lookup

    from utils import api_limits

    def cap_one(vrm, mileage):
        try:
            with api_limits.quick():
                c = _cap_lookup(vrm, mileage or 0)
        except Exception as e:
            return {}, f"CAP failed: {e}", None
        out = {"cap_id": c.get("cap_id"), "make": c.get("make"), "model": c.get("model"),
               "derivative": c.get("derivative"),
               "cap_clean": c.get("live_clean") if c.get("live_clean") is not None else c.get("monthly_clean")}
        return out, None if c.get("cap_id") else f"CAP: {c.get('note') or 'not found'}", c

    def at_one(vrm, mileage):
        if not mileage:
            return {}, "Autotrader needs a mileage", None
        try:
            a = _at_lookup(vrm, mileage)
        except Exception as e:
            return {}, f"Autotrader failed: {e}", None
        if not a.get("found"):
            return {}, "Autotrader: not found", a
        return {"at_retail_valuation": _num(a.get("retail_valuation")), "at_rating": _num(a.get("retail_rating"))}, None, a

    if not vrms_mileage:
        return {}
    api_limits.apply()
    n = len(vrms_mileage)
    # CAP and Autotrader for every car at once, each up to its Settings
    # workers: the slowest single call sets the time, not the sum of them.
    with ThreadPoolExecutor(max_workers=api_limits.workers("cap", n)) as cap_pool, \
            ThreadPoolExecutor(max_workers=api_limits.workers("at", n)) as at_pool:
        jobs = [(vrm, m, cap_pool.submit(cap_one, vrm, m), at_pool.submit(at_one, vrm, m)) for vrm, m in vrms_mileage]
        results = {}
        for vrm, m, fc, fa in jobs:
            (c, cn, cap_full), (a, an, at_full) = fc.result(), fa.result()
            results[vrm] = {**c, **a, "note": "; ".join(n for n in (cn, an) if n) or None}
            if on_result and m and cap_full is not None and at_full is not None:
                on_result(vrm, m, cap_full, at_full)
    return results


def buyer_figures_from_rows(rows: list) -> list:
    """[{vrm, rrp and/or est_prep_cost}] from purchase rows' custom values, the
    filled-in ones only, so a blank box does not clear a figure a buyer gave earlier."""
    out = []
    for r in rows:
        custom = r.get("custom") or {}
        figures = {k: money(custom[k]) for k in BUYER_FIGURES if money(custom.get(k)) is not None}
        if figures:
            out.append({"vrm": r["vrm"], **figures})
    return out




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
        field = {"key": key, "label": label, "type": ftype, "required": bool(f.get("required"))}
        suppliers = [str(s).strip()[:200] for s in f.get("suppliers") or [] if str(s).strip()]
        if suppliers:
            field["suppliers"] = suppliers
        out.append(field)
    return out


def field_applies(field: dict, supplier: str) -> bool:
    """Whether a custom field is for this supplier. No suppliers set means all
    of them. Same as fieldApplies() in cars_bought.js."""
    names = field.get("suppliers") or []
    return not names or str(supplier or "").strip().lower() in {str(n).strip().lower() for n in names}


def add_sheet_fields(fields: list) -> list:
    """The fields plus any of SHEET_CUSTOM_FIELDS not already there, by key or label."""
    keys = {f.get("key") for f in fields}
    labels = {str(f.get("label") or "").strip().lower() for f in fields}
    return list(fields) + [dict(s) for s in SHEET_CUSTOM_FIELDS if s["key"] not in keys and s["label"].lower() not in labels]


def clean_custom_value(field: dict, value):
    """A value for one custom field as it is stored. Blank is None. Raises
    ValueError when it does not fit the field's type."""
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    ftype = field.get("type")
    if ftype == "yesno":
        if text not in ("Yes", "No"):
            raise ValueError(f"{field.get('label')} must be Yes or No")
        return text
    if ftype in ("money", "number"):
        amount = money(text)
        if amount is None:
            raise ValueError(f"{field.get('label')} must be a number")
        return float(amount)
    return text[:500]


def metric_column(metric: dict) -> str:
    return f"METRIC:{metric['id'].upper()}"


def table_live_columns(fields: list, metrics: list = None) -> list:
    """Every column the All Vehicles Bought table can show."""
    live = [{"column": c, "type": "", "label": label, "format": fmt, "decimals": 2 if fmt == "gbp" else 0}
            for c, label, fmt in TABLE_FIELDS]
    for f in fields:
        fmt = {"money": "gbp", "number": "number"}.get(f.get("type"), "text")
        live.append({"column": f"CUSTOM:{f['key'].upper()}", "type": "buyer fills in", "label": f.get("label"),
                     "format": fmt, "decimals": 2 if fmt == "gbp" else 0})
    for m in metrics or []:
        live.append({"column": metric_column(m), "type": "metric", "label": m["label"], "format": m["format"], "decimals": m["decimals"]})
    return live


def read_table_columns(saved, fields: list, metrics: list = None) -> list:
    """The columns chosen in Settings, else the buying sheet's."""
    if isinstance(saved, list) and saved:
        return saved
    live = {c["column"]: c for c in table_live_columns(fields, metrics)}
    return [{k: live[c][k] for k in ("column", "label", "format", "decimals")} for c in DEFAULT_TABLE_ORDER if c in live]


def table_columns_after_metric_save(columns: list, fields: list, metrics: list) -> list:
    """After a metric is saved or deleted: a new one goes on the table, a deleted
    one comes off, and one already shown takes its new label and format."""
    live = {c["column"]: c for c in table_live_columns(fields, metrics)}
    wanted = {metric_column(m) for m in metrics}
    out = []
    for c in columns:
        if c["column"].startswith("METRIC:"):
            if c["column"] not in wanted:
                continue
            c = {**c, **{k: live[c["column"]][k] for k in ("label", "format", "decimals")}}
        out.append(c)
    shown = {c["column"] for c in out}
    return out + [{k: live[metric_column(m)][k] for k in ("column", "label", "format", "decimals")}
                  for m in metrics if metric_column(m) not in shown]


def clean_table_columns(columns, fields: list, metrics: list = None) -> list:
    """The columns sent from the page: known ones only, each once."""
    live = {c["column"]: c for c in table_live_columns(fields, metrics)}
    out, seen = [], set()
    for c in columns or []:
        name = str((c or {}).get("column") or "")
        if name not in live or name in seen:
            continue
        seen.add(name)
        fmt = c.get("format") if c.get("format") in TABLE_FORMATS else live[name]["format"]
        out.append({"column": name, "label": str(c.get("label") or "").strip()[:60] or live[name]["label"], "format": fmt,
                    "decimals": max(0, min(4, to_int(c.get("decimals")) or 0))})
    return out


def add_new_field_columns(columns: list, old_fields: list, new_fields: list) -> list:
    """A newly added custom field also goes on the table, so buyers can fill it in."""
    old_keys = {f.get("key") for f in old_fields}
    live = {c["column"]: c for c in table_live_columns(new_fields)}
    shown = {c["column"] for c in columns}
    extra = [f"CUSTOM:{f['key'].upper()}" for f in new_fields if f["key"] not in old_keys]
    return columns + [{k: live[c][k] for k in ("column", "label", "format", "decimals")} for c in extra if c not in shown]


def table_rows(rows: list) -> list:
    """Vehicle rows for the table, with the two worked-out figures."""
    out = []
    for r in rows:
        row = {k: v for k, v in r.items() if k != "fees_detail"}
        row["vat_status"] = vat_status(r)
        row["purchase_total"] = purchase_total(r)
        out.append(row)
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
                   COALESCE(SUM(COALESCE(v.vehicle_gross, 0) + COALESCE(v.fees_gross, 0)), 0), b.buyer, b.s3_key IS NOT NULL
            FROM purchase_batches b LEFT JOIN purchase_vehicles v ON v.batch_id = b.batch_id
            GROUP BY b.batch_id ORDER BY b.purchase_date DESC, b.batch_id DESC LIMIT %s""", (limit,))
        rows = cur.fetchall()
    conn.commit()
    return [{"batch_id": r[0], "supplier_name": r[1], "invoice_number": r[2], "purchase_date": r[3].isoformat(),
             "source": r[4], "filename": r[5], "invoice_gross": float(r[6]) if r[6] is not None else None,
             "submitted_by": r[7], "submitted_at": r[8].isoformat(timespec="minutes"), "vehicles": r[9],
             "rows_gross": float(r[10]), "buyer": r[11], "has_file": r[12]} for r in rows]


def batch_file(conn, batch_id: int) -> tuple:
    """(filename, s3_key) of the file a batch was read from; (None, None) if none."""
    ensure_tables(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT filename, s3_key FROM purchase_batches WHERE batch_id = %s", (batch_id,))
        row = cur.fetchone()
    conn.commit()
    return row if row else (None, None)


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
                   v.id AS vehicle_id, b.s3_key IS NOT NULL AS has_file,
                   {', '.join('v.' + c for c in VEHICLE_COLS)}
            FROM purchase_vehicles v JOIN purchase_batches b ON b.batch_id = v.batch_id
            {('WHERE ' + ' AND '.join(where)) if where else ''}
            ORDER BY b.purchase_date DESC, b.batch_id DESC, v.id""", params)
        rows = [dict(r) for r in cur.fetchall()]
    conn.commit()
    return rows


def update_vehicle_custom(conn, vehicle_id: int, key: str, value):
    """Set one custom field on one bought car, leaving its others alone. Returns
    the car's VRM, or None if there is no such car."""
    ensure_tables(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE purchase_vehicles SET custom = COALESCE(custom, '{}'::jsonb) || %s::jsonb WHERE id = %s RETURNING vrm",
                    (json.dumps({key: value}), vehicle_id))
        row = cur.fetchone()
    conn.commit()
    return row[0] if row else None


def batches_without_supplier(conn) -> list:
    """Supplier names on purchases not yet linked to the supplier list."""
    ensure_tables(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT supplier_name FROM purchase_batches WHERE supplier_id IS NULL")
        names = [r[0] for r in cur.fetchall()]
    conn.commit()
    return names


def link_batches_to_suppliers(conn, ids: dict) -> None:
    """ids: {supplier name as on the purchase: supplier_id}."""
    with conn.cursor() as cur:
        for name, supplier_id in ids.items():
            cur.execute("UPDATE purchase_batches SET supplier_id = %s WHERE supplier_id IS NULL AND supplier_name = %s", (supplier_id, name))
    conn.commit()


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
              "VAT Status", "Vehicle Net", "Vehicle VAT", "Vehicle Gross", "Fees Net", "Fees VAT", "Fees Gross",
              "Total Net", "Total VAT", "Total Purchase", "Fees Detail",
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
                    vat_status(r), n("vehicle_net"), n("vehicle_vat"), n("vehicle_gross"), n("fees_net"), n("fees_vat"), n("fees_gross"),
                    tot("net"), tot("vat"), purchase_total(r),
                    "; ".join(f"{f.get('description')}: {f.get('gross') if f.get('gross') is not None else f.get('net')}" for f in fees),
                    "Yes" if r["from_evaluator"] else "No", n("evaluator_list_price"), n("at_retail_valuation"), n("cap_clean")] +
                   [custom.get(f["key"], "") for f in custom_fields] +
                   [r["submitted_by"], r["submitted_at"].strftime("%d/%m/%Y %H:%M")])
    return header, out
