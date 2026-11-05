"""Weekly Business Review > Report view: reading its data.

Accounts come from Snowflake (finance's standard_costs sheet, loaded into
SOURCE_FINANCE.MANAGEMENT_ACCOUNTS). Recon and volume come from the SharePoint
copies in Settings > SharePoint. The functions that turn rows into lines are
pure, so they can be tested without a connection.
"""
import calendar
import datetime
import gzip
import json
import logging
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from .budgets import parse_number

logger = logging.getLogger(__name__)

RECON_KEY = "wbr_recon"
VOLUME_KEY = "wbr_volume"

_ACCOUNTS_SQL = """
    SELECT cost_type, month_start, cost_amount
    FROM PROD_CARSUPERMARKET_DB.SOURCE_FINANCE.MANAGEMENT_ACCOUNTS
    WHERE cost_type IN ({})
"""

# Report line -> cost_type. Kept in code because each line's meaning is fixed by
# the report's formulas. The two IFRS names are swapped on purpose: only this way
# round do the review's EBITDA and EBIT formulas give finance's figures.
ACCOUNT_LINES = {
    "Net Deliveries B2C": "b2c_units",
    "GPU-M (£/unit)": "gpu_m",
    "GPU-S (£/unit)": "gpu_s",
    "GPU-B (£/unit)": "gpu_b",
    "Revenue k£": "revenue",
    "COCA k£": "marketing_costs",
    "Sales costs k£": "sga_costs",
    "HQ costs k£": "hq_costs",
    "Depreciation k£": "depreciation",
    "IFRS impacts to EBIT k£": "ifrs_16_ebitda",
    "IFRS impacts to EBIT/EBITDA k£": "ifrs_16_ebit",
    "Interest & Tax k£": "interest_tax",
    "EBIT k£": "ebit",
    "EBITDA k£": "ebitda",
}


def add_months(month: str, count: int) -> str:
    """'2026-10' moved by count months."""
    total = int(month[:4]) * 12 + int(month[5:7]) - 1 + count
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _norm(value) -> str:
    return " ".join(str(value if value is not None else "").split()).lower()


def _number(value):
    """A cell as a number, or None when it is blank or not a number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return parse_number(str(value or ""))[0]
    except ValueError:
        return None


def _date(value):
    """A cell's ISO date, or None when it is not a date."""
    try:
        return datetime.date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


# ── Accounts ─────────────────────────────────────────────────────────────────

def _ratio(top, bottom, scale=1.0):
    return top / bottom * scale if top is not None and bottom else None


def _worked_out(line: dict, n1_units) -> dict:
    """The lines the table does not hold, from those it does."""
    units = line.get("Net Deliveries B2C")
    per_unit = {name: line.get(f"{name} (£/unit)") for name in ("GPU-M", "GPU-S", "GPU-B")}
    totals = {name: value * units / 1000 if value is not None and units is not None else None
              for name, value in per_unit.items()}
    have_all = all(v is not None for v in per_unit.values())
    return {
        "GPU-M k£": totals["GPU-M"],
        "GPU-S k£": totals["GPU-S"],
        "GPU-B k£": totals["GPU-B"],
        "GPU-MSB k£": sum(totals.values()) if have_all and units is not None else None,
        "GPU-MS (£/unit)": per_unit["GPU-M"] + per_unit["GPU-S"] if have_all else None,
        "GPU-MSB (£/unit)": sum(per_unit.values()) if have_all else None,
        "Revenue (£/unit)": _ratio(line.get("Revenue k£"), units, 1000),
        "COCA £/u": _ratio(line.get("COCA k£"), units, 1000),
        "EBITDA %": _ratio(line.get("EBITDA k£"), line.get("Revenue k£")),
        "YoY growth B2C %": units / n1_units - 1 if units is not None and n1_units else None,
    }


