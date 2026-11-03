"""KPI page data sources (Settings > Data Sources): Snowflake tables added on
the page, each read as chart metrics.

Three shapes of table:

  wide    - one row per date, every number column is a metric
            (like product_slt_kpis.daily_summary)
  long    - one row per date per name, e.g. ga4_daily_events: the split column
            (event_name) gives the metric names and the value column the figures
  records - one row per record (e.g. one per sale): every number column can
            be charted as its Sum or Average per day, and the rows counted
            (see column_metrics; the metric names are 'col:<COLUMN>:sum',
            'col:<COLUMN>:avg' and 'rows:count'). An Average also carries its
            row count per day, so a week or month is the true average over
            every row, not the average of the daily averages. An optional
            split column (valuation_status) adds one metric per name, counting
            the rows with that name each day. It can also hold metrics of its
            own (kept under 'differences', the name they started with), each
            with filters on the rows it reads:
              count      - the number of rows, read as one metric '<name>'
              average    - a number column: read as two metrics,
                           '<name> - Total' and '<name> - Count', so a chart's
                           Divided By gives a true average for weeks and months
              difference - column A minus column B on each row, read as the
                           same two metrics (two dates give days or minutes)

Each entry is stored in kpi_layout under 'data_sources':
    {id, name, table: 'SCHEMA.TABLE', shape, date_column,
     split_column, value_column,   (split_column for long and records tables,
                                    value_column for long tables only)
     differences: [{name, kind, column_a, column_b,
                    filters: [{column, operator, value}]}]}   (records tables only)

A metric saved before 'kind' existed has none: it is a difference when it has
a column B, else an average (see metric_kind).

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
# The shapes a KPI data source can have. SHAPES stays the two the DSE page
# (utils/dse_metrics.py) accepts.
KPI_SHAPES = SHAPES + ("records",)
SOURCES_MAX = 30
# Most names a long or records table's name column may hold. Each name is a
# metric in every list on the page, so an id column picked by mistake (one
# name per row) would put thousands of options in each and freeze the page.
# GA4 events holds 260 (2026-10-29), so leave plenty of room above that.
NAMES_MAX = 1000
NAME_MAX = 60
# Tables offered: the compute and product layers only, never raw tables.
SCHEMA_PATTERNS = ("COMPUTE!_%", "PRODUCT!_%")
DATE_TYPES = {"DATE", "TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ", "TIMESTAMP"}
NUMBER_TYPES = {"NUMBER", "FLOAT", "DECIMAL", "NUMERIC", "INT", "INTEGER", "BIGINT", "DOUBLE", "REAL"}
_FIELDS = {"id", "name", "table", "shape", "date_column", "split_column", "value_column", "name_order", "differences"}
DIFFERENCES_MAX = 30
DIFFERENCE_NAME_MAX = 40
TOTAL_SUFFIX = " - Total"
COUNT_SUFFIX = " - Count"
METRIC_KINDS = ("count", "average", "difference")
# Column aggregates of a records table: 'col:<COLUMN>:sum' / 'col:<COLUMN>:avg'
# and the row count. An average's row count per day is sent back alongside it
# under the metric name plus WEIGHT_SUFFIX.
_COLUMN_METRIC = re.compile(rf"^col:({IDENTIFIER}):(sum|avg)$")
ROWS_COUNT = "rows:count"
WEIGHT_SUFFIX = "#w"
COLUMN_AGGS = ("sum", "avg")


def parse_column_metric(metric) -> tuple[str, str] | None:
    """('COLUMN', 'sum' | 'avg') for a column aggregate, ('*', 'count') for
    the row count, else None."""
    if metric == ROWS_COUNT:
        return "*", "count"
    m = _COLUMN_METRIC.fullmatch(metric) if isinstance(metric, str) else None
    return (m.group(1), m.group(2)) if m else None


def column_metric(column: str, agg: str) -> str:
    return ROWS_COUNT if agg == "count" else f"col:{column}:{agg}"


def column_metrics(entry: dict, columns: dict) -> list:
    """The column aggregates a records table offers: the row count, then each
    number column's Sum and Average."""
    if entry.get("shape") != "records":
        return []
    out = [ROWS_COUNT]
    for c, t in columns.items():
        if t in NUMBER_TYPES and c != entry.get("date_column"):
            out += [column_metric(c, agg) for agg in COLUMN_AGGS]
    return out


