"""Autotrader monthly invoices (PDF), imported in KPI Settings > Autotrader.

Each invoice line (base package, finance calculator, tactical adverts, Connect
Retail Strategy) is stored with its own date range, so the KPI charts can work
out the cost and slots for every day from what was actually charged. The PPC
campaign line is left out: PPC is costed from its own setting (cost per PPC
advert view). Lines that are not recognised make the whole PDF refused, and
the lines kept must add up to the invoice's subtotal excluding VAT minus PPC.
"""

import datetime
import io
import re
from decimal import Decimal

# Flex (Autotrader's "Bonus Advert Allowance") is 10% of the base slots,
# rounded to the nearest whole slot. Invoices do not show it, so it is fixed
# here; the adjustment email rows in Settings carry their own Flex %.
INVOICE_FLEX_PCT = 10

LINE_TYPES = ("base", "finance", "tactical", "connect")

_MONEY = r"(-?[\d,]+\.\d{2})"
_DATED_LINE = re.compile(r"^(\d{2}/\d{2}/\d{2})\s*-\s*(\d{2}/\d{2}/\d{2})\s+(.+?)\s+" + _MONEY + r"$")
_PPC_START = re.compile(r"^AT PPC Campaign\b")
# "AT PPC Campaign - 40000 Clicks @ 0.23 per click 9,200.00". One invoice can
# have several at different prices (a lower price past a number of clicks).
_PPC_LINE = re.compile(r"^AT PPC Campaign\s*-\s*([\d,]+) Clicks @ ([\d.]+) per click\s+" + _MONEY + r"$")
# A line under the one before, with no dates of its own (extra invoices list
# several lines under one date range).
_UNDATED_LINE = re.compile(r"^(.+?)\s+" + _MONEY + r"$")
# Base: the package that holds the adverts (slots counted). "Market Extension
# Location Plus" is an add-on charged on the same adverts, so it is base cost
# with no slots of its own; likewise its tactical line. Descriptions on extra
# invoices go on "(based upon group total of 1015)", sometimes wrapped.
_BASE_ADDON = re.compile(r"^Market Extension Location Plus\b(?!.*Tactical).*?-\s*(\d+) Adverts\b")
_BASE = re.compile(r"^(?:Market Extension\b.*?|Ultra Package)\s*-\s*(\d+) Adverts\b")
# "Ultra Package At Super Rate - 40 Adverts ... -457.27": a credit against the
# Ultra Package, so base cost (negative, so it is subtracted) with no slots.
_BASE_CREDIT = re.compile(r"^Ultra Package At Super Rate\s*-\s*(\d+) Adverts\b")
_FINANCE = re.compile(r"^Finance Calculator\s*-\s*(\d+) Adverts\b")
_TACTICAL_ADDON = re.compile(r"^(\d+) Market Extension Location Plus Tactical Adverts\b")
_TACTICAL = re.compile(r"^(\d+) Tactical Adverts\b")
_CONNECT = re.compile(r"^Connect: Retail Strategy$")
_DEALER = re.compile(r"Dealer ID\s*-\s*(\d+)")
_INVOICE_NO = re.compile(r"Invoice Number:\s*([A-Z0-9]+)")
_ISSUED = re.compile(r"Date Issued:\s*(\d{2}/\d{2}/\d{4})")
_SUBTOTAL = re.compile(r"Subtotal excl\. VAT[^\d-]*" + _MONEY)


def flex_slots(base: int, pct: float = INVOICE_FLEX_PCT) -> int:
    """Flex slots, rounded half up to a whole slot (1035 -> 104)."""
    return int(base * pct / 100 + 0.5)


