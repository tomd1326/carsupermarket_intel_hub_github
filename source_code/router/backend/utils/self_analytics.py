"""Self-Analytics (/self-analytics): run a Snowflake SELECT, then chart the
rows in the browser (static/js/self_analytics.js does the grouping and the
charts). Saved reports keep the SQL and their charts; page settings keep the
limits and the look. Both live in the prod pricer DB, like ai_knowledge, so
local and prod share them.

Only a single read query can run: it must start with SELECT or WITH and is run
as  SELECT * FROM (<sql>) LIMIT n , which Snowflake refuses for anything that
changes data. Snowflake AI functions are refused too (no Snowflake AI anywhere)."""

import datetime
import decimal
import json
import logging
import re
import time

from utils.db_utils import get_prod_pricer_connection

logger = logging.getLogger(__name__)

SQL_MAX = 50_000
NAME_MAX = 120
FOLDER_MAX = 80

CHART_TYPES = ["bar", "stacked_bar", "horizontal_bar", "line", "area", "pie", "doughnut", "scatter", "table", "tile"]
DESCRIPTION_MAX = 3000
# Who sees a report besides the person who made it and the admin.
VISIBILITIES = ["only_me", "everyone", "chosen"]
GRAINS = ["none", "day", "week", "month", "quarter", "year"]
AGGS = ["sum", "avg", "count", "count_distinct", "min", "max", "median"]
FILTER_OPS = ["=", "!=", ">", ">=", "<", "<=", "contains", "not_contains", "is_empty", "not_empty"]
FORMATS = ["number", "currency", "percent"]
SORTS = ["x", "value_desc", "value_asc"]

# ── Page settings ────────────────────────────────────────────────────────────

SETTINGS_DEFAULTS = {
    "row_limit": 20000,
    "max_response_mb": 5,
    "charts_per_row": 2,
    "chart_height_px": 320,
    "chart_title_px": 14,
    "axis_font_px": 11,
    "legend_font_px": 11,
    "data_label_px": 10,
    "table_rows_shown": 200,
    "cache_minutes": 15,    # a saved report's rows are reused this long, unless Refresh is pressed
    "tile_value_px": 40,
    "tile_label_px": 13,
    "report_title_px": 22,
    "default_grain": "month",
    "default_chart_type": "bar",
    "describe_default_agg": "avg",   # used by Describe A Chart when no calculation word is given
    "currency_symbol": "£",
    "palette": ["#1e3a8a", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6", "#0ea5e9",
                "#f97316", "#14b8a6", "#e11d48", "#64748b"],
    "intro": ("Run a Snowflake query, then build charts from the rows. Describe A Chart turns a "
              "plain request such as \"average purchase price and sale price by month, line chart\" "
              "into a chart using the column names and the Also Called words on the Semantic & "
              "Context Layer page. No external AI is used."),
}
SETTINGS_LIMITS = {
    "row_limit": (100, 200000),
    "max_response_mb": (1, 6),     # Lambda cannot send back more than 6 MB
    "charts_per_row": (1, 4),
    "chart_height_px": (160, 900),
    "chart_title_px": (9, 30),
    "axis_font_px": (8, 24),
    "legend_font_px": (8, 24),
    "data_label_px": (7, 24),
    "table_rows_shown": (20, 5000),
    "cache_minutes": (0, 1440),
    "tile_value_px": (14, 120),
    "tile_label_px": (9, 40),
    "report_title_px": (12, 48),
}
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def clean_settings(stored) -> dict:
    d = SETTINGS_DEFAULTS
    stored = stored if isinstance(stored, dict) else {}
    result = {}
    for key, (low, high) in SETTINGS_LIMITS.items():
        try:
            result[key] = min(max(int(stored[key]), low), high)
        except (KeyError, TypeError, ValueError):
            result[key] = d[key]
    result["default_grain"] = stored.get("default_grain") if stored.get("default_grain") in GRAINS else d["default_grain"]
    result["default_chart_type"] = (stored.get("default_chart_type") if stored.get("default_chart_type") in CHART_TYPES
                                    else d["default_chart_type"])
    result["describe_default_agg"] = (stored.get("describe_default_agg") if stored.get("describe_default_agg") in AGGS
                                      else d["describe_default_agg"])
    symbol = stored.get("currency_symbol")
    result["currency_symbol"] = symbol[:3] if isinstance(symbol, str) else d["currency_symbol"]
    palette = stored.get("palette")
    palette = [c for c in palette if isinstance(c, str) and _HEX.match(c)] if isinstance(palette, list) else []
    result["palette"] = palette[:30] or list(d["palette"])
    intro = stored.get("intro")
    result["intro"] = intro[:3000] if isinstance(intro, str) else d["intro"]
    return result


