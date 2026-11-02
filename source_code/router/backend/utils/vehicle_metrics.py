"""Vehicle metrics: an average (or similar) worked out from a Snowflake table and
shown against a vehicle, for example % AT PTM or days to sell across past sales
of the same CAP ID.

Each metric is set up on the page and saved as:

    {id, label, description, enabled, table, column, calculation, ignore_zero,
     date_column, date_mode, days, date_from, date_to,
     filters: [{column, operator, value}],
     match:    [{field, column}],   the vehicle's field and the table column it must equal
     fallback: [{field, column}],   used when `match` finds too few rows (may be empty)
     min_rows, format, decimals}

Cars Bought is the first page to show them. Nothing here belongs to that page,
so the Supplier Stock Evaluator and Assess Vehicle can use the same metrics.

Nothing here talks to Snowflake or the database; the router does that. Table and
column names go into the SQL, so they are checked against the live table first.
Values are always sent as parameters.
"""

import re

from utils import kpi_data_sources as kds

METRIC_ID = r"m_[a-z0-9]{1,20}"
METRICS_MAX = 40
LABEL_MAX = 60
DESCRIPTION_MAX = 300
VALUE_MAX = 200
FILTERS_MAX = 20
MATCH_MAX = 3
DAYS_MAX = 3650
ROWS_LIMIT = 100000      # most groups read for one metric

CALCULATIONS = {"avg": "AVG", "median": "MEDIAN", "min": "MIN", "max": "MAX", "sum": "SUM", "count": "COUNT"}
DATE_MODES = ("all", "rolling", "fixed")
FORMATS = ("number", "gbp", "pct")
NO_VALUE_OPERATORS = ("is_blank", "is_not_blank")
COMPARE_OPERATORS = {"greater_than": ">", "at_least": ">=", "less_than": "<", "at_most": "<="}
OPERATORS = ("equals", "not_equals", "contains", "in_list") + tuple(COMPARE_OPERATORS) + NO_VALUE_OPERATORS
# The vehicle fields a metric can match on, and how the page names them.
VEHICLE_FIELDS = {"cap_id": "CAP ID", "vrm": "Reg", "make": "Make", "model": "Model", "derivative": "Variant",
                  "supplier_name": "Supplier", "colour": "Colour"}
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_FIELDS = {"id", "label", "description", "enabled", "table", "column", "calculation", "ignore_zero", "date_column",
           "date_mode", "days", "date_from", "date_to", "filters", "match", "fallback", "min_rows", "format", "decimals"}