def metric_label(metric: str) -> str:
    """How a metric name reads: 'Sum Of PRICE', 'Average Of PRICE', 'Count Of
    Rows'; any other name as it is."""
    parsed = parse_column_metric(metric)
    if not parsed:
        return metric
    column, agg = parsed
    return {"sum": f"Sum Of {column}", "avg": f"Average Of {column}", "count": "Count Of Rows"}[agg]


def metric_kind(d: dict) -> str:
    """A records table metric's kind, worked out from its columns when it was
    saved before 'kind' existed."""
    kind = d.get("kind")
    if kind:
        return kind
    if not d.get("column_a"):
        return "count"
    return "difference" if d.get("column_b") else "average"


def order_names(names: list, order) -> list:
    """names in the saved order (Settings > Data Sources > Value Order): the
    ones in the order first, as ordered, then any others as they came."""
    if not order:
        return list(names)
    rank = {n: i for i, n in enumerate(order)}
    return sorted(names, key=lambda n: (n not in rank, rank.get(n, 0)))


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
    if shape not in KPI_SHAPES:
        return "Choose whether the table is one row per date, one row per date per name, or one row per record"
    date_column = entry.get("date_column")
    if date_column not in columns or columns[date_column] not in DATE_TYPES:
        return "Choose the table's date column"
    if entry.get("differences") and shape != "records":
        return "Only a one-row-per-record table can have differences"
    if shape == "wide":
        if entry.get("split_column") or entry.get("value_column"):
            return "A one-row-per-date table has no name or value column"
        if not wide_metric_columns(entry, columns):
            return f"{table} has no number columns to chart"
        return None
    split_column, value_column = entry.get("split_column"), entry.get("value_column")
    # A records table's name column only counts rows per name; its columns
    # can be charted without one (column_metrics).
    if shape == "records" and not split_column:
        if value_column:
            return "A one-row-per-record table has no value column: each row counts as one"
        return _validate_differences(entry.get("differences"), columns)
    if split_column not in columns or split_column == date_column:
        return "Choose the column holding the metric names (e.g. EVENT_NAME)"
    order = entry.get("name_order")
    if order is not None and (not isinstance(order, list) or len(order) > NAMES_MAX
                              or any(not isinstance(n, str) or len(n) > 200 for n in order)):
        return "bad value order"
    if shape == "records":
        if value_column:
            return "A one-row-per-record table has no value column: each row counts as one"
        return _validate_differences(entry.get("differences"), columns)
    if value_column not in columns or columns[value_column] not in NUMBER_TYPES:
        return "Choose the number column holding the figures (e.g. EVENT_VOLUME)"
    if value_column in (date_column, split_column):
        return "The value column must be different from the date and name columns"
    return None


def difference_kind(type_a, type_b) -> str | None:
    """What column A minus column B gives: 'days' for two dates, 'minutes' for
    two date columns where at least one has a time, 'number' for two numbers,
    None when they cannot be subtracted. With no column B, column A's own
    figure, which has to be a number."""
    if type_b is None:
        return "number" if type_a in NUMBER_TYPES else None
    if type_a in DATE_TYPES and type_b in DATE_TYPES:
        return "days" if type_a == "DATE" and type_b == "DATE" else "minutes"
    if type_a in NUMBER_TYPES and type_b in NUMBER_TYPES:
        return "number"
    return None


