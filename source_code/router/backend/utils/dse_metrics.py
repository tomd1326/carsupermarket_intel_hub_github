"""DSE (Daily Spend & Enquiries) page: the saved rows and the month maths.

Each row of the page is one metric read from a Snowflake table chosen on the
page, stored in kpi_layout under 'dse_layout':

    {id, label, section, table, shape, date_column,
     metric_column,                            wide only: the number column shown
     split_column, metric_name, value_column,  long only: e.g. EVENT_NAME = 'x', SUM(EVENT_VOLUME)
     agg, fmt, decimals, shade}

The two shapes are the KPI data source shapes (utils/kpi_data_sources.py), so
the reading is done with the same checked SQL. `metric_column` is not called
`value_column`: there that name means the summed column of a long table, and
validate_entry rejects it on a wide one.

Nothing here talks to Snowflake or the database; routers/dse.py does that.
"""

import datetime
import re

from utils import kpi_data_sources as kds

ROW_ID = r"r_[a-z0-9]{1,20}"
ROWS_MAX = 200
LABEL_MAX = 80
SECTION_MAX = 60
NAME_MAX = 200
AGGS = ("sum", "avg")
FORMATS = ("num", "pct", "pct100", "gbp")
SHADES = ("none", "high-good", "high-bad")
COMPARE_MODES = ("same_days", "full_month")
_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")
_ROW_FIELDS = {"id", "label", "section", "table", "shape", "date_column", "metric_column",
               "split_column", "metric_name", "value_column", "agg", "fmt", "decimals", "shade"}
_LONG_ONLY = ("split_column", "metric_name", "value_column")

# Used until Settings is first saved. Sizes are px.
DEFAULT_SETTINGS = {
    "default_days": 30,
    "compare_mode": "same_days",
    "font_size": 12,
    "label_width": 230,
    "day_width": 52,
    "summary_width": 78,
    "row_height": 22,
    "weekend_bg": "#eef2f7",
    "section_bg": "#1e3a5f",
    "section_text": "#ffffff",
    "summary_bg": "#f8fafc",
}
_SETTING_RANGES = {
    "default_days": (1, 366),
    "font_size": (9, 20),
    "label_width": (100, 600),
    "day_width": (30, 160),
    "summary_width": (40, 200),
    "row_height": (16, 48),
}
_SETTING_COLOURS = ("weekend_bg", "section_bg", "section_text", "summary_bg")


def settings_with_defaults(saved) -> dict:
    out = dict(DEFAULT_SETTINGS)
    if isinstance(saved, dict):
        out.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
    return out


def validate_settings(settings) -> str | None:
    if not isinstance(settings, dict) or set(settings) - set(DEFAULT_SETTINGS):
        return "Bad settings"
    if settings.get("compare_mode", "same_days") not in COMPARE_MODES:
        return "Bad comparison setting"
    for key, (low, high) in _SETTING_RANGES.items():
        if key in settings:
            v = settings[key]
            if not isinstance(v, int) or isinstance(v, bool) or not low <= v <= high:
                return f"{key.replace('_', ' ').title()} must be a whole number from {low} to {high}"
    for key in _SETTING_COLOURS:
        if key in settings and not (isinstance(settings[key], str) and _COLOUR.fullmatch(settings[key])):
            return f"Bad colour: {settings[key]}"
    return None


def to_entry(row: dict) -> dict:
    """The row as a KPI data source entry, for kds.validate_entry / fetch_sql."""
    entry = {"name": row.get("label") or "", "table": row.get("table"),
             "shape": row.get("shape"), "date_column": row.get("date_column")}
    if row.get("shape") == "long":
        entry["split_column"] = row.get("split_column")
        entry["value_column"] = row.get("value_column")
    return entry


def group_key(row: dict) -> tuple:
    """Rows with the same key are read in one query."""
    if row["shape"] == "wide":
        return (row["table"], row["date_column"], "wide")
    return (row["table"], row["date_column"], "long", row["split_column"], row["value_column"])


