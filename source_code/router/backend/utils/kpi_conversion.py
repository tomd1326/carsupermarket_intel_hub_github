"""One-off: moves the saved KPI charts and data sources to the new method, where
a data source is just a table and a date column, and each chart's metric says
which column, Sum / Average / Count and filters it reads (kpi_data_sources.py
query_metric). scripts/convert_kpi_charts.py runs it.

Nothing here talks to Snowflake or the database, so it can all be tested. What
changes:

  every multi-series chart   each series gets its kind (column or line), and
                             is stored in the order it draws, so a source's
                             Value Order can go
  long table name            Sum of the value column where the name column = it
  records table name         Count of rows where the name column = it
  Count Rows metric          Count of rows with its filters
  average or difference      '<name> - Total' Divided By '<name> - Count' becomes
                             the Average, and the chart stops being a ratio
  data source                reduced to its table and date column, as a
                             one-row-per-record table

Column names and formats follow to the new metric, and a metric with no name
keeps showing what it showed before, so the legends do not change. A source
whose figures do not match stays exactly as it is, with its charts (see
plan_conversion's skip).
"""

import copy

from utils import kpi_data_sources as kds


def _equals(column: str, value: str) -> dict:
    return {"column": column, "operator": "equals", "value": value}


def source_metric_map(entry: dict) -> dict:
    """{old metric: new metric} for the difference metrics of a source. Names in
    its name column are worked out when they are met (name_metric), as there
    can be hundreds and only the ones charts use matter."""
    out = {}
    for d in entry.get("differences") or []:
        name, filters = d["name"].strip(), d.get("filters") or []
        if kds.metric_kind(d) == "count":
            out[name] = kds.query_metric("count", filters=filters)
            continue
        a, b = d["column_a"], d.get("column_b") or ""
        out[name + kds.TOTAL_SUFFIX] = kds.query_metric("sum", a, b, filters)
        # The rows the total reads: those with no blank in its columns.
        blanks = [{"column": c, "operator": "is_not_blank", "value": ""} for c in (a, b) if c]
        out[name + kds.COUNT_SUFFIX] = kds.query_metric("count", filters=[*filters, *blanks])
    return out


def average_metric(entry: dict, total_metric: str, count_metric: str) -> str | None:
    """The Average that a difference's Total Divided By its own Count is."""
    for d in entry.get("differences") or []:
        name = d["name"].strip()
        if kds.metric_kind(d) != "count" and total_metric == name + kds.TOTAL_SUFFIX and count_metric == name + kds.COUNT_SUFFIX:
            return kds.query_metric("avg", d["column_a"], d.get("column_b") or "", d.get("filters") or [])
    return None


def name_metric(entry: dict, name: str) -> str | None:
    """The built metric for a name in a long or records table's name column."""
    split = entry.get("split_column")
    if not split:
        return None
    if entry["shape"] == "long":
        return kds.query_metric("sum", entry["value_column"], filters=[_equals(split, name)])
    return kds.query_metric("count", filters=[_equals(split, name)])


def new_metric(entry: dict, metric: str) -> str | None:
    """A saved metric's built form, or None if it needs no change (already built)."""
    if kds.parse_query_metric(metric):
        return None
    return source_metric_map(entry).get(metric) or name_metric(entry, metric)


def _draw_order(series: list, sources: dict, names_of) -> list:
    """The series in the order the chart draws them today (kpi.js orderedSeries):
    those from a source with a Value Order take that order, in the places
    those series take up."""
    def rank(s):
        entry = sources.get(s.get("source"))
        if not entry or not entry.get("name_order"):
            return -1
        names = kds.order_names(names_of(entry), entry["name_order"])
        return names.index(s["metric"]) if s.get("metric") in names else -1
    slots = [i for i, s in enumerate(series) if rank(s) >= 0]
    moved = sorted((series[i] for i in slots), key=rank)
    out = list(series)
    for slot, s in zip(slots, moved):
        out[slot] = s
    return out


def _is_ratio_of_own_count(row: dict, entry: dict) -> str | None:
    divide = row.get("divide_by")
    if not divide or row.get("metric_plus") or row.get("divide_plus"):
        return None
    # A ratio shown as a %, or with its own target stored on the chart, is not
    # the same chart as a plain average: it stays a ratio of the two new metrics.
    if row.get("ratio_as", "dec") != "dec" or row.get("target_mode") in ("fixed", "import"):
        return None
    if divide.get("source", "snowflake") != row.get("source", "snowflake"):
        return None
    return average_metric(entry, row.get("metric"), divide.get("metric"))