def accounts_lines(rows) -> dict:
    """{month: {report line: value}} from (cost_type, month_start, cost_amount) rows."""
    by_type = {cost_type: line for line, cost_type in ACCOUNT_LINES.items()}
    months: dict = {}
    for cost_type, month_start, amount in rows:
        line = by_type.get(cost_type)
        if line:
            months.setdefault(str(month_start)[:7], {})[line] = float(amount) if amount is not None else None
    return {month: {**line, **_worked_out(line, months.get(add_months(month, -12), {}).get("Net Deliveries B2C"))}
            for month, line in months.items()}


def _query(sql: str, params: list) -> tuple:
    from utils.snowflake_client import SnowflakeClient
    with SnowflakeClient() as sf:
        return sf.execute(sql, params)


def _query_accounts() -> dict:
    types = list(ACCOUNT_LINES.values())
    rows, _ = _query(_ACCOUNTS_SQL.format(", ".join(["%s"] * len(types))), types)
    return accounts_lines(rows)


def load_accounts(refresh: bool = False) -> dict:
    return _cached("accounts", _query_accounts, refresh=refresh)


# ── Cache ────────────────────────────────────────────────────────────────────
# Every section reads the same data, so it is fetched once an hour and kept in S3.
# Memory on Lambda belongs to one instance, so each read asks S3 which version is
# current: a Reload deletes the object, and every instance then sees it has gone.

_CACHE_BUCKET = "aa-raw-sources-uk"
_CACHE_PREFIX = "applications/pricing/uk-vehicle-list-evaluator/shared/cache/wbr_report/"
_CACHE_SECONDS = 3600
_memory: dict = {}   # S3 key -> (etag, value)


def _s3():
    import boto3
    # Its own session each time: this runs on several threads at once.
    return boto3.session.Session().client("s3", region_name="eu-west-1")


def _cache_key(name: str) -> str:
    return f"{_CACHE_PREFIX}{name}.json.gz"


def _frame_to_body(frame: pd.DataFrame) -> dict:
    clean = frame.astype(object).where(frame.notna(), None)
    return {"columns": list(frame.columns), "rows": clean.values.tolist()}


def _frame_from_body(body: dict) -> pd.DataFrame:
    return pd.DataFrame(body["rows"], columns=body["columns"])


def _cached(name: str, build, refresh: bool = False, to_body=None, from_body=None):
    """The value kept in S3 if it is under an hour old, else build it and keep it.
    Trouble with S3 is logged, not raised: the page then just fetches the data."""
    key = _cache_key(name)
    to_body, from_body = to_body or (lambda v: v), from_body or (lambda v: v)
    s3 = None
    try:
        s3 = _s3()
        if refresh:
            s3.delete_object(Bucket=_CACHE_BUCKET, Key=key)
        else:
            head = s3.head_object(Bucket=_CACHE_BUCKET, Key=key)
            age = (datetime.datetime.now(datetime.timezone.utc) - head["LastModified"]).total_seconds()
            if age < _CACHE_SECONDS:
                held = _memory.get(key)
                if held and held[0] == head["ETag"]:
                    return held[1]
                got = s3.get_object(Bucket=_CACHE_BUCKET, Key=key)
                value = from_body(json.loads(gzip.decompress(got["Body"].read())))
                _memory[key] = (got["ETag"], value)
                return value
    except Exception as e:
        if "404" not in str(e) and "NoSuchKey" not in str(e):
            logger.warning("Weekly Business Review report cache read of %s failed: %s", name, e)
    value = build()
    try:
        if s3 is not None:
            body = gzip.compress(json.dumps(to_body(value), default=str).encode(), compresslevel=1)
            put = s3.put_object(Bucket=_CACHE_BUCKET, Key=key, Body=body, ContentType="application/json")
            _memory[key] = (put["ETag"], value)
    except Exception as e:
        logger.warning("Weekly Business Review report cache save of %s failed: %s", name, e)
    return value