def _validate_differences(differences, columns: dict) -> str | None:
    # Here, not at the top: vehicle_metrics imports this module.
    from utils import vehicle_metrics as vm
    if differences is None:
        return None
    if not isinstance(differences, list) or len(differences) > DIFFERENCES_MAX:
        return f"At most {DIFFERENCES_MAX} differences"
    seen = set()
    for d in differences:
        if not isinstance(d, dict) or set(d) - {"name", "kind", "column_a", "column_b", "filters"}:
            return "bad metric"
        name = d.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > DIFFERENCE_NAME_MAX:
            return f"Metric names must be 1 to {DIFFERENCE_NAME_MAX} characters"
        name = name.strip()
        if name.lower() in seen:
            return f"There is already a metric called {name}"
        seen.add(name.lower())
        kind = metric_kind(d)
        if kind not in METRIC_KINDS:
            return f"{name}: bad metric kind"
        a, b = d.get("column_a") or "", d.get("column_b") or ""
        if kind == "count":
            if a or b:
                return f"{name}: a count of rows has no column"
        elif kind == "average":
            if a not in columns:
                return f"{name}: choose a column"
            if b:
                return f"{name}: an average of a column has no second column"
            if difference_kind(columns[a], None) is None:
                return f"{name}: {a} is not a number column. Choose a number column, or Difference for two dates"
        else:
            if a not in columns or b not in columns:
                return f"{name}: choose both columns"
            if a == b:
                return f"{name}: choose two different columns"
            if difference_kind(columns[a], columns[b]) is None:
                return f"{name}: {a} and {b} must both be dates or both be numbers"
        filters = d.get("filters", [])
        if not isinstance(filters, list) or len(filters) > vm.FILTERS_MAX:
            return f"{name}: at most {vm.FILTERS_MAX} filters"
        for f in filters:
            if not isinstance(f, dict) or set(f) != {"column", "operator", "value"} or f["operator"] not in vm.OPERATORS:
                return f"{name}: bad filter"
            if f["column"] not in columns:
                return f"{name}: choose a column for each filter"
            if not isinstance(f["value"], str) or len(f["value"]) > vm.VALUE_MAX:
                return f"{name}: filter values must be text of at most {vm.VALUE_MAX} characters"
            if f["operator"] not in vm.NO_VALUE_OPERATORS and not f["value"].strip():
                return f"{name}: enter a value for each filter"
    return None


def difference_metrics(entry: dict) -> list:
    """The metric names a source's own metrics add: a count's name as it is,
    an average's or difference's '<name> - Total' and '<name> - Count'."""
    out = []
    for d in entry.get("differences") or []:
        name = d["name"].strip()
        out += [name] if metric_kind(d) == "count" else [name + TOTAL_SUFFIX, name + COUNT_SUFFIX]
    return out


def count_metrics(entry: dict) -> list:
    """The names of a source's count-of-rows metrics: plain figures, offered
    in Edit Chart like any other metric."""
    return [d["name"].strip() for d in entry.get("differences") or [] if metric_kind(d) == "count"]


def difference_info(entry: dict, columns: dict) -> dict:
    """{total metric: {count metric, kind}}, so Edit Chart can fill in Divided
    By and the format when an average's or difference's total is chosen."""
    out = {}
    for d in entry.get("differences") or []:
        if metric_kind(d) == "count":
            continue
        name = d["name"].strip()
        out[name + TOTAL_SUFFIX] = {"count": name + COUNT_SUFFIX,
                                    "kind": difference_kind(columns.get(d["column_a"]), columns.get(d.get("column_b")))}
    return out


def metric_references(obj, source_id: str, metric: str) -> int:
    """How many places in a saved layout use this one metric of the source."""
    if isinstance(obj, dict):
        own = 1 if obj.get("source", "snowflake") == source_id and obj.get("metric") == metric else 0
        return own + sum(metric_references(v, source_id, metric) for v in obj.values())
    if isinstance(obj, list):
        return sum(metric_references(v, source_id, metric) for v in obj)
    return 0