def metric_key(row: dict) -> str:
    """The row's metric as fetch_sql and rows_to_by_date name it."""
    return row["metric_column"].upper() if row["shape"] == "wide" else row["metric_name"]


def validate_row_shape(row) -> str | None:
    """Checks that need no Snowflake: field names, label, formats."""
    if not isinstance(row, dict) or set(row) - _ROW_FIELDS:
        return "Bad row"
    if not isinstance(row.get("id"), str) or not re.fullmatch(ROW_ID, row["id"]):
        return "Bad row id"
    label = row.get("label")
    if not isinstance(label, str) or not label.strip() or len(label.strip()) > LABEL_MAX:
        return f"Row labels must be 1 to {LABEL_MAX} characters"
    section = row.get("section", "")
    if not isinstance(section, str) or len(section.strip()) > SECTION_MAX:
        return f"{label}: section names must be at most {SECTION_MAX} characters"
    if row.get("shape") not in kds.SHAPES:
        return f"{label}: choose whether the table is one row per date or one row per date per name"
    if row.get("agg") not in AGGS:
        return f"{label}: choose Sum or Average for the month columns"
    if row.get("fmt") not in FORMATS:
        return f"{label}: bad format"
    decimals = row.get("decimals")
    if not isinstance(decimals, int) or isinstance(decimals, bool) or not 0 <= decimals <= 4:
        return f"{label}: decimals must be 0 to 4"
    if row.get("shade", "none") not in SHADES:
        return f"{label}: bad colour scale"
    if row["shape"] == "wide":
        if any(row.get(f) for f in _LONG_ONLY):
            return f"{label}: a one-row-per-date table has no name or value column"
        if not isinstance(row.get("metric_column"), str) or not row["metric_column"]:
            return f"{label}: choose the column to show"
    else:
        if row.get("metric_column"):
            return f"{label}: a one-row-per-name table uses a name and a value column, not a metric column"
        name = row.get("metric_name")
        if not isinstance(name, str) or not name.strip() or len(name) > NAME_MAX:
            return f"{label}: choose the name to show"
    return None


def validate_row(row, columns: dict) -> str | None:
    """Every check, against the table's live {COLUMN: data_type}. Run on save
    and again before every read, as the table can change after saving."""
    err = validate_row_shape(row)
    if err:
        return err
    label = row["label"].strip()
    if not _allowed_table(row.get("table")):
        return f"{label}: choose a COMPUTE_ or PRODUCT_ table"
    err = kds.validate_entry(to_entry(row), columns, [])
    if err:
        return f"{label}: {err}"
    if row["shape"] == "wide":
        column = row["metric_column"]
        if column not in kds.wide_metric_columns(to_entry(row), columns):
            return f"{label}: {column} is not a number column of {row['table']}"
    return None


def _allowed_table(table) -> bool:
    return (isinstance(table, str) and re.fullmatch(rf"{kds.IDENTIFIER}\.{kds.IDENTIFIER}", table) is not None
            and table.split(".")[0].startswith(("COMPUTE_", "PRODUCT_")))


def clean_row(row: dict) -> dict:
    """The row as stored: text trimmed, the other shape's fields dropped."""
    out = {"id": row["id"], "label": row["label"].strip(), "section": (row.get("section") or "").strip(),
           "table": row["table"], "shape": row["shape"], "date_column": row["date_column"],
           "agg": row["agg"], "fmt": row["fmt"], "decimals": row["decimals"], "shade": row.get("shade", "none")}
    if row["shape"] == "wide":
        out["metric_column"] = row["metric_column"]
    else:
        out["split_column"] = row["split_column"]
        out["metric_name"] = row["metric_name"].strip()
        out["value_column"] = row["value_column"]
    return out


def validate_rows(rows) -> str | None:
    """List-level checks: count and repeated ids."""
    if not isinstance(rows, list) or len(rows) > ROWS_MAX:
        return f"At most {ROWS_MAX} rows"
    ids = [r.get("id") for r in rows if isinstance(r, dict)]
    if len(ids) != len(set(ids)):
        return "Two rows have the same id"
    return None