def _whole(value, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def validate_shape(metric) -> str | None:
    """Checks that need no Snowflake."""
    if not isinstance(metric, dict) or set(metric) - _FIELDS:
        return "Bad metric"
    if not isinstance(metric.get("id"), str) or not re.fullmatch(METRIC_ID, metric["id"]):
        return "Bad metric id"
    label = metric.get("label")
    if not isinstance(label, str) or not label.strip() or len(label.strip()) > LABEL_MAX:
        return f"Enter a label of at most {LABEL_MAX} characters"
    label = label.strip()
    if len(str(metric.get("description") or "")) > DESCRIPTION_MAX:
        return f"{label}: the description must be at most {DESCRIPTION_MAX} characters"
    table = metric.get("table")
    if not isinstance(table, str) or not re.fullmatch(rf"{kds.IDENTIFIER}\.{kds.IDENTIFIER}", table) \
            or not table.split(".")[0].startswith(("COMPUTE_", "PRODUCT_")):
        return f"{label}: choose a COMPUTE_ or PRODUCT_ table"
    if metric.get("calculation") not in CALCULATIONS:
        return f"{label}: choose a calculation"
    if metric.get("date_mode") not in DATE_MODES:
        return f"{label}: choose a date range"
    if metric["date_mode"] != "all" and not metric.get("date_column"):
        return f"{label}: choose the date column for the date range"
    if metric["date_mode"] == "rolling" and not _whole(metric.get("days"), 1, DAYS_MAX):
        return f"{label}: the number of days must be from 1 to {DAYS_MAX}"
    if metric["date_mode"] == "fixed":
        start, end = metric.get("date_from") or "", metric.get("date_to") or ""
        if not start and not end:
            return f"{label}: enter a From or To date"
        if any(d and not _ISO_DATE.fullmatch(str(d)) for d in (start, end)):
            return f"{label}: dates must be written as YYYY-MM-DD"
        if start and end and start > end:
            return f"{label}: the From date is after the To date"
    if not _whole(metric.get("min_rows"), 1, 100000):
        return f"{label}: the fewest rows must be a whole number of at least 1"
    if metric.get("format") not in FORMATS:
        return f"{label}: bad format"
    if not _whole(metric.get("decimals"), 0, 4):
        return f"{label}: decimals must be 0 to 4"
    filters = metric.get("filters")
    if not isinstance(filters, list) or len(filters) > FILTERS_MAX:
        return f"{label}: at most {FILTERS_MAX} filters"
    for f in filters:
        if not isinstance(f, dict) or set(f) != {"column", "operator", "value"} or f["operator"] not in OPERATORS:
            return f"{label}: bad filter"
        value = f["value"]
        if not isinstance(value, str) or len(value) > VALUE_MAX:
            return f"{label}: filter values must be text of at most {VALUE_MAX} characters"
        if f["operator"] not in NO_VALUE_OPERATORS and not value.strip():
            return f"{label}: enter a value for each filter"
    for name, title in (("match", "Match On"), ("fallback", "If Too Few Rows, Match On")):
        pairs = metric.get(name)
        if not isinstance(pairs, list) or len(pairs) > MATCH_MAX:
            return f"{label}: {title} can have at most {MATCH_MAX} columns"
        for p in pairs:
            if not isinstance(p, dict) or set(p) != {"field", "column"} or p["field"] not in VEHICLE_FIELDS:
                return f"{label}: bad {title} row"
    if not metric["match"]:
        return f"{label}: choose what to match each vehicle on"
    return None


def validate(metric, columns: dict) -> str | None:
    """Every check, against the table's live {COLUMN: data_type}. Run on save and
    before every read, as the table can change after saving."""
    err = validate_shape(metric)
    if err:
        return err
    label, table = metric["label"].strip(), metric["table"]
    if not columns:
        return f"{label}: table {table} was not found"

    def known(column) -> bool:
        return isinstance(column, str) and column in columns

    if not known(metric.get("column")):
        return f"{label}: choose a column of {table}"
    if metric["calculation"] != "count" and columns[metric["column"]] not in kds.NUMBER_TYPES:
        return f"{label}: {metric['column']} is not a number column, so it can only be counted"
    if metric["date_mode"] != "all":
        if not known(metric.get("date_column")):
            return f"{label}: {metric.get('date_column')} is not a column of {table}"
        if columns[metric["date_column"]] not in kds.DATE_TYPES:
            return f"{label}: {metric['date_column']} is not a date column"
    for f in metric["filters"]:
        if not known(f["column"]):
            return f"{label}: filter column {f['column'] or '(none chosen)'} is not in {table}"
    for pair in metric["match"] + metric["fallback"]:
        if not known(pair["column"]):
            return f"{label}: match column {pair['column'] or '(none chosen)'} is not in {table}"
    return None


def clean(metric: dict) -> dict:
    """The metric as stored: text trimmed, the other date modes' fields dropped."""
    out = {"id": metric["id"], "label": metric["label"].strip(), "description": str(metric.get("description") or "").strip(),
           "enabled": bool(metric.get("enabled")), "table": metric["table"], "column": metric["column"],
           "calculation": metric["calculation"], "ignore_zero": bool(metric.get("ignore_zero")),
           "date_column": metric.get("date_column") or "", "date_mode": metric["date_mode"],
           "filters": [{"column": f["column"], "operator": f["operator"],
                        "value": "" if f["operator"] in NO_VALUE_OPERATORS else f["value"].strip()} for f in metric["filters"]],
           "match": [dict(p) for p in metric["match"]], "fallback": [dict(p) for p in metric["fallback"]],
           "min_rows": metric["min_rows"], "format": metric["format"], "decimals": metric["decimals"]}
    if metric["date_mode"] == "rolling":
        out["days"] = metric["days"]
    if metric["date_mode"] == "fixed":
        out["date_from"], out["date_to"] = metric.get("date_from") or "", metric.get("date_to") or ""
    return out


def filter_sql(filters: list) -> tuple:
    """(clauses, params). All of the filters must match."""
    clauses, params = [], []
    for f in filters:
        column, operator, value = f["column"], f["operator"], f["value"]
        if operator == "is_blank":
            clauses.append(f"({column} IS NULL OR TO_VARCHAR({column}) = '')")
        elif operator == "is_not_blank":
            clauses.append(f"({column} IS NOT NULL AND TO_VARCHAR({column}) <> '')")
        elif operator == "contains":
            clauses.append(f"CONTAINS(UPPER(TO_VARCHAR({column})), UPPER(%s))")
            params.append(value)
        elif operator == "in_list":
            values = [v.strip() for v in value.split(",") if v.strip()]
            clauses.append(f"TO_VARCHAR({column}) IN ({', '.join(['%s'] * len(values))})")
            params.extend(values)
        elif operator in COMPARE_OPERATORS:
            clauses.append(f"{column} {COMPARE_OPERATORS[operator]} %s")
            params.append(value)
        else:
            clauses.append(f"TO_VARCHAR({column}) {'=' if operator == 'equals' else '<>'} %s")
            params.append(value)
    return clauses, params


def date_sql(metric: dict) -> tuple:
    """(clauses, params) for the metric's date range."""
    column = metric.get("date_column")
    if metric["date_mode"] == "rolling":
        return [f"{column} >= DATEADD(DAY, -%s, CURRENT_DATE())"], [metric["days"]]
    if metric["date_mode"] == "fixed":
        clauses, params = [], []
        if metric.get("date_from"):
            clauses.append(f"{column}::DATE >= %s")
            params.append(metric["date_from"])
        if metric.get("date_to"):
            clauses.append(f"{column}::DATE <= %s")
            params.append(metric["date_to"])
        return clauses, params
    return [], []


def read_sql(metric: dict, pairs: list, database: str) -> tuple:
    """(sql, params): one row per value of the match columns: (key, figure, rows).
    The key is the same text vehicle_key() makes for a vehicle."""
    column = metric["column"]
    key = " || '|' || ".join(f"UPPER(TRIM(TO_VARCHAR({p['column']})))" for p in pairs)
    where = [f"{column} IS NOT NULL"] + [f"{p['column']} IS NOT NULL" for p in pairs]
    if metric.get("ignore_zero"):
        where.append(f"{column} <> 0")
    dates, date_params = date_sql(metric)
    filters, filter_params = filter_sql(metric["filters"])
    sql = (f"SELECT {key} AS match_key, {CALCULATIONS[metric['calculation']]}({column}) AS figure, COUNT({column}) AS row_count\n"
           f"FROM {database}.{metric['table']}\n"
           f"WHERE {' AND '.join(where + dates + filters)}\n"
           f"GROUP BY 1\nLIMIT {ROWS_LIMIT}")
    return sql, date_params + filter_params


def column_choices(columns: dict) -> list:
    """A table's columns for the page, saying which can be calculated and which are dates."""
    return [{"column": c, "type": t, "is_number": t in kds.NUMBER_TYPES, "is_date": t in kds.DATE_TYPES}
            for c, t in columns.items()]


def rows_to_figures(rows: list) -> dict:
    """{key: [figure, rows]} from what read_sql returned."""
    return {str(r[0]): [float(r[1]) if r[1] is not None else None, int(r[2])] for r in rows if r[0] is not None}


def vehicle_key(vehicle: dict, pairs: list) -> str | None:
    """The vehicle's values for the match columns, or None if any is blank."""
    parts = []
    for p in pairs:
        value = vehicle.get(p["field"])
        text = str(value).strip().upper() if value is not None else ""
        if not text:
            return None
        parts.append(text)
    return "|".join(parts)


def figure_for(vehicle: dict, metric: dict, figures: dict, fallback_figures: dict | None = None) -> dict | None:
    """{value, rows, matched} for one vehicle, or None. The match is used if it
    has at least min_rows rows behind it, else the fallback on the same test."""
    for pairs, found, level in ((metric["match"], figures, "match"), (metric["fallback"], fallback_figures, "fallback")):
        if not pairs or not found:
            continue
        hit = found.get(vehicle_key(vehicle, pairs) or "")
        if hit and hit[1] >= metric["min_rows"]:
            return {"value": hit[0], "rows": hit[1],
                    "matched": " And ".join(VEHICLE_FIELDS[p["field"]] for p in pairs), "level": level}
    return None
