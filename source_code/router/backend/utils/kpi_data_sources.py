"""KPI page data sources (Settings > Data Sources): Snowflake tables added on
the page, each read as chart metrics.

Two shapes of table:

  wide - one row per date, every number column is a metric
         (like product_slt_kpis.daily_summary)
  long - one row per date per name, e.g. ga4_daily_events: the split column
         (event_name) gives the metric names and the value column the figures

Each entry is stored in kpi_layout under 'data_sources':
    {id, name, table: 'SCHEMA.TABLE', shape, date_column,
     split_column, value_column}   (the last two for long tables only)

The id is made once, when the source is added, and never changes, so a
rename keeps every chart and stored target that uses it. Charts store it as
their 'source', the same place 'snowflake' / 'sharepoint' / 'autotrader' go.

Nothing here talks to Snowflake or the database; kpi.py does that. Table and
column names are checked against INFORMATION_SCHEMA there before they are put
into SQL; dates and metric names are always bound as parameters.
"""

import datetime
import re

SOURCE_ID = r"ds_[a-z0-9_]{1,40}"
IDENTIFIER = r"[A-Z][A-Z0-9_$]*"
SHAPES = ("wide", "long")
SOURCES_MAX = 30
NAME_MAX = 60
# Tables offered: the compute and product layers only, never raw tables.
SCHEMA_PATTERNS = ("COMPUTE!_%", "PRODUCT!_%")
DATE_TYPES = {"DATE", "TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ", "TIMESTAMP"}
NUMBER_TYPES = {"NUMBER", "FLOAT", "DECIMAL", "NUMERIC", "INT", "INTEGER", "BIGINT", "DOUBLE", "REAL"}
_FIELDS = {"id", "name", "table", "shape", "date_column", "split_column", "value_column"}


def is_source_id(source) -> bool:
    return isinstance(source, str) and re.fullmatch(SOURCE_ID, source) is not None


def new_id(name: str, taken) -> str:
    """An id from the name, e.g. 'GA4 Events' -> 'ds_ga4_events', with a
    number added if that one is already used."""
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:30] or "source"
    base = f"ds_{slug}"
    sid, n = base, 2
    while sid in taken:
        sid, n = f"{base}_{n}", n + 1
    return sid


def validate_entry(entry, columns: dict, others: list) -> str | None:
    """Error message for one entry, or None. columns: {COLUMN: data_type} of
    its table as read from INFORMATION_SCHEMA ({} when the table was not
    found). others: the other saved entries, for the repeated-name check."""
    if not isinstance(entry, dict) or set(entry) - _FIELDS:
        return "bad data source"
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > NAME_MAX:
        return f"Data source names must be 1 to {NAME_MAX} characters"
    if any(o.get("name", "").strip().lower() == name.strip().lower() for o in others):
        return f"There is already a data source called {name.strip()}"
    table = entry.get("table")
    if not isinstance(table, str) or not re.fullmatch(rf"{IDENTIFIER}\.{IDENTIFIER}", table):
        return "Choose a table"
    if not columns:
        return f"Table {table} was not found"
    shape = entry.get("shape")
    if shape not in SHAPES:
        return "Choose whether the table is one row per date or one row per date per name"
    date_column = entry.get("date_column")
    if date_column not in columns or columns[date_column] not in DATE_TYPES:
        return "Choose the table's date column"
    if shape == "wide":
        if entry.get("split_column") or entry.get("value_column"):
            return "A one-row-per-date table has no name or value column"
        if not wide_metric_columns(entry, columns):
            return f"{table} has no number columns to chart"
        return None
    split_column, value_column = entry.get("split_column"), entry.get("value_column")
    if split_column not in columns or split_column == date_column:
        return "Choose the column holding the metric names (e.g. EVENT_NAME)"
    if value_column not in columns or columns[value_column] not in NUMBER_TYPES:
        return "Choose the number column holding the figures (e.g. EVENT_VOLUME)"
    if value_column in (date_column, split_column):
        return "The value column must be different from the date and name columns"
    return None


def clean_entry(entry: dict) -> dict:
    """The entry as stored: name trimmed, the long-only fields dropped from a
    wide table."""
    out = {"id": entry["id"], "name": entry["name"].strip(), "table": entry["table"],
           "shape": entry["shape"], "date_column": entry["date_column"]}
    if entry["shape"] == "long":
        out["split_column"] = entry["split_column"]
        out["value_column"] = entry["value_column"]
    return out


