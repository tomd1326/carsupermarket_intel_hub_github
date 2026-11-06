"""Deterministic report planning. Prose suggests fields; only confirmed choices run.

Executable source descriptions are approved table entries in the site's knowledge.
No generated expressions, joins, LLM, or arbitrary SharePoint URLs are accepted.
"""
import datetime as dt
import hashlib
import json
import re

from utils import ai_knowledge as ak
from utils import self_analytics as sa

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TABLE = re.compile(r"^PROD_CARSUPERMARKET_DB\.[A-Z_][A-Z0-9_]*\.[A-Z_][A-Z0-9_]*$", re.I)
TYPES = {"number", "text", "date", "datetime", "bool"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def active_entries():
    return [e for e in ak.list_entries() if e.get("enabled") and e.get("status") == "active"
            and e.get("show_on_page", True) and e.get("scope") in ak.SCOPES]


def schema(entry):
    """Strict, editable data contract; documentation is never executed as SQL."""
    raw = (entry.get("fields") or {}).get("report_schema")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("table"), str) or not TABLE.fullmatch(data["table"]):
            raise ValueError("Use A Fully Qualified PROD Table Name")
        cols = data["columns"]
        if not isinstance(cols, list) or not 1 <= len(cols) <= 200:
            raise ValueError("Define Between 1 And 200 Columns")
        names = set()
        for c in cols:
            if not isinstance(c, dict) or not IDENTIFIER.fullmatch(c.get("name", "")) or c.get("type") not in TYPES:
                raise ValueError("Each Column Needs A Valid Name And Type")
            if c["name"].lower() in names:
                raise ValueError("Duplicate Column Name")
            names.add(c["name"].lower())
        questions = data.get("questions", [])
        if not isinstance(questions, list) or len(questions) > 20:
            raise ValueError("Use Up To 20 Source Questions")
        for q in questions:
            if not isinstance(q, dict) or q.get("column", "").lower() not in names or not isinstance(q.get("choices"), list):
                raise ValueError("Each Question Must Name A Defined Column And Choices")
            if not q.get("label") or not q["choices"] or any(not isinstance(v, dict) or not v.get("id") or not v.get("label") for v in q["choices"]):
                raise ValueError("Each Choice Needs An ID And Label")
            if len({v['id'] for v in q['choices']}) != len(q['choices']):
                raise ValueError("Choice IDs Must Be Unique")
        return {**data, "questions": questions, "id": entry["id"], "name": entry["title"], "kind": "snowflake",
                "evidence": entry.get("content", ""), "revision": digest(data)}
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"Source Definition {entry['title']}: {exc}") from exc


def catalog():
    entries = active_entries()
    sources, issues = [], []
    for e in entries:
        try:
            source = schema(e)
            if source:
                sources.append(source)
        except ValueError as exc:
            issues.append(str(exc))
    from utils import sharepoint_sources as sp
    try:
        sources.extend({"id": "sp:" + s["source_key"], "name": s["name"], "kind": "sharepoint"}
                       for s in sp.list_sources() if s.get("enabled"))
    except Exception:
        issues.append("SharePoint Sources Could Not Be Read. Check Settings > SharePoint.")
    return {"sources": sources, "issues": issues, "knowledge": [
        {"title": e["title"], "content": e.get("content", ""), "scope": e.get("scope"), "fields": e.get("fields") or {}}
        for e in entries if not (e.get("fields") or {}).get("report_schema")],
        "limits": "One Approved Dataset Per Report. Joins And New Calculations Need A Verified Source Definition. "
                  "A Missing Catalogue Field Does Not Prove The Data Is Absent From Snowflake."}