def clean_entry(entry: dict) -> dict:
    """The entry as stored: name trimmed, the long-only fields dropped from a
    wide table."""
    out = {"id": entry["id"], "name": entry["name"].strip(), "table": entry["table"],
           "shape": entry["shape"], "date_column": entry["date_column"]}
    if entry["shape"] == "long" or (entry["shape"] == "records" and entry.get("split_column")):
        out["split_column"] = entry["split_column"]
        if entry.get("name_order"):
            out["name_order"] = list(dict.fromkeys(entry["name_order"]))
    if entry["shape"] == "long":
        out["value_column"] = entry["value_column"]
    if entry["shape"] == "records" and entry.get("differences"):
        from utils import vehicle_metrics as vm
        out["differences"] = [{"name": d["name"].strip(), "kind": metric_kind(d),
                               "column_a": d.get("column_a") or "", "column_b": d.get("column_b") or "",
                               "filters": [{"column": f["column"], "operator": f["operator"],
                                            "value": "" if f["operator"] in vm.NO_VALUE_OPERATORS else f["value"].strip()}
                                           for f in d.get("filters", [])]}
                              for d in entry["differences"]]
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


def sources_used(obj) -> set:
    """Every source a chart (or any part of one) reads its figures from, read
    the same way as references(). A calculated target's reference is left
    out: it only sets the target line, not the chart's figures."""
    if isinstance(obj, dict):
        source = obj.get("source", "snowflake" if "metric" in obj else None)
        own = {source} if source else set()
        return own.union(*(sources_used(v) for k, v in obj.items() if k != "target_calc"))
    if isinstance(obj, list):
        return set().union(*(sources_used(v) for v in obj))
    return set()


def _difference_selects(entry: dict, wanted: set, columns: dict, start, end) -> tuple[list, list]:
    """(SELECTs, bound values) giving each wanted difference metric per date.
    Rows missing either column are left out of both the total and the count."""
    from utils import vehicle_metrics as vm
    t, d = entry["table"], entry["date_column"]
    selects, params = [], []
    for diff in entry.get("differences") or []:
        filters, filter_params = vm.filter_sql(diff.get("filters", []))
        where = " AND ".join([f"{d}::DATE >= %s", f"{d}::DATE <= %s"] + filters)
        name = diff["name"].strip()
        if metric_kind(diff) == "count":
            if name in wanted:
                selects.append(f"SELECT {d}::DATE AS D, %s AS S, COUNT(*) AS V FROM {t} WHERE {where} GROUP BY 1")
                params += [name, str(start), str(end), *filter_params]
            continue
        a, b = diff["column_a"], diff.get("column_b")
        kind = difference_kind(columns[a], columns[b] if b else None)
        value = a if not b else f"({a} - {b})" if kind == "number" else f"DATEDIFF({'DAY' if kind == 'days' else 'MINUTE'}, {b}, {a})"
        for suffix, total in ((TOTAL_SUFFIX, "SUM"), (COUNT_SUFFIX, "COUNT")):
            metric = name + suffix
            if metric in wanted:
                selects.append(f"SELECT {d}::DATE AS D, %s AS S, {total}({value}) AS V FROM {t} WHERE {where} GROUP BY 1")
                params += [metric, str(start), str(end), *filter_params]
    return selects, params


def _column_selects(entry: dict, wanted: list, start, end) -> tuple[list, list]:
    """(SELECTs, bound values) giving each column aggregate per date. An
    average comes with its row count per day under '<metric>#w', so the
    weeks and months can be weighted by it. Column names are only ever those
    column_metrics listed from INFORMATION_SCHEMA."""
    t, d = entry["table"], entry["date_column"]
    where = f"{d}::DATE >= %s AND {d}::DATE <= %s"
    selects, params = [], []
    for metric in wanted:
        column, agg = parse_column_metric(metric)
        parts = [("COUNT(*)", metric)] if agg == "count" else [(f"SUM({column})", metric)] if agg == "sum" \
            else [(f"AVG({column})", metric), (f"COUNT({column})", metric + WEIGHT_SUFFIX)]
        for expr, name in parts:
            selects.append(f"SELECT {d}::DATE AS D, %s AS S, {expr} AS V FROM {t} WHERE {where} GROUP BY 1")
            params += [name, str(start), str(end)]
    return selects, params