# ── SQL check ────────────────────────────────────────────────────────────────

_AI_PATTERNS = re.compile(r"\bCORTEX\b|\bAI_[A-Z_]+\s*\(|\bML\.|\bCOMPLETE\s*\(", re.IGNORECASE)


def _strip(sql: str) -> str:
    """The SQL with comments removed and string contents blanked, so keyword
    checks never trip on text inside quotes or comments."""
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif c in ("'", '"'):
            j = i + 1
            while j < n:
                if sql[j] == c and j + 1 < n and sql[j + 1] == c:
                    j += 2
                    continue
                if sql[j] == "\\" and c == "'":
                    j += 2
                    continue
                if sql[j] == c:
                    break
                j += 1
            out.append(c + c if c == "'" else '"x"')
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def check_sql(sql) -> str | None:
    """Why the SQL cannot be run, or None."""
    if not isinstance(sql, str) or not sql.strip():
        return "Type a query first."
    if len(sql) > SQL_MAX:
        return f"The query must be under {SQL_MAX:,} characters."
    bare = _strip(sql).strip().rstrip(";").strip()
    if not bare:
        return "Type a query first."
    if ";" in bare:
        return "Run one query at a time (there is a ; in the middle)."
    first = re.match(r"\(*\s*([A-Za-z]+)", bare)
    if not first or first.group(1).upper() not in ("SELECT", "WITH"):
        return "Only queries that read data can be run: start with SELECT or WITH."
    if _AI_PATTERNS.search(bare):
        return "Snowflake AI functions (Cortex, AI_...) cannot be used."
    return None


def runnable_sql(sql: str, limit: int) -> str:
    """The query as it is sent: wrapped, so it can only read, and capped one
    row over the limit so we know when rows were cut off."""
    body = sql.strip()
    while body.endswith(";"):
        body = body[:-1].rstrip()
    return f"SELECT * FROM (\n{body}\n) LIMIT {int(limit) + 1}"


# ── Results ──────────────────────────────────────────────────────────────────

def _value(v):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    if isinstance(v, decimal.Decimal):
        f = float(v)
        return int(f) if f.is_integer() and abs(f) < 2 ** 53 else f
    if isinstance(v, datetime.datetime):
        return v.replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")
    if isinstance(v, datetime.date):
        return v.isoformat()
    if isinstance(v, datetime.time):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return None
    return str(v)


def column_type(values: list) -> str:
    """number, date, datetime, bool or text, from the first values that are set."""
    seen = [v for v in values if v is not None][:200]
    if not seen:
        return "text"
    if all(isinstance(v, bool) for v in seen):
        return "bool"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in seen):
        return "number"
    if all(isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in seen):
        return "date"
    if all(isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?", v) for v in seen):
        return "datetime"
    return "text"


def shape_result(rows: list, cols: list, limit: int) -> dict:
    truncated = len(rows) > limit
    rows = [[_value(v) for v in r] for r in rows[:limit]]
    columns = [{"name": name, "type": column_type([r[i] for r in rows])} for i, name in enumerate(cols)]
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}


def run_query(sql: str, settings: dict) -> dict:
    """Run a checked query. Raises ValueError with a reason Tom can act on."""
    error = check_sql(sql)
    if error:
        raise ValueError(error)
    from utils.snowflake_client import SnowflakeClient   # not needed by the tests
    limit = settings["row_limit"]
    started = time.monotonic()
    with SnowflakeClient() as sf:
        rows, cols = sf.execute(runnable_sql(sql, limit))
    result = shape_result(rows, cols, limit)
    result["ms"] = int((time.monotonic() - started) * 1000)
    size = len(json.dumps(result["rows"], separators=(",", ":")))
    if size > settings["max_response_mb"] * 1_000_000:
        raise ValueError(f"The result is {size / 1_000_000:.1f} MB, over the {settings['max_response_mb']} MB "
                         "the website can send back. Pick fewer columns, add a WHERE, or group in SQL.")
    return result


# ── Saved reports ────────────────────────────────────────────────────────────