def spreadsheet(source_id):
    from utils import sharepoint_sources as sp
    key = source_id[3:]
    src = sp.get_source(key)
    if not src.get("enabled"):
        raise ValueError("This SharePoint Source Is Disabled")
    copy = sp.get_copy(key, check="auto", wait=False)
    if copy.error or copy.pending or not copy.values:
        raise ValueError(copy.error or "The SharePoint Copy Is Updating Or Missing. Refresh It In Settings > SharePoint And Try Again.")
    headers = [str(v or "").strip() for v in copy.values[0]]
    if not headers or any(not h for h in headers) or len(set(h.lower() for h in headers)) != len(headers):
        raise ValueError("The File Needs Unique, Nonempty Column Headers")
    rows = [list(r[:len(headers)]) + [None] * max(0, len(headers) - len(r)) for r in copy.values[1:]]
    columns = sa.shape_result(rows[:200], headers, 200)["columns"]
    date_header = src.get("date_header")
    for c in columns:
        if c["name"] == date_header:
            c["type"] = "date"
    result = {"id": source_id, "name": src["name"], "kind": "sharepoint", "columns": columns,
              "questions": [], "grain": "Confirm The Workbook Row Meaning With Its Owner",
              "evidence": "Configured SharePoint File: " + src["name"],
              "revision": digest({"headers": headers, "date": date_header, "url": src["url"],
                                  "range": src.get("sheet_or_table"), "kind": src.get("read_kind")}),
              "freshness": str(copy.modified or copy.saved_at or "Unknown")}
    return result, rows


def source(source_id):
    if not isinstance(source_id, str):
        raise ValueError("Choose A Source")
    if source_id.startswith("sp:"):
        return spreadsheet(source_id)[0]
    for e in active_entries():
        if e["id"] == source_id:
            result = schema(e)
            if result:
                return result
    raise ValueError("Approve A Source In Semantic & Context Layer First")