def fetch_sql(entry: dict, metrics: list, start, end, columns: dict | None = None) -> tuple[str, list]:
    """SQL and bound values reading `metrics` for one entry from start to end.
    The table and column names must already have been checked against the
    live table; metrics of a wide table likewise (they are column names).
    columns: the table's {COLUMN: data_type}, needed when a difference is read.

    Wide: the date and the chosen columns. Long: every date the table holds
    in the range, left-joined to the summed values of the chosen names, so a
    date the table has but a name has no row for comes back as that date with
    no name (worked out as 0 in rows_to_by_date)."""
    t, d = entry["table"], entry["date_column"]
    if entry["shape"] == "wide":
        cols = ", ".join(dict.fromkeys(metrics))
        return (f"SELECT {d}::DATE AS D, {cols} FROM {t} "
                f"WHERE {d}::DATE >= %s AND {d}::DATE <= %s ORDER BY 1"), [str(start), str(end)]
    s = entry.get("split_column")
    # Records: each row counts as one.
    total = "COUNT(*)" if entry["shape"] == "records" else f"SUM({entry['value_column']})"
    differences = set(difference_metrics(entry))
    aggregates = set(column_metrics(entry, columns or {}))
    wanted = list(dict.fromkeys(metrics))
    names = [m for m in wanted if m not in differences and m not in aggregates]
    selects, params = _difference_selects(entry, set(wanted) & differences, columns or {}, start, end)
    agg_selects, agg_params = _column_selects(entry, [m for m in wanted if m in aggregates], start, end)
    selects += agg_selects
    params += agg_params
    if names:
        placeholders = ", ".join(["%s"] * len(names))
        selects.insert(0, f"SELECT {d}::DATE AS D, {s}::VARCHAR AS S, {total} AS V FROM {t} "
                          f"WHERE {d}::DATE >= %s AND {d}::DATE <= %s AND {s}::VARCHAR IN ({placeholders}) "
                          f"GROUP BY 1, 2")
        params = [str(start), str(end), *names, *params]
    sql = (f"WITH days AS (SELECT DISTINCT {d}::DATE AS D FROM {t} "
           f"WHERE {d}::DATE >= %s AND {d}::DATE <= %s), "
           f"vals AS ({' UNION ALL '.join(selects)}) "
           f"SELECT days.D, vals.S, vals.V FROM days LEFT JOIN vals ON vals.D = days.D ORDER BY 1")
    return sql, [str(start), str(end), *params]


def _as_date(value) -> datetime.date:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return datetime.date.fromisoformat(str(value)[:10])


def rows_to_by_date(entry: dict, metrics: list, rows, names) -> dict:
    """{date: {metric: value}} from fetch_sql's result.

    Long and records tables: on any date the table holds, a chosen name with
    no row is 0, because GA4 only writes a row for an event that happened. A date missing
    from the whole table is left out, so the chart shows a gap there rather
    than a false 0."""
    out = {}
    if entry["shape"] == "wide":
        names = [str(n).upper() for n in names]
        for r in rows:
            row = dict(zip(names, r))
            out[_as_date(row.pop("D"))] = row
        return out
    # A day with rows but none for an average's column has no average, not 0.
    blank = {m: (None if (parse_column_metric(m) or ("", ""))[1] == "avg" else 0) for m in metrics}
    for d, split, value in rows:
        day = out.setdefault(_as_date(d), dict(blank))
        if split is not None:
            day[str(split)] = value if value is not None else blank.get(str(split), 0)
    return out