# ── Margin (replaces the Sheet's data_margin tab) ────────────────────────────
# GPU-M, GPU-S, GPU-MS and GPU-MSB are the columns in WEEKLY_BUSINESS_REVIEW, the same
# figures as the KPI charts. Apex holds a car's costs as one number, so the split into
# recon and other comes from AutoView. Where the two do not match, costs_unmatched
# holds the difference, so the breakdown still adds up to GPU-M.

_MARGIN_TEXT = ["invoice_status", "order_acknowledged_month", "order_acknowledged_date",
                "invoice_posted_month", "invoice_posted_date", "supplier_name", "supplier_channel",
                "finance_company", "deal_site_clean", "payment_type"]
_STD_GPU_M = ["std_prep_centre_rent", "std_transport_salaries", "std_external_transport_costs",
              "std_delivery_sale", "std_late_costs", "std_damage_claims_rebates_other",
              "std_credited_deals_carried_over", "std_subcontracted_valet_costs", "std_write_downs"]
_STD_GPU_S = ["std_finance_debit_backs", "std_product_debit_backs", "std_service_dept_efficiency",
              "std_rfl_v5_costs"]
_MARGIN_NUMBERS = ["dis", "delivery_volume", "rate_volume", "sale_price_ex_vat", "purchase_price_ex_vat",
                   "discount_ex_vat", "margin_vat_cost", "finance_subsidy", "product_margin_admin_fee",
                   "chassis_margin_after_subsidy", "recon_cost", "other_costs", "finance_commission",
                   "product_margin_warranty", "product_margin_gap_rti_insurance",
                   "product_margin_premia_gap_rti_insurance", "product_margin_smart_plus_insurance",
                   "product_margin_service_mot_plan", "product_margin_services_mots_carried_over",
                   "product_margin_ceramic_vehicle_protection", "product_margin_anti_bacterial_protection",
                   "product_margin_ev_charger", "product_margin_mats", "total_product_margin",
                   *_STD_GPU_M, *_STD_GPU_S, "gpu_m", "gpu_s", "gpu_ms", "gpu_b", "gpu_msb"]
_MARGIN_SQL = f"""
    SELECT {', '.join(_MARGIN_TEXT + _MARGIN_NUMBERS)}
    FROM PROD_CARSUPERMARKET_DB.PRODUCT_SALES_PERFORMANCE.WEEKLY_BUSINESS_REVIEW
    WHERE order_acknowledged_date <= %s
"""
_INVOICE_DATES = ["invoice_posted_date", "invoice_posted_month"]


def _week_end(dates: pd.Series) -> pd.Series:
    """The Sunday of each date's Monday to Sunday week, as ISO text. Rows go into
    weeks by this, not by dbt's week keys (see wbr_report._week)."""
    parsed = pd.to_datetime(dates, errors="coerce")
    sunday = parsed + pd.to_timedelta(6 - parsed.dt.weekday, unit="D")
    return sunday.dt.strftime("%Y-%m-%d").astype(object).where(sunday.notna(), None)


def _total(frame: pd.DataFrame, columns: list) -> pd.Series:
    """Add columns across, ignoring blanks."""
    return frame[columns].sum(axis=1, min_count=1)