def duplicate_dates(rows, date_index: int = 0) -> list:
    """Dates a wide table returned more than once. rows_to_by_date would keep
    only the last, so these are shown as an error instead."""
    seen, dupes = set(), []
    for r in rows:
        d = kds._as_date(r[date_index])
        if d in seen and d not in dupes:
            dupes.append(d)
        seen.add(d)
    return dupes


# ── Date range maths ─────────────────────────────────────────────────────────

RANGE_DAYS_MAX = 366


def parse_range(start, end, default_days: int, yesterday: datetime.date) -> tuple:
    """(start, end, error). Dates are YYYY-MM-DD. With neither given: the last
    `default_days` days up to and including yesterday. With only one, the other
    is worked out from it the same way."""
    try:
        end_d = datetime.date.fromisoformat(end) if end else None
        start_d = datetime.date.fromisoformat(start) if start else None
    except ValueError:
        return None, None, "Dates must be written as YYYY-MM-DD"
    if end_d is None:
        end_d = yesterday if start_d is None else max(start_d, min(yesterday, start_d + datetime.timedelta(days=default_days - 1)))
    if start_d is None:
        start_d = end_d - datetime.timedelta(days=default_days - 1)
    if end_d < start_d:
        return None, None, "The end date is before the start date"
    if (end_d - start_d).days + 1 > RANGE_DAYS_MAX:
        return None, None, f"Choose at most {RANGE_DAYS_MAX} days"
    return start_d, end_d, None


def range_days(start: datetime.date, end: datetime.date) -> list:
    return [start + datetime.timedelta(days=i) for i in range((end - start).days + 1)]


def shift_year(d: datetime.date) -> datetime.date:
    """The same day a year earlier (29 Feb becomes 28 Feb)."""
    try:
        return d.replace(year=d.year - 1)
    except ValueError:
        return d.replace(year=d.year - 1, day=28)


def fetch_range(start: datetime.date, end: datetime.date) -> tuple:
    """Everything the page needs: from the earlier of the same days last year
    and the period just before, to the end of the range."""
    length = (end - start).days + 1
    return min(shift_year(start), start - datetime.timedelta(days=length)), end


def _number(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _agg(values: list, mode: str):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) if mode == "sum" else sum(vals) / len(vals)


def _daily_avg(values: list):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def row_values(daily: dict, start: datetime.date, end: datetime.date, agg: str, compare_mode: str) -> dict:
    """Per-day values for start..end plus the summary columns, for one row.

    daily: {date: value} covering the range, the period of the same length
    just before it, and the same days last year. The range runs to the row's
    last day with a value, so a row whose data ends yesterday and one that has
    today are each compared on their own days. 'same_days' compares that many
    days of the other periods; 'full_month' uses as many as the whole range.
    """
    day = datetime.timedelta(days=1)
    days = [_number(daily.get(d)) for d in range_days(start, end)]
    filled = [i for i, v in enumerate(days) if v is not None]
    upto = filled[-1] + 1 if filled else 0
    count = upto if compare_mode == "same_days" else len(days)
    prev = [_number(daily.get(start - (count - i) * day)) for i in range(count)] if upto else []
    ly_start = shift_year(start)
    last_year = [_number(daily.get(ly_start + i * day)) for i in range(count)] if upto else []
    current = days[:upto]
    total, ly_total = _agg(current, agg), _agg(last_year, agg)
    return {
        "days": days,
        "mtd": total,
        "mtd_avg": _daily_avg(current) if agg == "sum" else total,
        "prev_mtd": _agg(prev, agg),
        "prev_mtd_avg": _daily_avg(prev) if agg == "sum" else _agg(prev, agg),
        "ly_mtd": ly_total,
        "ly_compare": (total - ly_total) / abs(ly_total) * 100 if total is not None and ly_total else None,
        "days_counted": upto,
    }