def literal(value):
    if value is None:
        raise ValueError("Choose A Filter Value")
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if len(str(value)) > 500:
        raise ValueError("Filter Value Is Too Long")
    # Snowflake interprets backslash escapes inside single quoted strings.
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def prepare(payload, definition=None):
    if not isinstance(payload, dict):
        raise ValueError("Bad Report Plan")
    spec = payload.get("source_spec") or {}
    if not isinstance(spec, dict):
        raise ValueError("Choose A Report Source")
    definition = definition or source(spec.get("id"))
    if spec.get("revision") != definition["revision"]:
        raise ValueError("The Source Definition Changed. Choose It Again And Review The Report.")
    cols = {c["name"]: c for c in definition["columns"]}
    date = spec.get("date")
    if date not in cols or cols[date]["type"] not in ("date", "datetime"):
        raise ValueError("Which Date Should The Report Use? Choose A Defined Date Column.")
    try:
        start, end = (dt.date.fromisoformat(spec[k]) for k in ("from", "to"))
        if end < start:
            raise ValueError()
        next_day = end + dt.timedelta(days=1)
    except (KeyError, ValueError, TypeError, OverflowError):
        raise ValueError("Choose A Valid From And To Date, In That Order")
    fields = spec.get("fields")
    if not isinstance(fields, list) or not fields or len(fields) > 200 or any(c not in cols for c in fields):
        raise ValueError("Choose The Exact Columns Needed, Including Any Drill-Down Details")
    fields = list(dict.fromkeys([date] + fields))
    filters = []
    answers = spec.get("answers") or {}
    if not isinstance(answers, dict):
        raise ValueError("Answer Each Source Question")
    for q in definition.get("questions", []):
        choices = [v for v in q["choices"] if v["id"] == answers.get(q["column"])]
        if len(choices) != 1:
            raise ValueError(q["label"])
        choice = choices[0]
        if "value" in choice:
            filters.append({"column": q["column"], "op": choice.get("op", "="), "value": choice["value"]})
    extra = spec.get("filters", [])
    if not isinstance(extra, list) or len(extra) > 20:
        raise ValueError("Use Up To 20 Report Filters")
    filters += extra
    for f in filters:
        if not isinstance(f, dict) or f.get("column") not in cols or f.get("op") not in ("=", "!=", ">", ">=", "<", "<=", "is_empty", "not_empty"):
            raise ValueError("Choose A Known Column And Supported Report Filter")
        if f["op"] not in ("is_empty", "not_empty"):
            literal(f.get("value"))
    interactive = spec.get("interactive", [])
    if not isinstance(interactive, list) or any(c not in fields for c in interactive):
        raise ValueError("Interactive Filters Must Be Selected Report Columns")
    charts = payload.get("charts")
    if not isinstance(charts, list) or not 1 <= len(charts) <= 40:
        raise ValueError("Add At Least One Chart, Table Or Number Tile")
    cleaned = []
    for i, chart in enumerate(charts):
        if not isinstance(chart, dict) or chart.get("type") not in sa.CHART_TYPES:
            raise ValueError("Choose Each Chart Type")
        if chart.get("cohort") not in ("complete", "available"):
            raise ValueError("Choose Matched Rows Or Available Values For Every Chart")
        if chart.get("grain") not in sa.GRAINS or chart.get("format") not in sa.FORMATS:
            raise ValueError("Confirm Date Grouping And Units For Every Chart")
        if not isinstance(chart.get("measures"), list) or not 1 <= len(chart["measures"]) <= 12:
            raise ValueError("Each Chart Needs Between 1 And 12 Measures")
        if chart["type"] in ("pie", "doughnut", "tile") and (len(chart["measures"]) != 1 or chart.get("split")):
            raise ValueError("Pie, Doughnut And Number Tiles Need One Measure And No Series Split")
        if chart["type"] == "tile" and (chart.get("x") or chart.get("grain") != "none"):
            raise ValueError("A Number Tile Needs One Total With No X Axis Or Date Grouping")
        if chart["type"] != "tile" and not chart.get("x"):
            raise ValueError("Choose The Chart X Axis, Or Use A Number Tile For One Total")
        if not isinstance(chart.get("filters", []), list) or len(chart.get("filters", [])) > 20:
            raise ValueError("Use Up To 20 Filters Per Chart")
        for f in chart.get("filters", []):
            if not isinstance(f, dict) or f.get("column") not in fields or f.get("op") not in sa.FILTER_OPS:
                raise ValueError("Choose A Valid Chart Filter")
        required = [chart.get("x"), chart.get("split")]
        for m in chart["measures"]:
            if not isinstance(m, dict):
                raise ValueError("Choose A Measure")
            if m.get("agg") not in sa.AGGS or (not m.get("column") and m.get("agg") != "count"):
                raise ValueError("Choose Each Measure And Calculation")
            required += [m.get("column"), m.get("minus"), m.get("denominator")]
            if m.get("denominator") and (m.get("minus") or m.get("agg") != "sum"):
                raise ValueError("A Ratio Uses Sum(Numerator) / Sum(Denominator), Without Minus")
            if (m.get("denominator") or m.get("as_pct")) and chart.get("format") != "percent":
                raise ValueError("Choose Percent Units For Ratios And Percentage Differences")
            if m.get("as_pct") and not m.get("minus"):
                raise ValueError("A Percentage Difference Needs A Minus Column")
            if m.get("agg") not in ("count", "count_distinct"):
                if any(c and cols.get(c, {}).get("type") != "number" for c in [m.get("column"), m.get("minus"), m.get("denominator")]):
                    raise ValueError("Numeric Calculations Need Numeric Columns")
        required += [f.get("column") for f in chart.get("filters", [])]
        if any(c not in fields for c in required if c):
            raise ValueError("Select All Columns Used By Charts And Their Filters")
        if chart.get("grain") != "none" and cols.get(chart.get("x"), {}).get("type") not in ("date", "datetime"):
            raise ValueError("Date Grouping Needs A Date Axis")
        if chart["type"] == "scatter" and (len(chart["measures"]) != 1 or chart.get("split") or
                cols.get(chart.get("x"), {}).get("type") != "number" or
                chart["measures"][0].get("minus") or chart["measures"][0].get("denominator")):
            raise ValueError("Scatter Uses Two Raw Numeric Columns, One Measure And No Split")
        cleaned.append(sa._clean_chart(chart, i))
    sql = ""
    if definition["kind"] == "snowflake":
        quote = lambda c: '"' + c.upper() + '"'
        conditions = [f"{quote(date)} >= '{start}'::DATE", f"{quote(date)} < '{next_day}'::DATE"]
        for f in filters:
            op = f["op"]
            conditions.append(f"{quote(f['column'])} " + ("IS NULL" if op == "is_empty" else "IS NOT NULL" if op == "not_empty" else f"{op} {literal(f['value'])}"))
        sql = "SELECT " + ", ".join(quote(c) for c in fields) + "\nFROM " + definition["table"] + "\nWHERE " + "\n  AND ".join(conditions)
    clean_spec = {k: spec[k] for k in ("id", "revision", "date", "from", "to")}
    clean_spec.update(fields=fields, answers=answers, filters=extra, interactive=interactive,
                      request=str(payload.get("prompt", spec.get("request", "")))[:3000])
    plan = {"source_spec": clean_spec, "sql": sql, "charts": cleaned,
            "columns": [cols[c] for c in fields], "source_name": definition["name"],
            "grain": definition.get("grain", "Not Documented"), "filters": filters,
            "evidence": definition.get("evidence", ""), "freshness": definition.get("freshness", "Live Snowflake Query At Run Time; Upstream Build Time Not Verified"),
            "prompt": clean_spec["request"]}
    plan["review_hash"] = digest(plan)
    return plan