def margin_frame(frame: pd.DataFrame, cutoff: str) -> pd.DataFrame:
    """The sales rows the review uses. cutoff is the review's Sunday. An order placed by
    then but delivered after it is kept, as not yet delivered, so a run on
    Wednesday does not add Monday's and Tuesday's deliveries to the month."""
    df = frame.copy()
    for column in _MARGIN_NUMBERS:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df[df["purchase_price_ex_vat"].notna() & (df["purchase_price_ex_vat"] != 0)]
    df = df[df["order_acknowledged_date"].astype(str).str[:10] <= cutoff].copy()

    late = df["invoice_posted_date"].notna() & (df["invoice_posted_date"].astype(str).str[:10] > cutoff)
    df.loc[late, _INVOICE_DATES] = None
    df["deliveries"] = df["delivery_volume"].where(~late, 0).fillna(0).astype(int)

    df["recon_cost"] = -df["recon_cost"].fillna(0)
    df["other_costs"] = -df["other_costs"].fillna(0)
    for column in ("purchase_price_ex_vat", "discount_ex_vat", "margin_vat_cost", "finance_subsidy"):
        df[column] = -df[column]

    df["total_cos"] = (df["gpu_m"] - df["chassis_margin_after_subsidy"]).round(2)
    df["costs_unmatched"] = (df["total_cos"] - _total(df, ["recon_cost", "other_costs", *_STD_GPU_M])).round(2)
    df["gpu_s_cost"] = _total(df, _STD_GPU_S).round(2)
    df["volume_warranty"] = (df["product_margin_warranty"] > 0).astype(int)

    df["order_week_end"] = _week_end(df["order_acknowledged_date"])
    df["delivery_week_end"] = _week_end(df["invoice_posted_date"])
    return df.reset_index(drop=True)


def _with_lower_columns(rows, columns) -> pd.DataFrame:
    return pd.DataFrame([list(r) for r in rows], columns=[c.lower() for c in columns])


def load_margin(cutoff: str, refresh: bool = False) -> pd.DataFrame:
    def build():
        rows, columns = _query(_MARGIN_SQL, [cutoff])
        return margin_frame(_with_lower_columns(rows, columns), cutoff)
    return _cached(f"margin_{cutoff}", build, refresh, _frame_to_body, _frame_from_body)


def autoview_costs_usable(month: str, autoview_start: str) -> bool:
    """False for a month before AutoView data starts: its recon and other costs are blank."""
    return month >= autoview_start


# ── Purchases and daily summary ──────────────────────────────────────────────

_PURCHASES_SQL = """
    SELECT purchase_date, purchase_month, date_first_advertised, date_first_advertised_month,
           supplier_channel, gpu_m, sales_channel, days_to_sell_band
    FROM PROD_CARSUPERMARKET_DB.PRODUCT_VEHICLE_JOURNEY.PURCHASES
    WHERE (purchase_date BETWEEN %s AND %s) OR (date_first_advertised BETWEEN %s AND %s)
"""
_DAILY_SUMMARY_SQL = """
    SELECT date, avg_gpu_m_stock_on_sale, avg_gpu_m_b2b2c, avg_gpu_m_consignment,
           avg_gpu_m_c2b2c_px, avg_gpu_m_c2b2c_other
    FROM PROD_CARSUPERMARKET_DB.PRODUCT_SLT_KPIS.DAILY_SUMMARY
    WHERE date BETWEEN %s AND %s
"""


def purchases_frame(frame: pd.DataFrame) -> pd.DataFrame:
    df = frame.copy()
    df["gpu_m"] = pd.to_numeric(df["gpu_m"], errors="coerce")
    df["purchase_week_end"] = _week_end(df["purchase_date"])
    df["advertised_week_end"] = _week_end(df["date_first_advertised"])
    return df


def load_purchases(start: str, end: str, refresh: bool = False) -> pd.DataFrame:
    def build():
        rows, columns = _query(_PURCHASES_SQL, [start, end, start, end])
        return purchases_frame(_with_lower_columns(rows, columns))
    return _cached(f"purchases_{start}_{end}", build, refresh, _frame_to_body, _frame_from_body)


def load_daily_summary(start: str, end: str, refresh: bool = False) -> pd.DataFrame:
    def build():
        rows, columns = _query(_DAILY_SUMMARY_SQL, [start, end])
        return _with_lower_columns(rows, columns)
    return _cached(f"daily_summary_{start}_{end}", build, refresh, _frame_to_body, _frame_from_body)


# ── SharePoint copies ────────────────────────────────────────────────────────