def wide_metric_columns(entry: dict, columns: dict) -> list:
    """The number columns of a wide table, other than its date column."""
    return [c for c, t in columns.items() if t in NUMBER_TYPES and c != entry.get("date_column")]


BUILT_IN = (
    ("snowflake", "Daily Summary", "PRODUCT_SLT_KPIS.DAILY_SUMMARY"),
    ("sharepoint", "SharePoint KPI Spreadsheets", "Every KPI spreadsheet in Settings > SharePoint, read as one source"),
    ("autotrader", "Autotrader Costs", "Worked out from Settings > Autotrader Costs"),
)


def built_in_sources(metric_counts: dict, uses: dict, sp_files: list) -> list:
    """The three sources built into the KPI page, for Settings > Data Sources.

    metric_counts / uses: {source id: count}. sp_files: the KPI spreadsheets
    as {name, enabled}. Charts only store 'sharepoint', not which file a
    metric came from, so the counts belong to the one SharePoint source and
    the files are listed under it without counts of their own."""
    out = []
    for source_id, name, detail in BUILT_IN:
        entry = {"id": source_id, "name": name, "detail": detail,
                 "metrics": int(metric_counts.get(source_id) or 0), "uses": int(uses.get(source_id) or 0)}
        if source_id == "sharepoint":
            entry["files"] = [{"name": str(f.get("name") or ""), "enabled": bool(f.get("enabled"))} for f in sp_files]
        out.append(entry)
    return out


def references(obj, source_id: str) -> int:
    """How many places in a saved layout (or any part of one) use the source:
    any dict whose 'source' is the id — a chart, a stacked series, an added,
    divide-by or multiply-by metric, or a calculated target's reference.
    A metric saved with no source is daily_summary ('snowflake'), as the
    layout check reads it."""
    if isinstance(obj, dict):
        source = obj.get("source", "snowflake" if "metric" in obj else None)
        own = 1 if source == source_id else 0
        return own + sum(references(v, source_id) for v in obj.values())
    if isinstance(obj, list):
        return sum(references(v, source_id) for v in obj)
    return 0


def fetch_sql(entry: dict, metrics: list, start, end) -> tuple[str, list]:
    """SQL and bound values reading `metrics` for one entry from start to end.
    The table and column names must already have been checked against the
    live table; metrics of a wide table likewise (they are column names).

    Wide: the date and the chosen columns. Long: every date the table holds
    in the range, left-joined to the summed values of the chosen names, so a
    date the table has but a name has no row for comes back as that date with
    no name (worked out as 0 in rows_to_by_date)."""
    t, d = entry["table"], entry["date_column"]
    if entry["shape"] == "wide":
        cols = ", ".join(dict.fromkeys(metrics))
        return (f"SELECT {d}::DATE AS D, {cols} FROM {t} "
                f"WHERE {d}::DATE >= %s AND {d}::DATE <= %s ORDER BY 1"), [str(start), str(end)]
    s, v = entry["split_column"], entry["value_column"]
    names = list(dict.fromkeys(metrics))
    placeholders = ", ".join(["%s"] * len(names))
    sql = (f"WITH days AS (SELECT DISTINCT {d}::DATE AS D FROM {t} "
           f"WHERE {d}::DATE >= %s AND {d}::DATE <= %s), "
           f"vals AS (SELECT {d}::DATE AS D, {s}::VARCHAR AS S, SUM({v}) AS V FROM {t} "
           f"WHERE {d}::DATE >= %s AND {d}::DATE <= %s AND {s}::VARCHAR IN ({placeholders}) "
           f"GROUP BY 1, 2) "
           f"SELECT days.D, vals.S, vals.V FROM days LEFT JOIN vals ON vals.D = days.D ORDER BY 1")
    return sql, [str(start), str(end), str(start), str(end), *names]


def _as_date(value) -> datetime.date:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value)[:10])


def rows_to_by_date(entry: dict, metrics: list, rows, names) -> dict:
    """{date: {metric: value}} from fetch_sql's result.

    Long tables: on any date the table holds, a chosen name with no row is 0,
    because GA4 only writes a row for an event that happened. A date missing
    from the whole table is left out, so the chart shows a gap there rather
    than a false 0."""
    out = {}
    if entry["shape"] == "wide":
        names = [str(n).upper() for n in names]
        for r in rows:
            row = dict(zip(names, r))
            out[_as_date(row.pop("D"))] = row
        return out
    for d, split, value in rows:
        day = out.setdefault(_as_date(d), {m: 0 for m in metrics})
        if split is not None:
            day[str(split)] = value if value is not None else 0
    return out