def _clean_chart(c: dict, index: int) -> dict:
    """One chart's options, checked. Column names are kept as typed: a column
    the query no longer returns just shows an empty chart with a note."""
    def col(v):
        return v.strip()[:200] if isinstance(v, str) and v.strip() else None

    measures = []
    for m in (c.get("measures") or [])[:12]:
        if not isinstance(m, dict) or not col(m.get("column")) and m.get("agg") != "count":
            continue
        measures.append({
            "column": col(m.get("column")),
            "agg": m.get("agg") if m.get("agg") in AGGS else "sum",
            "minus": col(m.get("minus")),
            "as_pct": bool(m.get("as_pct")),
            "label": (m.get("label") or "")[:120] if isinstance(m.get("label"), str) else "",
            "color": m.get("color") if isinstance(m.get("color"), str) and _HEX.match(m["color"]) else "",
        })
    filters = []
    for f in (c.get("filters") or [])[:20]:
        if isinstance(f, dict) and col(f.get("column")) and f.get("op") in FILTER_OPS:
            filters.append({"column": col(f["column"]), "op": f["op"], "value": str(f.get("value") or "")[:500]})
    try:
        top_n = min(max(int(c.get("top_n") or 0), 0), 1000)
    except (TypeError, ValueError):
        top_n = 0
    try:
        width = min(max(int(c.get("width") or 1), 1), 4)
    except (TypeError, ValueError):
        width = 1
    try:
        decimals = min(max(int(c.get("decimals") or 0), 0), 4)
    except (TypeError, ValueError):
        decimals = 0
    return {
        "id": str(c.get("id") or f"c{index}")[:40],
        "title": (c.get("title") or "")[:200] if isinstance(c.get("title"), str) else "",
        "subtitle": (c.get("subtitle") or "")[:300] if isinstance(c.get("subtitle"), str) else "",
        "type": c.get("type") if c.get("type") in CHART_TYPES else "bar",
        "x": col(c.get("x")),
        "grain": c.get("grain") if c.get("grain") in GRAINS else "none",
        "split": col(c.get("split")),
        "measures": measures,
        "filters": filters,
        "sort": c.get("sort") if c.get("sort") in SORTS else "x",
        "top_n": top_n,
        "format": c.get("format") if c.get("format") in FORMATS else "number",
        "decimals": decimals,
        "from_zero": c.get("from_zero", True) is not False,
        "data_labels": bool(c.get("data_labels")),
        "legend": c.get("legend", True) is not False,
        "width": width,
        "description": (c.get("description") or "")[:500] if isinstance(c.get("description"), str) else "",
    }


def clean_report(report: dict) -> dict:
    if not isinstance(report, dict):
        raise ValueError("Bad report")
    name = (report.get("name") or "").strip() if isinstance(report.get("name"), str) else ""
    if not name or len(name) > NAME_MAX:
        raise ValueError(f"A name is needed, under {NAME_MAX} characters.")
    error = check_sql(report.get("sql"))
    if error:
        raise ValueError(error)
    charts = report.get("charts") if isinstance(report.get("charts"), list) else []
    folder = report.get("folder") if isinstance(report.get("folder"), str) else ""
    description = report.get("description") if isinstance(report.get("description"), str) else ""
    visibility = report.get("visibility")
    if visibility not in VISIBILITIES:
        # Reports saved before named sharing only had the shared tick.
        visibility = "everyone" if report.get("shared") else "only_me"
    users = report.get("users") if isinstance(report.get("users"), list) else []
    users = sorted({u.strip() for u in users if isinstance(u, str) and u.strip()})[:200]
    return {
        "name": name, "sql": report["sql"].strip(), "folder": folder.strip()[:FOLDER_MAX],
        "description": description.strip()[:DESCRIPTION_MAX],
        "charts": [_clean_chart(c, i) for i, c in enumerate(charts[:40]) if isinstance(c, dict)],
        "visibility": visibility, "users": users if visibility == "chosen" else [],
        "shared": visibility == "everyone",
        "show_table": bool(report.get("show_table")),
    }


_ready = False