def _copy_values(key: str) -> tuple:
    """(rows or None, warnings). Never waits for a download: one in progress
    shows as a warning, and the last copy is used when there is one."""
    from utils import sharepoint_sources as sps
    name = sps.get_source(key)["name"]
    copy = sps.get_copy(key)
    warnings = [copy.error] if copy.error else []
    if copy.values is None and not copy.error:
        warnings.append(f"{name} is still downloading. Reload shortly.")
    return copy.values, warnings


def source_statuses() -> list:
    """The two files' status for Settings > Report."""
    from utils import sharepoint_sources as sps
    out = []
    for key in (RECON_KEY, VOLUME_KEY):
        src = sps.get_source(key, fresh=True)
        out.append({"name": src["name"], "enabled": bool(src["enabled"]),
                    **{c: src.get(c) for c in ("last_saved_at", "last_checked_at", "last_row_count", "last_error")}})
    return out


# ── Recon ────────────────────────────────────────────────────────────────────

def recon_frame(values, headers: dict) -> tuple:
    """(rows, warnings) from the recon copy. headers is {field: [header texts]}.
    Columns are found by header name, so they can move in the file."""
    if not values:
        return [], []
    head = [_norm(h) for h in values[0]]
    columns, warnings = {}, []
    for field, names in headers.items():
        wanted = {_norm(n) for n in names}
        found = next((i for i, h in enumerate(head) if h in wanted), None)
        if found is None:
            warnings.append(f"Recon column '{field}' was not found in the file.")
        else:
            columns[field] = found
    if "Date" not in columns:
        return [], warnings
    rows = []
    for row in values[1:]:
        day = _date(row[columns["Date"]])
        if not day:
            continue
        entry = {"date": day.isoformat(), "year_month": day.strftime("%Y-%m"),
                 "week_end": (day + datetime.timedelta(days=7 - day.isoweekday())).isoformat()}
        entry.update({field: _number(row[col]) for field, col in columns.items() if field != "Date"})
        rows.append(entry)
    return rows, warnings


def load_recon() -> tuple:
    from utils import sharepoint_sources as sps
    values, warnings = _copy_values(RECON_KEY)
    rows, more = recon_frame(values, sps.header_names(RECON_KEY))
    return rows, warnings + more


# ── Volume ───────────────────────────────────────────────────────────────────

_SECTIONS = {"retail (b2c)": "retail", "trade (b2b)": "trade"}
_MEASURES = {"stock in # of vehicles": "units", "sleeping stock (#)": "sleeping", "sleeping stock": "sleeping"}
# A retail stock total, and the block of rows above it that it closes.
_BLOCK_TOTALS = {"upstream stock total": "upstream", "upstream stock": "upstream",
                 "in prep total": "in_prep", "in prep": "in_prep",
                 "on sale stock total": "on_sale", "on sale stock": "on_sale"}
_NO_BLOCK = {"trade hold", "ordered"}
_LABEL_ALIASES = {"consigment": "consignment"}
_STOCK_GROUP = "stock new"


def volume_key(group: str, label: str, section: str = "", measure: str = "", block: str = "") -> tuple:
    """The key of a row. Several labels repeat in the file (B2B, PX, Goole ...),
    so the group, section, measure and block tell them apart."""
    clean = _norm(label)
    return (_norm(group), section, measure, block, _LABEL_ALIASES.get(clean, clean))