def run(spec, charts, settings):
    if not settings.get("builder_enabled"):
        raise ValueError("Guided Reports Are Switched Off In Self-Analytics Settings")
    definition, rows = (spreadsheet(spec["id"]) if spec["id"].startswith("sp:") else (source(spec["id"]), None))
    plan = prepare({"source_spec": spec, "charts": charts}, definition)
    if definition["kind"] == "snowflake":
        result = sa.run_query(plan["sql"], settings)
    else:
        names = [c["name"] for c in definition["columns"]]
        date_index = names.index(spec["date"])
        selected = []
        for row in rows:
            value = row[date_index]
            if value in (None, ""):
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                value = (dt.datetime(1899, 12, 30) + dt.timedelta(days=value)).date().isoformat()
            else:
                value = str(value)[:10]
            try:
                dt.date.fromisoformat(value)
            except ValueError:
                raise ValueError("The Selected Workbook Date Column Contains An Unrecognised Date. Use ISO Dates Or Excel Dates.")
            if not spec["from"] <= value <= spec["to"]:
                continue
            row[date_index] = value
            if all(_matches(row[names.index(f["column"])], f) for f in plan["filters"]):
                selected.append([row[names.index(c)] for c in plan["source_spec"]["fields"]])
                if len(selected) > settings["row_limit"]:
                    break
        result = sa.shape_result(selected, plan["source_spec"]["fields"], settings["row_limit"])
        result.update(ms=0, source_modified=definition["freshness"])
    if result["truncated"]:
        raise ValueError("The Report Exceeds The Row Limit. Narrow The Dates Or Filters; Partial Data Cannot Be Charted.")
    if len(json.dumps(result)) > settings["max_response_mb"] * 1_000_000:
        raise ValueError("The Report Is Too Large. Select Fewer Columns Or Narrow The Date Range.")
    return result


def _matches(value, f):
    op, wanted = f["op"], f.get("value")
    if op == "is_empty":
        return value is None or value == ""
    if op == "not_empty":
        return value is not None and value != ""
    if value is None:
        return False
    a, b = str(value), str(wanted)
    if isinstance(value, bool):
        a, b = a.lower(), b.lower()
    elif isinstance(value, (int, float)):
        try:
            a, b = float(value), float(wanted)
        except (ValueError, TypeError):
            raise ValueError("A Numeric Filter Needs A Numeric Value")
    return {"=": lambda: a == b, "!=": lambda: a != b, ">": lambda: a > b, ">=": lambda: a >= b,
            "<": lambda: a < b, "<=": lambda: a <= b}[op]()