def _money(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def _short_date(text: str) -> datetime.date:
    return datetime.datetime.strptime(text, "%d/%m/%y").date()


def parse_invoice(pdf_bytes: bytes) -> dict:
    """Read one invoice PDF. Returns {invoice_number, issued, subtotal, ppc,
    lines: [{line_type, date_from, date_to, slots, charge, description}]}.
    Raises ValueError, with a message for the page, when the PDF cannot be
    read, has a line that is not recognised, or does not add up."""
    try:
        # Inside the try: a server without pypdf installed (an image built
        # before it was added) then gives a readable message, not a 500 page.
        from pypdf import PdfReader
        pages = [p.extract_text() or "" for p in PdfReader(io.BytesIO(pdf_bytes)).pages]
    except Exception as e:
        raise ValueError(f"Could not read the PDF: {type(e).__name__}: {e}")
    if not pages:
        raise ValueError("The PDF has no pages")
    text = "\n".join(pages)
    number = _INVOICE_NO.search(text)
    issued = _ISSUED.search(text)
    subtotal = _SUBTOTAL.search(pages[0])
    if not number or not issued or not subtotal:
        raise ValueError("Not an Autotrader invoice: Invoice Number, Date Issued or Subtotal excl. VAT not found")

    lines, unknown, ppc, ppc_clicks = [], [], Decimal("0"), 0
    # Page 1 is the summary; the lines are on the pages after it.
    for raw in "\n".join(pages[1:]).splitlines():
        line = raw.strip()
        if _PPC_START.match(line):
            m = _PPC_LINE.match(line)
            if not m:
                unknown.append(line)      # PPC worded another way: clicks unknown, so refuse
                continue
            ppc_clicks += int(m.group(1).replace(",", ""))
            ppc += _money(m.group(3))
            continue
        m = _DATED_LINE.match(line)
        if m:
            date_from, date_to = _short_date(m.group(1)), _short_date(m.group(2))
            description, charge = m.group(3).strip(), _money(m.group(4))
        else:
            u = _UNDATED_LINE.match(line)
            if not u or not lines:
                continue          # headings, totals, a wrapped description's second half
            date_from, date_to = lines[-1]["date_from"], lines[-1]["date_to"]
            description, charge = u.group(1).strip(), _money(u.group(2))
        slots = None
        if _BASE_ADDON.match(description) or _BASE_CREDIT.match(description):
            line_type = "base"
        elif (b := _BASE.match(description)):
            line_type, slots = "base", int(b.group(1))
        elif (f := _FINANCE.match(description)):
            line_type, slots = "finance", int(f.group(1))
        elif _TACTICAL_ADDON.match(description):
            line_type = "tactical"
        elif (t := _TACTICAL.match(description)):
            line_type, slots = "tactical", int(t.group(1))
        elif _CONNECT.match(description):
            line_type = "connect"
        elif charge == 0:
            continue              # a free extra (e.g. "3 Months Stock Boost 0.00"): no cost, no slots
        else:
            unknown.append(line)
            continue
        if date_to < date_from:
            raise ValueError(f"Line ends before it starts: {line}")
        lines.append({"line_type": line_type, "date_from": date_from, "date_to": date_to,
                      "slots": slots, "charge": charge, "description": description})

    if unknown:
        raise ValueError("Lines not recognised, so nothing was imported: " + " | ".join(unknown))
    if not lines:
        raise ValueError("No invoice lines found")
    expected = _money(subtotal.group(1)) - ppc
    found = sum((l["charge"] for l in lines), Decimal("0"))
    if found != expected:
        raise ValueError(f"The lines add up to {found:,.2f} but the subtotal excluding VAT minus PPC is "
                         f"{expected:,.2f}, so a line was missed or misread. Nothing was imported.")
    return {"invoice_number": number.group(1),
            "issued": datetime.datetime.strptime(issued.group(1), "%d/%m/%Y").date(),
            "subtotal": _money(subtotal.group(1)), "ppc": ppc, "ppc_clicks": ppc_clicks, "lines": lines,
            "dealer_id": (_DEALER.search(text) or [None, None])[1]}


# ── Aurora (prod pricer cluster, alongside kpi_layout) ──────────────────────

def _ensure_tables(cur) -> None:
    cur.execute("""
        CREATE TABLE IF NOT EXISTS at_invoices (
            invoice_number VARCHAR(30) PRIMARY KEY,
            issued DATE NOT NULL,
            subtotal NUMERIC(12, 2) NOT NULL,
            ppc NUMERIC(12, 2) NOT NULL,
            s3_key TEXT,
            imported_at TIMESTAMP NOT NULL,
            imported_by VARCHAR(50)
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS at_invoice_lines (
            id SERIAL PRIMARY KEY,
            invoice_number VARCHAR(30) NOT NULL REFERENCES at_invoices (invoice_number) ON DELETE CASCADE,
            line_type VARCHAR(10) NOT NULL,
            date_from DATE NOT NULL,
            date_to DATE NOT NULL,
            slots INTEGER,
            charge NUMERIC(12, 2) NOT NULL,
            description TEXT NOT NULL
        )""")
    # Added after the first imports: NULL on those until they are imported again.
    cur.execute("ALTER TABLE at_invoices ADD COLUMN IF NOT EXISTS ppc_clicks INTEGER")
    cur.execute("ALTER TABLE at_invoices ADD COLUMN IF NOT EXISTS dealer_id VARCHAR(20)")


def _overlap_message(cur, invoice: dict) -> str | None:
    """Another invoice for the SAME dealer already charging the same kind of
    line on the same day would be counted twice (e.g. a re-issued invoice
    with a new number). Invoices for other dealers (the extra invoices, e.g.
    Sheffield) are meant to overlap: their costs are added together."""
    for l in invoice["lines"]:
        cur.execute("""
            SELECT l.invoice_number, l.date_from, l.date_to FROM at_invoice_lines l
            JOIN at_invoices i ON i.invoice_number = l.invoice_number
            WHERE l.invoice_number <> %s AND l.line_type = %s AND l.date_from <= %s AND l.date_to >= %s
              AND i.dealer_id IS NOT DISTINCT FROM %s
            LIMIT 1""", (invoice["invoice_number"], l["line_type"], l["date_to"], l["date_from"],
                         invoice["dealer_id"]))
        row = cur.fetchone()
        if row:
            return (f"Invoice {row[0]} already has a {l['line_type']} line for "
                    f"{row[1]:%d/%m/%Y} - {row[2]:%d/%m/%Y}. Delete that invoice first if this one replaces it.")
    return None


def save_invoice(conn, invoice: dict, s3_key: str | None, user: str, now: datetime.datetime) -> bool:
    """Store an invoice and its lines in one transaction. An invoice already
    stored under the same number is replaced, lines and all, so a line left
    out of the new import does not stay behind. Returns True if it replaced
    one. Raises ValueError if it overlaps a different invoice."""
    with conn.cursor() as cur:
        _ensure_tables(cur)
        overlap = _overlap_message(cur, invoice)
        if overlap:
            conn.rollback()
            raise ValueError(overlap)
        cur.execute("DELETE FROM at_invoices WHERE invoice_number = %s", (invoice["invoice_number"],))
        replaced = cur.rowcount > 0
        cur.execute("""
            INSERT INTO at_invoices (invoice_number, issued, subtotal, ppc, ppc_clicks, dealer_id, s3_key, imported_at, imported_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (invoice["invoice_number"], invoice["issued"], invoice["subtotal"], invoice["ppc"],
             invoice["ppc_clicks"], invoice["dealer_id"], s3_key, now, user))
        for l in invoice["lines"]:
            cur.execute("""
                INSERT INTO at_invoice_lines (invoice_number, line_type, date_from, date_to, slots, charge, description)
                VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (invoice["invoice_number"], l["line_type"], l["date_from"], l["date_to"],
                 l["slots"], l["charge"], l["description"]))
    conn.commit()
    return replaced


def delete_invoice(conn, invoice_number: str) -> bool:
    with conn.cursor() as cur:
        _ensure_tables(cur)
        cur.execute("DELETE FROM at_invoices WHERE invoice_number = %s", (invoice_number,))
        deleted = cur.rowcount > 0
    conn.commit()
    return deleted


def read_lines(conn) -> list:
    """Every stored invoice line: [{line_type, date_from, date_to, slots, charge}]."""
    with conn.cursor() as cur:
        _ensure_tables(cur)
        cur.execute("SELECT line_type, date_from, date_to, slots, charge FROM at_invoice_lines")
        rows = cur.fetchall()
    conn.commit()
    return [{"line_type": r[0], "date_from": r[1], "date_to": r[2], "slots": r[3], "charge": r[4]}
            for r in rows]


def list_invoices(conn) -> list:
    """One row per stored invoice, oldest first, for the Settings list."""
    with conn.cursor() as cur:
        _ensure_tables(cur)
        cur.execute("""
            SELECT i.invoice_number, i.issued, i.subtotal, i.ppc, i.s3_key, i.imported_at, i.imported_by,
                   MIN(l.date_from), MAX(l.date_to), COALESCE(SUM(l.charge), 0), i.ppc_clicks
            FROM at_invoices i LEFT JOIN at_invoice_lines l ON l.invoice_number = i.invoice_number
            GROUP BY i.invoice_number ORDER BY MIN(l.date_from), i.issued""")
        rows = cur.fetchall()
    conn.commit()
    return [{"invoice_number": r[0], "issued": r[1].isoformat(), "subtotal": float(r[2]), "ppc": float(r[3]),
             "has_pdf": bool(r[4]), "imported_at": r[5].isoformat(timespec="minutes"), "imported_by": r[6],
             "date_from": r[7].isoformat() if r[7] else None, "date_to": r[8].isoformat() if r[8] else None,
             "lines_total": float(r[9]), "ppc_clicks": r[10]}
            for r in rows]


def ppc_rows(conn) -> tuple:
    """PPC Cost rows from the invoices: one per invoiced month, from the 1st,
    at that month's cost per click = PPC cost / clicks. PPC lines carry no
    dates, and one invoice can have two prices (a lower one past a number of
    clicks), so every day of the month gets the month's overall price; clicks
    x this price then adds back to the invoiced PPC for the month.
    The month is the one the invoice's dated lines are for (an invoice
    issued on 1 October is for September). Returns (rows, invoices still
    to be imported again because they were stored before clicks were read)."""
    rows, missing = {}, []
    for inv in list_invoices(conn):
        if inv["ppc"] and inv["ppc_clicks"] is None:
            missing.append(inv["invoice_number"])
            continue
        if not inv["ppc_clicks"]:
            continue
        if inv["date_from"]:
            month = inv["date_from"][:7] + "-01"
        else:
            issued = datetime.date.fromisoformat(inv["issued"])
            month = (issued.replace(day=1) - datetime.timedelta(days=1)).replace(day=1).isoformat()
        cost, clicks = rows.get(month, (0.0, 0))
        rows[month] = (cost + inv["ppc"], clicks + inv["ppc_clicks"])
    # 10 decimals: clicks x price must add back to the invoiced PPC to the penny.
    return ([{"from": m, "ppc_view_cost": round(cost / clicks, 10)} for m, (cost, clicks) in sorted(rows.items())],
            missing)


# ── Per-day figures ──────────────────────────────────────────────────────────

def daily_figures(lines: list) -> dict:
    """{date: {base, tactical, cost_base, cost_finance, cost_tactical, cost_connect}}
    for every day an invoice line covers. Each line's charge is spread evenly
    over its own days (both ends included). Base and tactical slots on a day
    are the slots of the lines covering it; finance is charged on the same
    adverts as the base package, so it adds cost but no slots."""
    out: dict = {}
    for l in lines:
        days = (l["date_to"] - l["date_from"]).days + 1
        per_day = float(l["charge"]) / days
        for i in range(days):
            d = l["date_from"] + datetime.timedelta(days=i)
            day = out.setdefault(d, {"base": 0, "tactical": 0, "cost_base": 0.0, "cost_finance": 0.0,
                                     "cost_tactical": 0.0, "cost_connect": 0.0})
            day["cost_" + l["line_type"]] += per_day
            if l["line_type"] in ("base", "tactical"):
                day[l["line_type"]] += l["slots"] or 0
    return out


def rows_from_lines(lines: list) -> list:
    """Settings rows from the invoice lines: one row for every run of days
    with the same slots and the same daily costs, with its own From and To.
    Costs are the invoiced cost for the row's own days (From to To), not per
    month, so they match the invoice lines. A run never crosses a month end,
    so each month's total can be read off the rows. Not rounded: the page
    shows 2 decimals and the charts round only what they display."""
    rows = []
    costs = ("cost_base", "cost_finance", "cost_tactical", "cost_connect")
    fields = ("me_cost", "finance_cost", "tactical_cost", "connect_cost")
    for d, f in sorted(daily_figures(lines).items()):
        # Rounded only to compare days: float sums of the same lines can
        # differ in the last digit.
        per_day = tuple(round(f[k], 6) for k in costs)
        key = (d.year, d.month, f["base"], f["tactical"], per_day)
        last = rows[-1] if rows else None
        if last and last["_key"] == key and last["to"] == (d - datetime.timedelta(days=1)):
            last["to"] = d
            for c, field in zip(costs, fields):
                last[field] += f[c]
            continue
        rows.append({"_key": key, "from": d, "to": d, "base": f["base"], "flex_pct": INVOICE_FLEX_PCT,
                     "tactical": f["tactical"], **{field: f[c] for c, field in zip(costs, fields)}})
    return [{**{k: v for k, v in r.items() if k != "_key"},
             "from": r["from"].isoformat(), "to": r["to"].isoformat(), "source": "invoice"}
            for r in rows]


def coverage(lines: list) -> dict:
    """First and last invoiced day, and the gaps between them with no line."""
    days = sorted(daily_figures(lines))
    if not days:
        return {"from": None, "to": None, "gaps": []}
    gaps = []
    for prev, nxt in zip(days, days[1:]):
        if (nxt - prev).days > 1:
            gaps.append([(prev + datetime.timedelta(days=1)).isoformat(),
                         (nxt - datetime.timedelta(days=1)).isoformat()])
    return {"from": days[0].isoformat(), "to": days[-1].isoformat(), "gaps": gaps}