def volume_frame(values) -> tuple:
    """({"weeks": [ISO Sundays, oldest first], "rows": {key: {week: value}}}, warnings)
    from the Performance tab: one column per week ending Sunday, newest on the left."""
    empty = {"weeks": [], "rows": {}}
    if not values:
        return empty, []
    start = next((i for i, row in enumerate(values) if _norm(row[0]).startswith("weekly performance")), None)
    if start is None:
        return empty, ["The Weekly Performance header row was not found in the volume file."]
    weeks = {i: _date(cell).isoformat() for i, cell in enumerate(values[start]) if i >= 2 and _date(cell)}
    rows, warnings = {}, []

    def add(key, row):
        if key in rows:
            warnings.append(f"Volume row '{key[-1]}' appears twice in '{key[0]}'; the first is used.")
            return
        rows[key] = {week: _number(row[i]) for i, week in weeks.items() if i < len(row)}

    group = section = measure = ""
    pending = []
    for row in values[start + 1:]:
        if _norm(row[0]):
            group, section, measure = _norm(row[0]), "", ""
            pending = []
        label = _norm(row[1]) if len(row) > 1 else ""
        if not label:
            continue
        if group != _STOCK_GROUP:
            add(volume_key(group, label), row)
        elif label in _SECTIONS:
            section, measure = _SECTIONS[label], ""
            pending = []
        elif label in _MEASURES:
            measure = _MEASURES[label]
            pending = []
        elif section != "retail" or label in _NO_BLOCK:
            add(volume_key(group, label, section, measure), row)
        elif label in _BLOCK_TOTALS:
            block = _BLOCK_TOTALS[label]
            for held_label, held_row in pending + [(label, row)]:
                add(volume_key(group, held_label, section, measure, block), held_row)
            pending = []
        else:
            pending.append((label, row))
    return {"weeks": sorted(weeks.values()), "rows": rows}, warnings


def load_volume() -> tuple:
    values, warnings = _copy_values(VOLUME_KEY)
    frame, more = volume_frame(values)
    return frame, warnings + more


def month_end_proxy(weeks: list, month: str):
    """The week standing in for a month's end: the week ending within 4 days after
    the month's last day, else within 4 days before it. None if there is neither."""
    year, number = int(month[:4]), int(month[5:7])
    last = datetime.date(year, number, calendar.monthrange(year, number)[1])
    days = {week: (datetime.date.fromisoformat(week) - last).days for week in weeks}
    for week, gap in sorted(days.items()):
        if 0 <= gap <= 4:
            return week
    for week, gap in sorted(days.items(), reverse=True):
        if -4 <= gap < 0:
            return week
    return None


def month_end_weeks(weeks: list, month: str) -> list:
    """Every week ending within 4 days either side of a month's last day. A month can
    have two; the spreadsheet's N-1 stock figures average them."""
    year, number = int(month[:4]), int(month[5:7])
    last = datetime.date(year, number, calendar.monthrange(year, number)[1])
    return [week for week in weeks if abs((datetime.date.fromisoformat(week) - last).days) <= 4]


# ── Budget ───────────────────────────────────────────────────────────────────

def budget_lines(rows) -> dict:
    """{(section, label): {month: value}} from (section, label, is_percent, month, value)
    rows. Percent lines are stored as typed on the sheet (50.0), so they become 0.5 here."""
    lines: dict = {}
    for section, label, is_percent, month, value in rows:
        line = lines.setdefault((section, label), {})
        if month and value is not None:
            line[month] = float(value) / 100 if is_percent else float(value)
    return lines


