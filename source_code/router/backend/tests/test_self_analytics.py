"""Self-Analytics (utils/self_analytics.py): which SQL may run, how it is sent,
how results are shaped, and the settings and report checks.

The risks covered: a statement that changes data or runs a Snowflake AI
function getting through, two statements run as one, a result too big for
the page, and a bad setting or chart breaking the page.

Run from source_code/router/backend:
    poetry run pytest tests/test_self_analytics.py
"""

import datetime
import decimal
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from utils import self_analytics as sa


# ── Which SQL may run ────────────────────────────────────────────────────────

def test_read_queries_are_allowed():
    assert sa.check_sql("SELECT vrm FROM PROD_CARSUPERMARKET_DB.X.Y WHERE a = 'delete'") is None
    assert sa.check_sql("with a as (select 1 x) select * from a;") is None
    assert sa.check_sql("-- note\n/* drop table */ SELECT 1") is None
    assert sa.check_sql("(SELECT 1) UNION ALL (SELECT 2)") is None


def test_anything_else_is_refused():
    for sql in ["", "   ", "DELETE FROM t", "UPDATE t SET a = 1", "DROP TABLE t", "CREATE TABLE t (a int)",
                "INSERT INTO t SELECT 1", "CALL proc()", "USE ROLE ACCOUNTADMIN", "GRANT SELECT ON t TO r",
                "SELECT 1; DROP TABLE t", "SELECT 1; SELECT 2"]:
        assert sa.check_sql(sql) is not None, sql


def test_semicolons_and_keywords_inside_strings_do_not_count():
    assert sa.check_sql("SELECT 'a; b' AS x, \"weird;name\" FROM t") is None


def test_snowflake_ai_is_refused():
    for sql in ["SELECT SNOWFLAKE.CORTEX.COMPLETE('m', 'hi')", "SELECT AI_COMPLETE('m', 'hi')",
                "select ai_classify(x, ['a']) from t", "SELECT * FROM t WHERE cortex.sentiment(x) > 0"]:
        assert "AI" in sa.check_sql(sql), sql
    # The words inside a string are fine.
    assert sa.check_sql("SELECT 'cortex' AS word") is None


def test_query_is_wrapped_and_capped_one_over():
    sql = sa.runnable_sql("SELECT 1;  ", 500)
    assert sql.startswith("SELECT * FROM (\nSELECT 1\n)") and sql.endswith("LIMIT 501")


# ── Results ──────────────────────────────────────────────────────────────────

def test_results_are_shaped_for_the_page():
    rows = [(decimal.Decimal("12.50"), datetime.date(2026, 8, 3), "Ent", True, datetime.datetime(2026, 8, 3, 9, 0)),
            (decimal.Decimal("3"), datetime.date(2026, 9, 1), None, False, None),
            (None, None, "Moto", None, None)]
    out = sa.shape_result(rows, ["P", "D", "CH", "B", "TS"], limit=2)
    assert out["truncated"] is True and out["row_count"] == 2
    assert out["rows"][0][:4] == [12.5, "2026-08-03", "Ent", True]
    assert out["rows"][1][0] == 3 and isinstance(out["rows"][1][0], int)
    assert [c["type"] for c in out["columns"]] == ["number", "date", "text", "bool", "datetime"]


# ── Settings and reports ─────────────────────────────────────────────────────

def test_settings_defaults_and_limits():
    assert sa.clean_settings(None) == sa.SETTINGS_DEFAULTS
    s = sa.clean_settings({"row_limit": 10 ** 9, "max_response_mb": 50, "palette": ["#zzzzzz", "#112233", 5],
                           "default_grain": "fortnight", "describe_default_agg": "median"})
    assert s["row_limit"] == sa.SETTINGS_LIMITS["row_limit"][1]
    assert s["max_response_mb"] == 6
    assert s["palette"] == ["#112233"]
    assert s["default_grain"] == sa.SETTINGS_DEFAULTS["default_grain"]
    assert s["describe_default_agg"] == "median"


def test_report_check():
    good = {"name": "Prices", "sql": "SELECT 1", "charts": [
        {"type": "line", "x": "INVOICE_POSTED_DATE", "grain": "month",
         "measures": [{"column": "SALE", "agg": "avg", "minus": "COST", "as_pct": 1, "color": "red"}],
         "filters": [{"column": "CH", "op": "=", "value": "Ent"}, {"column": "CH", "op": "drop"}],
         "width": 9, "top_n": -3},
        "not a chart"]}
    r = sa.clean_report(good)
    assert len(r["charts"]) == 1
    c = r["charts"][0]
    assert c["measures"][0]["minus"] == "COST" and c["measures"][0]["as_pct"] is True and c["measures"][0]["color"] == ""
    assert len(c["filters"]) == 1 and c["width"] == 4 and c["top_n"] == 0
    # Sharing: old reports with only the shared tick; chosen people kept only for "chosen".
    assert r["visibility"] == "only_me" and r["users"] == []
    assert sa.clean_report({**good, "shared": True})["visibility"] == "everyone"
    chosen = sa.clean_report({**good, "visibility": "chosen", "users": ["b", "a", "a", 5]})
    assert chosen["users"] == ["a", "b"] and chosen["shared"] is False
    assert sa.clean_report({**good, "visibility": "everyone", "users": ["a"]})["users"] == []
    for bad in [{**good, "name": ""}, {**good, "sql": "DROP TABLE t"}, "x"]:
        try:
            sa.clean_report(bad)
            assert False, bad
        except ValueError:
            pass


def test_who_can_see_a_report():
    r = {"created_by": "tom", "visibility": "chosen", "users": ["jane"]}
    assert sa.can_see(r, "tom", False) and sa.can_see(r, "jane", False) and sa.can_see(r, "x", True)
    assert not sa.can_see(r, "bob", False)
    assert sa.can_see({**r, "visibility": "everyone"}, "bob", False)
    assert not sa.can_see({**r, "visibility": "only_me"}, "jane", False)


def test_export_keeps_only_the_report_fields():
    out = sa.export_report({"id": 3, "name": "N", "sql": "SELECT 1", "charts": [], "created_by": "tom",
                            "visibility": "everyone", "users": []})
    assert out["format"] == "self_analytics_report" and "id" not in out and "created_by" not in out


def test_saved_report_rows_are_reused_until_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr(sa, "run_query", lambda sql, s: calls.append(sql) or {"rows": [[1]], "columns": []})
    sa._cache.clear()
    report = {"id": 7, "sql": "SELECT 1"}
    settings = {**sa.SETTINGS_DEFAULTS, "cache_minutes": 15}
    first = sa.run_report(report, settings, False, "06 Nov 2026 10:00")
    second = sa.run_report(report, settings, False, "06 Nov 2026 10:05")
    assert len(calls) == 1 and second["from_cache"] and second["as_at"] == "06 Nov 2026 10:00"
    sa.run_report(report, settings, True, "06 Nov 2026 10:06")
    sa.run_report({**report, "sql": "SELECT 2"}, settings, False, "06 Nov 2026 10:07")
    assert len(calls) == 3 and not first["from_cache"]
    sa._cache.clear()
