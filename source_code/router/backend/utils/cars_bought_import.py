"""Import the old Cars Bought sheet, saved as a CSV, so the All Cars Bought table
holds the history from before cars were added on this site.

The page shows which CSV column goes into which field, suggested from the names
the table uses, and the admin can change any of them. Cars are grouped into one
purchase per date, supplier, buyer and invoice number. A car with the same VRM
and date as one already stored is left out, so a file can be imported again
safely, and a large file can be saved a part at a time.
"""
import csv
import io
import re

from . import cars_bought as cb

SOURCE = cb.IMPORT_SOURCE

# (field, label, longest text the column holds). Text fields are cut to fit.
_BATCH_FIELDS = [("purchase_date", "Date", None), ("buyer", "Buyer", 100), ("supplier_name", "Supplier", 200),
                 ("invoice_number", "Invoice Number", 100)]
_CAR_FIELDS = [("vrm", "VRM", 20), ("mileage", "Mileage", None), ("first_registered", "Date First Registered", None),
               ("make", "Make", 100), ("model", "Model", 150), ("derivative", "Variant", 255), ("cap_id", "CAP ID", None),
               ("colour", "Colour", 50), ("vin", "VIN", 30), ("supplier_description", "Supplier Description", None),
               ("vehicle_net", "Vehicle Net", None), ("vehicle_vat", "Vehicle VAT", None),
               ("vehicle_gross", "Purchase Price (Vehicle Gross)", None), ("fees_net", "Fees Net", None),
               ("fees_vat", "Fees VAT", None), ("fees_gross", "Fees Gross", None),
               ("at_retail_valuation", "AT Valuation", None), ("cap_clean", "CAP Clean", None)]
_LIMITS = {key: limit for key, _, limit in _BATCH_FIELDS + _CAR_FIELDS if limit}
_TABLE_COLUMN_FIELD = {"PURCHASE_TOTAL": "vehicle_gross"}   # the table's Purchase Price is worked out from this

REQUIRED = {"purchase_date": "Date", "supplier_name": "Supplier", "vrm": "VRM"}


def _plain(text) -> str:
    return re.sub(r"[^a-z0-9%]", "", str(text or "").lower())


def field_options(custom_fields: list) -> list:
    """[{key, label}] of everything a CSV column can go into."""
    return ([{"key": k, "label": label} for k, label, _ in _BATCH_FIELDS + _CAR_FIELDS]
            + [{"key": f"custom:{f['key']}", "label": f.get("label") or f["key"]} for f in custom_fields])


def suggest_mapping(headings: list, custom_fields: list) -> dict:
    """{CSV heading: field key}, for headings with the same name as a field or a
    column of the All Cars Bought table. Each field is used once, by the first
    heading that matches it. Headings with no match are left out."""
    names = {}
    for key, label, _ in _BATCH_FIELDS + _CAR_FIELDS:
        names.setdefault(_plain(label), key)
        names.setdefault(_plain(key), key)
    for column, label, _ in cb.TABLE_FIELDS:
        key = _TABLE_COLUMN_FIELD.get(column, column.lower())
        if any(key == k for k, _, _ in _BATCH_FIELDS + _CAR_FIELDS):
            names.setdefault(_plain(label), key)
    for f in custom_fields:
        names.setdefault(_plain(f.get("label")), f"custom:{f['key']}")
    out, used = {}, set()
    for heading in headings:
        key = names.get(_plain(heading))
        if key and key not in used:
            out[heading] = key
            used.add(key)
    return out


def read_csv(data: bytes) -> tuple:
    """(headings, rows as lists). Headings are the first row with anything in it."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    rows = [[cell.strip() for cell in row] for row in csv.reader(io.StringIO(text))]
    rows = [row for row in rows if any(row)]
    if not rows:
        raise ValueError("the file is empty")
    return rows[0], rows[1:]


def _custom_value(field: dict, text: str):
    # The sheet says YES and NO, in capitals.
    if field.get("type") == "yesno":
        text = {"yes": "Yes", "no": "No"}.get(text.lower(), text)
    return cb.clean_custom_value(field, text)


def _read_row(row: list, columns: list, custom_fields: dict) -> tuple:
    """({field: value}, problems) for one CSV row. A custom value that does not
    fit its field is left blank and reported, and the car is still imported."""
    car, custom, notes = {}, {}, []
    for at, key in columns:
        text = row[at] if at < len(row) else ""
        if not text:
            continue
        if key.startswith("custom:"):
            field = custom_fields.get(key[7:])
            try:
                value = _custom_value(field, text) if field else None
            except ValueError:
                notes.append(f"{field.get('label')} left blank: {text[:40]}")
                continue
            if value is not None:
                custom[key[7:]] = value
        else:
            car[key] = text[:_LIMITS[key]] if key in _LIMITS else text
    car["custom"] = custom
    return car, notes


def plan(headings: list, rows: list, mapping: dict, custom_fields: list, stored: dict) -> dict:
    """What importing would do. stored: {vrm: set of purchase dates} already in
    the database. Nothing is saved here."""
    options = {o["key"] for o in field_options(custom_fields)}
    columns = [(at, mapping[h]) for at, h in enumerate(headings) if mapping.get(h) in options]
    custom_by_key = {f["key"]: f for f in custom_fields}
    mapped = {key for _, key in columns}
    missing = [label for key, label in REQUIRED.items() if key not in mapped]

    batches, problems, seen = {}, [], set()
    same_day, other_day, cars = 0, 0, 0
    for line, row in enumerate(rows, start=2):
        if missing:
            break
        car, notes = _read_row(row, columns, custom_by_key)
        vrm = cb.clean_vrm(car.get("vrm"))
        date = cb.to_date(car.get("purchase_date"))
        supplier = str(car.get("supplier_name") or "").strip()
        where = f"Row {line}" + (f" ({vrm})" if vrm else "")
        reason = ("no VRM" if not vrm else
                  f"date not read: {str(car.get('purchase_date') or 'blank')[:20]}" if not date else
                  "no supplier" if not supplier else None)
        if reason:
            problems.append(f"{where} left out: {reason}")
            continue
        if (vrm, date) in seen:
            problems.append(f"{where} left out: on an earlier row with the same date")
            continue
        seen.add((vrm, date))
        if date in stored.get(vrm, ()):
            same_day += 1
            continue
        if stored.get(vrm):
            other_day += 1
        problems.extend(f"{where}: {n}" for n in notes)
        buyer = str(car.get("buyer") or "").strip()
        invoice = str(car.get("invoice_number") or "").strip()
        batch = batches.setdefault((date, supplier.lower(), buyer.lower(), invoice),
                                   {"purchase_date": date, "supplier_name": supplier, "buyer": buyer or None,
                                    "invoice_number": invoice or None, "vehicles": []})
        batch["vehicles"].append({**{k: v for k, v in car.items() if k not in ("purchase_date", "supplier_name", "buyer", "invoice_number")},
                                  "vrm": vrm})
        cars += 1
    return {"missing": missing, "batches": list(batches.values()), "cars": cars, "rows": len(rows),
            "already_here": same_day, "bought_on_another_date": other_day, "problems": problems}


def stored_purchases(conn) -> dict:
    """{vrm: set of purchase dates} for every car already stored."""
    cb.ensure_tables(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT v.vrm, b.purchase_date FROM purchase_vehicles v JOIN purchase_batches b ON b.batch_id = v.batch_id")
        out = {}
        for vrm, date in cur.fetchall():
            out.setdefault(vrm, set()).add(date)
    return out