def load_budget() -> dict:
    """Read from the PROD cluster, like the Budgets tab. Not kept in S3: an edit there
    should show at once, and this read is quick."""
    from utils.db_utils import get_prod_pricer_connection
    with get_prod_pricer_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT l.section, l.label, l.is_percent, v.month, v.value
                           FROM budget_lines l LEFT JOIN budget_values v ON v.line_id = l.id""")
            rows = cur.fetchall()
        conn.commit()
    return budget_lines(rows)


# ── What the report found ────────────────────────────────────────────────────

_RECON_DATE_FIELDS = {"date", "year_month", "week_end"}


def source_summary(meta: dict, accounts: dict, recon: list, volume: dict) -> tuple:
    """(what each source holds, warnings about what the review's dates need)."""
    warnings = []
    review, current = meta["update_date"], meta["current_month"]
    earliest_n1 = min(meta["n1_months"].values())
    accounts_months = sorted(accounts)
    # The file holds rows of zeros for dates still to come; those are not data.
    recon_dates = [r["date"] for r in recon if any(v for k, v in r.items() if k not in _RECON_DATE_FIELDS)]
    if accounts_months and current not in accounts:
        warnings.append(f"The accounts have no figures for {current}.")
    if recon_dates and max(recon_dates) < review:
        warnings.append(f"Recon data stops at {max(recon_dates)}, before the review date.")
    if recon_dates and min(recon_dates)[:7] > earliest_n1:
        warnings.append(f"Recon data starts at {min(recon_dates)}, so N-1 before then is blank.")
    if volume["weeks"] and review not in volume["weeks"]:
        warnings.append(f"Volume data is missing for the week ending {review}.")
    return {
        "accounts_months": [accounts_months[0], accounts_months[-1]] if accounts_months else [],
        "recon_dates": [min(recon_dates), max(recon_dates)] if recon_dates else [],
        "volume_weeks": [volume["weeks"][0], volume["weeks"][-1]] if volume["weeks"] else [],
    }, warnings


def snowflake_summary(meta: dict, margin: pd.DataFrame, purchases: pd.DataFrame, daily: pd.DataFrame) -> tuple:
    """(what the Snowflake tables returned, warnings). Catches the dbt models being
    back on their old, shorter windows, for example after a scheduled run."""
    warnings = []
    earliest_n1 = min(meta["n1_months"].values())
    out = {"margin_rows": len(margin), "purchases_rows": len(purchases), "daily_summary_rows": len(daily)}
    if len(margin):
        out["margin_max_order_date"] = str(margin["order_acknowledged_date"].max())[:10]
        if str(margin["order_acknowledged_month"].min()) > earliest_n1:
            warnings.append(f"Sales data only goes back to {margin['order_acknowledged_month'].min()}, "
                            "so earlier months and N-1 are incomplete.")
    if len(purchases) and str(purchases["purchase_month"].min()) > earliest_n1:
        warnings.append(f"Purchases data only goes back to {purchases['purchase_month'].min()}, "
                        "so earlier months and N-1 are incomplete.")
    return out, warnings


def add_sources(report: dict, refresh: bool = False) -> dict:
    """Read each source, add what was found to the report and return the data for
    the sections. A source that cannot be read becomes a warning, so the others still show."""
    meta = report["meta"]
    warnings = meta["warnings"]
    review = meta["update_date"]
    start = f"{min(meta['n1_months'].values())}-01"

    def read(label, reader, empty):
        try:
            return reader()
        except Exception as e:
            logger.error("Weekly Business Review report: %s read failed: %s", label, e)
            warnings.append(f"{label} could not be read: {e}")
            return empty

    # At the same time: one after another, a first load gets close to the API Gateway limit.
    with ThreadPoolExecutor(max_workers=7) as pool:
        jobs = [
            pool.submit(read, "Accounts", lambda: load_accounts(refresh), {}),
            pool.submit(read, "Sales data",
                        lambda: load_margin(review, refresh), pd.DataFrame()),
            pool.submit(read, "Purchases data", lambda: load_purchases(start, review, refresh), pd.DataFrame()),
            pool.submit(read, "Daily summary", lambda: load_daily_summary(start, review, refresh), pd.DataFrame()),
            pool.submit(read, "Recon data", load_recon, ([], [])),
            pool.submit(read, "Volume data", load_volume, ({"weeks": [], "rows": {}}, [])),
            pool.submit(read, "Budget", load_budget, {}),
        ]
    accounts, margin, purchases, daily, (recon, recon_warnings), (volume, volume_warnings), budget = [j.result() for j in jobs]
    meta["sources"], found = source_summary(meta, accounts, recon, volume)
    tables, table_warnings = snowflake_summary(meta, margin, purchases, daily)
    meta["sources"].update(tables)
    warnings.extend(recon_warnings + volume_warnings + found + table_warnings)
    return {"margin": margin, "purchases": purchases, "daily": daily, "recon": recon,
            "volume": volume, "accounts": accounts, "budget": budget}