def metric_pairs(layout: dict, sources: dict) -> dict:
    """{source id: {old: new}}: every metric the layout uses that changes, to be
    compared against Snowflake before anything is written. A ratio of a total
    over its own count is keyed by the two old metrics together."""
    pairs: dict = {}

    def walk(obj):
        if isinstance(obj, dict):
            source = obj.get("source", "snowflake")
            entry = sources.get(source) if "metric" in obj else None
            if entry and entry["shape"] != "wide":
                average = _is_ratio_of_own_count(obj, entry)
                if average:
                    pairs.setdefault(source, {})[(obj["metric"], obj["divide_by"]["metric"])] = average
                    for key, value in obj.items():
                        if key != "divide_by":
                            walk(value)
                    return
                new = new_metric(entry, obj["metric"])
                if new:
                    pairs.setdefault(source, {})[obj["metric"]] = new
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(layout)
    return pairs


def plan_conversion(layout: dict, data_sources: list, metric_names: dict, metric_formats: dict,
                    derived_names: dict, names_of, skip=()) -> dict:
    """The new values, from the old. names_of(entry): the names in a source's
    name column, for its Value Order. skip: source ids to leave exactly as they
    are, because their figures did not match. Returns {layout, data_sources,
    metric_names, metric_formats, derived_names, summary}. Run again it changes
    nothing, as everything converted is already built."""
    layout = copy.deepcopy(layout)
    sources = {s["id"]: s for s in data_sources}
    convert = {i: s for i, s in sources.items() if s["shape"] != "wide" and i not in skip}
    renames: dict = {}
    summary = {"series_marked": 0, "charts_on_new_metrics": [], "ratios_to_averages": 0, "sources_converted": sorted(convert)}

    def rename(obj) -> bool:
        changed = False
        if isinstance(obj, dict):
            entry = convert.get(obj.get("source", "snowflake")) if "metric" in obj else None
            if entry:
                new = new_metric(entry, obj["metric"])
                if new:
                    renames[f"{entry['id']}|{obj['metric']}"] = f"{entry['id']}|{new}"
                    obj["metric"] = new
                    changed = True
            for value in obj.values():
                changed = rename(value) or changed
        elif isinstance(obj, list):
            for value in obj:
                changed = rename(value) or changed
        return changed

    for row in layout.get("rows", []):
        if row.get("series"):
            # Before the metrics change, while they can still be ranked.
            row["series"] = _draw_order(row["series"], sources, names_of)
            for s in row["series"]:
                if not s.get("kind"):
                    s["kind"] = "line" if s.get("axis") == "secondary" else "column"
                    summary["series_marked"] += 1
            if row.get("source") == row["series"][0].get("source"):
                # The first series stands in as the chart's metric.
                row["metric"] = row["series"][0].get("metric")
        changed = False
        entry = convert.get(row.get("source", "snowflake"))
        average = _is_ratio_of_own_count(row, entry) if entry else None
        if average:
            old_total = row["metric"]
            renames[f"{entry['id']}|{old_total}"] = f"{entry['id']}|{average}"
            row["metric"] = average
            for key in ("divide_by", "divide_agg", "ratio_as", "ratio_target"):
                row.pop(key, None)
            summary["ratios_to_averages"] += 1
            changed = True
        changed = rename(row) or changed
        if changed:
            summary["charts_on_new_metrics"].append(row.get("id"))

    names, formats = dict(metric_names or {}), dict(metric_formats or {})
    derived = dict(derived_names or {})
    for old, new in renames.items():
        # A metric with no name showed its own raw name: keep that as its name.
        raw = old.split("|", 1)[1]
        names[new] = names.get(old) or (raw[:-len(kds.TOTAL_SUFFIX)] if raw.endswith(kds.TOTAL_SUFFIX) else raw)[:60]
        if old in formats:
            formats[new] = formats[old]
    for key in list(derived):
        parts = key.split("*")
        if len(parts) == 2 and (parts[0] in renames or parts[1] in renames):
            derived[f"{renames.get(parts[0], parts[0])}*{renames.get(parts[1], parts[1])}"] = derived.pop(key)

    new_sources = [{"id": s["id"], "name": s["name"], "table": s["table"], "shape": "records", "date_column": s["date_column"]}
                   if s["id"] in convert else copy.deepcopy(s) for s in data_sources]
    return {"layout": layout, "data_sources": new_sources, "metric_names": names, "metric_formats": formats,
            "derived_names": derived, "summary": summary}
