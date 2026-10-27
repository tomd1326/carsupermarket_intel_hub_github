"""KPI page data sources (utils/kpi_data_sources.py): Settings > Data Sources
entries, the SQL that reads them, and how the rows become daily figures.

The risks covered: a column name that is not in the table reaching the SQL,
event names losing their case, a missing event being shown as a gap instead
of 0 (or a missing day as a false 0), and a source being removed while a
chart still uses it.

Run from source_code/router/backend:
    poetry run pytest tests/test_kpi_data_sources.py
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from utils import kpi_data_sources as kds

EVENTS_COLUMNS = {"DT": "DATE", "EVENT_NAME": "TEXT", "EVENT_VOLUME": "NUMBER"}
EVENTS = {"id": "ds_ga4_events", "name": "GA4 Events", "table": "COMPUTE_UX_TRACKING.GA4_DAILY_EVENTS",
          "shape": "long", "date_column": "DT", "split_column": "EVENT_NAME", "value_column": "EVENT_VOLUME"}
SUMMARY_COLUMNS = {"DATE": "DATE", "MONTH": "TEXT", "ORDERS": "NUMBER", "AVG_PRICE": "FLOAT"}
SUMMARY = {"id": "ds_summary", "name": "Summary", "table": "PRODUCT_SLT_KPIS.DAILY_SUMMARY",
           "shape": "wide", "date_column": "DATE"}
D1, D2 = datetime.date(2026, 10, 1), datetime.date(2026, 10, 2)


# ── Entries ──────────────────────────────────────────────────────────────────

def test_valid_long_and_wide_entries_pass():
    assert kds.validate_entry(EVENTS, EVENTS_COLUMNS, []) is None
    assert kds.validate_entry(SUMMARY, SUMMARY_COLUMNS, []) is None


def test_column_not_in_table_is_refused():
    bad = {**EVENTS, "value_column": "EVENT_VOLUME; DROP TABLE X"}
    assert kds.validate_entry(bad, EVENTS_COLUMNS, []) is not None
    assert kds.validate_entry({**EVENTS, "split_column": "NOPE"}, EVENTS_COLUMNS, []) is not None


def test_bad_table_name_or_missing_table_is_refused():
    assert kds.validate_entry({**EVENTS, "table": "X; DROP"}, EVENTS_COLUMNS, []) is not None
    assert kds.validate_entry(EVENTS, {}, []) is not None


def test_date_column_must_be_a_date_and_value_a_number():
    assert kds.validate_entry({**EVENTS, "date_column": "EVENT_NAME"}, EVENTS_COLUMNS, []) is not None
    assert kds.validate_entry({**EVENTS, "value_column": "EVENT_NAME"}, EVENTS_COLUMNS, []) is not None


def test_wide_entry_cannot_have_long_columns():
    assert kds.validate_entry({**SUMMARY, "split_column": "MONTH"}, SUMMARY_COLUMNS, []) is not None


def test_repeated_name_is_refused_but_own_name_is_fine():
    assert kds.validate_entry(EVENTS, EVENTS_COLUMNS, [{**SUMMARY, "name": "ga4 events"}]) is not None
    assert kds.validate_entry(EVENTS, EVENTS_COLUMNS, [SUMMARY]) is None


def test_unknown_field_is_refused():
    assert kds.validate_entry({**EVENTS, "sql": "SELECT 1"}, EVENTS_COLUMNS, []) is not None


def test_new_id_is_stable_shape_and_unique():
    assert kds.new_id("GA4 Events", set()) == "ds_ga4_events"
    assert kds.new_id("GA4 Events", {"ds_ga4_events"}) == "ds_ga4_events_2"
    assert kds.is_source_id(kds.new_id("££", set()))


def test_clean_entry_drops_long_fields_from_wide():
    cleaned = kds.clean_entry({**SUMMARY, "name": "  Summary ", "split_column": "", "value_column": ""})
    assert cleaned == {**SUMMARY, "name": "Summary"}


def test_wide_metrics_are_number_columns_other_than_date():
    assert kds.wide_metric_columns(SUMMARY, SUMMARY_COLUMNS) == ["ORDERS", "AVG_PRICE"]


# ── Removal while in use ─────────────────────────────────────────────────────

def test_references_found_anywhere_in_layout():
    layout = {"rows": [
        {"id": "a", "source": "ds_ga4_events", "metric": "Valuation_Lead"},
        {"id": "b", "source": "snowflake", "metric": "ORDERS",
         "multiply_by": {"source": "ds_ga4_events", "metric": "x"}},
        {"id": "c", "chart_type": "stacked", "series": [{"source": "ds_ga4_events", "metric": "y"}]},
        {"id": "d", "source": "snowflake", "metric": "ORDERS"},
    ]}
    assert kds.references(layout, "ds_ga4_events") == 3
    assert kds.references(layout, "ds_other") == 0


def test_metric_with_no_source_counts_as_daily_summary():
    layout = {"rows": [{"id": "a", "metric": "ORDERS"}, {"id": "b", "source": "sharepoint", "metric": "Spend"}]}
    assert kds.references(layout, "snowflake") == 1
    assert kds.references(layout, "sharepoint") == 1


# ── Built-in sources ─────────────────────────────────────────────────────────

FILES = [{"name": "Daily Spend and Enquiries", "enabled": True, "url": "x"},
         {"name": "Autotrader CTR", "enabled": False}]


def test_built_in_lists_the_three_sources_in_order():
    out = kds.built_in_sources({}, {}, [])
    assert [s["id"] for s in out] == ["snowflake", "sharepoint", "autotrader"]


def test_sharepoint_files_listed_with_state_and_no_counts():
    sp = kds.built_in_sources({}, {}, FILES)[1]
    assert sp["files"] == [{"name": "Daily Spend and Enquiries", "enabled": True},
                           {"name": "Autotrader CTR", "enabled": False}]
    assert all(set(f) == {"name", "enabled"} for f in sp["files"])


def test_counts_belong_to_each_source_once():
    layout = {"rows": [{"source": "sharepoint", "metric": "Spend"},
                       {"source": "sharepoint", "metric": "CTR"},
                       {"source": "autotrader", "metric": "at_cost_total"}]}
    uses = {s: kds.references(layout, s) for s in ("snowflake", "sharepoint", "autotrader")}
    out = {s["id"]: s for s in kds.built_in_sources({"sharepoint": 7}, uses, FILES)}
    assert out["sharepoint"]["uses"] == 2 and out["sharepoint"]["metrics"] == 7
    assert out["autotrader"]["uses"] == 1 and out["snowflake"]["uses"] == 0
    assert "files" not in out["snowflake"] and "files" not in out["autotrader"]


def test_added_tables_never_in_built_in():
    assert not any(kds.is_source_id(s["id"]) for s in kds.built_in_sources({}, {}, FILES))


# ── Reading ──────────────────────────────────────────────────────────────────

def test_long_sql_binds_names_and_dates():
    sql, params = kds.fetch_sql(EVENTS, ["Valuation_Lead", "7_Valuation_Confirm_Vehicle"], D1, D2)
    assert "Valuation_Lead" not in sql
    assert params == ["2026-10-01", "2026-10-02", "2026-10-01", "2026-10-02",
                      "Valuation_Lead", "7_Valuation_Confirm_Vehicle"]
    assert sql.count("%s") == len(params)


def test_wide_sql_binds_dates():
    sql, params = kds.fetch_sql(SUMMARY, ["ORDERS"], D1, D2)
    assert params == ["2026-10-01", "2026-10-02"]
    assert sql.count("%s") == 2


def test_long_rows_keep_case_and_fill_missing_events_with_zero():
    rows = [(D1, "Valuation_Lead", 5), (D1, "7_Valuation_Confirm_Vehicle", 9),
            (D2, None, None)]   # the table has D2, but neither event happened
    out = kds.rows_to_by_date(EVENTS, ["Valuation_Lead", "7_Valuation_Confirm_Vehicle"], rows, ["D", "S", "V"])
    assert out[D1] == {"Valuation_Lead": 5, "7_Valuation_Confirm_Vehicle": 9}
    assert out[D2] == {"Valuation_Lead": 0, "7_Valuation_Confirm_Vehicle": 0}


def test_long_rows_leave_out_days_the_table_does_not_have():
    out = kds.rows_to_by_date(EVENTS, ["Valuation_Lead"], [(D1, "Valuation_Lead", 5)], ["D", "S", "V"])
    assert D2 not in out


def test_wide_rows_by_date():
    out = kds.rows_to_by_date(SUMMARY, ["ORDERS"], [(datetime.datetime(2026, 10, 1, 0, 0), 12)], ["d", "orders"])
    assert out == {D1: {"ORDERS": 12}}


# ── Section data source lock ─────────────────────────────────────────────────

def test_sources_used_finds_every_part_of_a_chart():
    row = {"metric": "VALUATIONS", "source": "ds_bcb", "divide_by": {"metric": "ORDERS"},
           "series": [{"metric": "X", "source": "sharepoint"}]}
    assert kds.sources_used(row) == {"ds_bcb", "snowflake", "sharepoint"}


def test_sources_used_ignores_a_calculated_target_reference():
    row = {"metric": "VALUATIONS", "source": "ds_bcb",
           "target_calc": {"reference": {"metric": "ORDERS", "source": "snowflake"}}}
    assert kds.sources_used(row) == {"ds_bcb"}