def _ensure(conn) -> None:
    global _ready
    if _ready:
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS self_analytics_reports (
                id SERIAL PRIMARY KEY,
                name VARCHAR(120) NOT NULL,
                folder VARCHAR(80) NOT NULL DEFAULT '',
                sql TEXT NOT NULL,
                charts TEXT NOT NULL DEFAULT '[]',
                shared BOOLEAN NOT NULL DEFAULT FALSE,
                created_by VARCHAR(100),
                updated_by VARCHAR(100),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS self_analytics_settings (
                key VARCHAR(50) PRIMARY KEY,
                value TEXT NOT NULL,
                updated_by VARCHAR(100),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")
        # Added for view-only sharing and the clean view. shared is kept in
        # step with visibility = 'everyone', so the line below is safe to rerun.
        cur.execute("ALTER TABLE self_analytics_reports ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE self_analytics_reports ADD COLUMN IF NOT EXISTS visibility VARCHAR(10) NOT NULL DEFAULT 'only_me'")
        cur.execute("ALTER TABLE self_analytics_reports ADD COLUMN IF NOT EXISTS show_table BOOLEAN NOT NULL DEFAULT FALSE")
        cur.execute("UPDATE self_analytics_reports SET visibility = 'everyone' WHERE shared AND visibility = 'only_me'")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS self_analytics_report_users (
                report_id INTEGER NOT NULL REFERENCES self_analytics_reports(id) ON DELETE CASCADE,
                username VARCHAR(100) NOT NULL,
                PRIMARY KEY (report_id, username)
            )""")
    conn.commit()
    _ready = True


_COLS = ("id, name, folder, sql, charts, shared, created_by, updated_by, updated_at, "
         "description, visibility, show_table")


def _row(r, users=None) -> dict:
    report = dict(zip([c.strip() for c in _COLS.split(",")], r))
    try:
        report["charts"] = json.loads(report["charts"] or "[]")
    except ValueError:
        report["charts"] = []
    if isinstance(report["updated_at"], datetime.datetime):
        report["updated_at"] = report["updated_at"].isoformat()
    report["users"] = sorted(users or [])
    return report


def can_see(report: dict, user: str, admin: bool) -> bool:
    return (admin or report["created_by"] == user or report["visibility"] == "everyone"
            or (report["visibility"] == "chosen" and user in report["users"]))


def _users_by_report(cur) -> dict:
    cur.execute("SELECT report_id, username FROM self_analytics_report_users")
    out = {}
    for report_id, username in cur.fetchall():
        out.setdefault(report_id, []).append(username)
    return out


def list_reports(user: str, admin: bool) -> list:
    with get_prod_pricer_connection() as conn:
        _ensure(conn)
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_COLS} FROM self_analytics_reports ORDER BY folder, name")
            rows = cur.fetchall()
            users = _users_by_report(cur)
        conn.commit()
    return [r for r in (_row(x, users.get(x[0])) for x in rows) if can_see(r, user, admin)]


def get_report(report_id: int) -> dict | None:
    with get_prod_pricer_connection() as conn:
        _ensure(conn)
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_COLS} FROM self_analytics_reports WHERE id = %s", (report_id,))
            row = cur.fetchone()
            cur.execute("SELECT username FROM self_analytics_report_users WHERE report_id = %s", (report_id,))
            users = [u[0] for u in cur.fetchall()]
        conn.commit()
    return _row(row, users) if row else None


def save_report(report: dict, user: str, admin: bool, known_users=None) -> dict:
    """Insert (no id) or update (id). Only the person who made a report, or
    the admin, can change it. known_users: the usernames that may be chosen."""
    clean = clean_report(report)
    if known_users is not None:
        unknown = [u for u in clean["users"] if u not in known_users]
        if unknown:
            raise ValueError(f"Not a user of this site: {', '.join(unknown)}")
    values = (clean["name"], clean["folder"], clean["sql"], json.dumps(clean["charts"]), clean["shared"], user,
              clean["description"], clean["visibility"], clean["show_table"])
    with get_prod_pricer_connection() as conn:
        _ensure(conn)
        with conn.cursor() as cur:
            if report.get("id"):
                cur.execute("SELECT created_by FROM self_analytics_reports WHERE id = %s", (int(report["id"]),))
                owner = cur.fetchone()
                if owner is None:
                    raise ValueError("That report no longer exists.")
                if not admin and owner[0] != user:
                    raise ValueError("Only the person who made this report can change it. Use Save As.")
                cur.execute(
                    "UPDATE self_analytics_reports SET name = %s, folder = %s, sql = %s, charts = %s, shared = %s, "
                    "updated_by = %s, description = %s, visibility = %s, show_table = %s, updated_at = NOW() "
                    f"WHERE id = %s RETURNING {_COLS}", values + (int(report["id"]),))
            else:
                cur.execute(
                    "INSERT INTO self_analytics_reports (name, folder, sql, charts, shared, updated_by, description, "
                    f"visibility, show_table, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    f"RETURNING {_COLS}", values + (user,))
            row = cur.fetchone()
            cur.execute("DELETE FROM self_analytics_report_users WHERE report_id = %s", (row[0],))
            for username in clean["users"]:
                cur.execute("INSERT INTO self_analytics_report_users (report_id, username) VALUES (%s, %s)",
                            (row[0], username))
        conn.commit()
    _cache.pop(row[0], None)
    return _row(row, clean["users"])


# ── Import and export ────────────────────────────────────────────────────────
# A report as a file, so a Claude chat can write the SQL and charts and Tom
# pastes or uploads it (format in docs/self_analytics_report_format.md).

EXPORT_KEYS = ["name", "folder", "description", "sql", "charts", "visibility", "users", "show_table"]


def export_report(report: dict) -> dict:
    return {"format": "self_analytics_report", "version": 1, **{k: report.get(k) for k in EXPORT_KEYS}}


def import_reports(payload, user: str, admin: bool, known_users=None) -> list:
    """Save every report in the file. One with the same name and folder as a
    report this person made is updated rather than added twice."""
    items = payload.get("reports") if isinstance(payload, dict) and isinstance(payload.get("reports"), list) else [payload]
    if not items or len(items) > 50:
        raise ValueError("The file must hold one report, or a list of up to 50 under \"reports\".")
    for i, item in enumerate(items):    # check everything before saving anything
        try:
            clean_report(item)
        except ValueError as e:
            raise ValueError(f"Report {i + 1}: {e}")
    mine = {(r["name"].lower(), r["folder"].lower()): r["id"]
            for r in list_reports(user, admin) if r["created_by"] == user}
    saved = []
    for item in items:
        item = {k: item.get(k) for k in EXPORT_KEYS if k in item}
        key = (item["name"].strip().lower(), (item.get("folder") or "").strip().lower())
        if key in mine:
            item["id"] = mine[key]
        saved.append(save_report(item, user, admin, known_users))
    return saved


# ── Cache for saved reports ──────────────────────────────────────────────────
# Kept in this Lambda container's memory: a report opened again within
# cache_minutes is not re-run. Another container may still run it once.
_cache: dict = {}     # report_id -> (sql, run_at_epoch, as_at_text, result)
_CACHE_MAX = 30


def run_report(report: dict, settings: dict, refresh: bool, now_text: str) -> dict:
    hit = _cache.get(report["id"])
    max_age = settings["cache_minutes"] * 60
    if hit and not refresh and hit[0] == report["sql"] and time.time() - hit[1] < max_age:
        return {**hit[3], "as_at": hit[2], "from_cache": True}
    result = run_query(report["sql"], settings)
    if max_age:
        if len(_cache) >= _CACHE_MAX:
            _cache.pop(min(_cache, key=lambda k: _cache[k][1]))
        _cache[report["id"]] = (report["sql"], time.time(), now_text, result)
    return {**result, "as_at": now_text, "from_cache": False}


def delete_report(report_id: int, user: str, admin: bool) -> None:
    with get_prod_pricer_connection() as conn:
        _ensure(conn)
        with conn.cursor() as cur:
            if admin:
                cur.execute("DELETE FROM self_analytics_reports WHERE id = %s", (report_id,))
            else:
                cur.execute("DELETE FROM self_analytics_reports WHERE id = %s AND created_by = %s", (report_id, user))
            if cur.rowcount == 0:
                raise ValueError("Only the person who made this report can delete it.")
        conn.commit()
    _cache.pop(report_id, None)


def get_settings() -> dict:
    with get_prod_pricer_connection() as conn:
        _ensure(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM self_analytics_settings WHERE key = 'page'")
            row = cur.fetchone()
        conn.commit()
    try:
        return clean_settings(json.loads(row[0]) if row else None)
    except ValueError:
        return clean_settings(None)


def save_settings(values: dict, user: str) -> dict:
    cleaned = clean_settings(values)
    with get_prod_pricer_connection() as conn:
        _ensure(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO self_analytics_settings (key, value, updated_by) VALUES ('page', %s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, "
                "updated_at = NOW()", (json.dumps(cleaned), user))
        conn.commit()
    return cleaned


def reset_settings() -> dict:
    with get_prod_pricer_connection() as conn:
        _ensure(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM self_analytics_settings WHERE key = 'page'")
        conn.commit()
    return clean_settings(None)
