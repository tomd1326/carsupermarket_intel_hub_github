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


def test_value_order_puts_ordered_names_first_and_is_kept():
    assert kds.order_names(["A", "B", "C", "D"], ["C", "A"]) == ["C", "A", "B", "D"]
    assert kds.order_names(["A", "B"], None) == ["A", "B"]
    assert kds.validate_entry({**EVENTS, "name_order": ["x", "y"]}, EVENTS_COLUMNS, []) is None
    assert kds.validate_entry({**EVENTS, "name_order": "x"}, EVENTS_COLUMNS, []) is not None
    assert kds.clean_entry({**EVENTS, "name_order": ["y", "x", "y"]})["name_order"] == ["y", "x"]


def test_records_entry_counts_rows_and_has_no_value_column():
    columns = {"VALUATION_CREATED_DATE": "DATE", "VALUATION_STATUS": "TEXT"}
    records = {"id": "ds_valuations", "name": "Valuations", "table": "PRODUCT_SLT_KPIS.VALUATIONS_BCB",
               "shape": "records", "date_column": "VALUATION_CREATED_DATE", "split_column": "VALUATION_STATUS"}
    assert kds.validate_entry(records, columns, []) is None
    assert kds.validate_entry({**records, "value_column": "VALUATION_STATUS"}, columns, []) is not None
    assert "value_column" not in kds.clean_entry(records)
    sql, _ = kds.fetch_sql(records, ["DELIVERED"], D1, D2)
    assert "COUNT(*)" in sql and "SUM(" not in sql


BCB_COLUMNS = {"VALUATION_CREATED_DATE": "DATE", "VALUATION_STATUS": "TEXT", "CREATED_AT": "TIMESTAMP_NTZ",
               "CALL_AT": "TIMESTAMP_NTZ", "FINAL": "NUMBER", "SYSTEM": "FLOAT", "LISTED_ON": "DATE"}
BCB = {"id": "ds_bcb", "name": "BCB", "table": "PRODUCT_VALUATIONS.VALUATIONS_BCB", "shape": "records",
       "date_column": "VALUATION_CREATED_DATE", "split_column": "VALUATION_STATUS",
       "differences": [{"name": " Mins To Call ", "column_a": "CALL_AT", "column_b": "CREATED_AT", "filters": []},
                       {"name": "Price Gap", "column_a": "FINAL", "column_b": "SYSTEM",
                        "filters": [{"column": "VALUATION_STATUS", "operator": "equals", "value": " DEAD "}]}]}


def test_difference_is_checked_against_the_table():
    assert kds.validate_entry(BCB, BCB_COLUMNS, []) is None
    bad = lambda **change: {**BCB, "differences": [{**BCB["differences"][0], **change}]}
    assert kds.validate_entry(bad(column_a="NOPE; DROP TABLE X"), BCB_COLUMNS, []) is not None
    assert kds.validate_entry(bad(column_a="FINAL"), BCB_COLUMNS, []) is not None    # a number minus a time
    assert kds.validate_entry(bad(column_a="CREATED_AT"), BCB_COLUMNS, []) is not None    # the same column twice
    assert kds.validate_entry(bad(name=" "), BCB_COLUMNS, []) is not None
    assert kds.validate_entry(bad(filters=[{"column": "NOPE", "operator": "equals", "value": "x"}]), BCB_COLUMNS, []) is not None
    assert kds.validate_entry(bad(filters=[{"column": "FINAL", "operator": "equals", "value": " "}]), BCB_COLUMNS, []) is not None
    assert kds.validate_entry(bad(filters=[{"column": "FINAL", "operator": "drop", "value": "x"}]), BCB_COLUMNS, []) is not None
    assert kds.validate_entry(bad(extra=1), BCB_COLUMNS, []) is not None
    same_name = {**BCB, "differences": [BCB["differences"][0], {**BCB["differences"][1], "name": "mins to call"}]}
    assert kds.validate_entry(same_name, BCB_COLUMNS, []) is not None


def test_records_table_with_differences_needs_no_name_column():
    no_name = {k: v for k, v in BCB.items() if k != "split_column"}
    assert kds.validate_entry(no_name, BCB_COLUMNS, []) is None
    assert kds.validate_entry({**no_name, "split_column": ""}, BCB_COLUMNS, []) is None
    assert "split_column" not in kds.clean_entry({**no_name, "split_column": ""})
    # No name column and no difference is fine too: the table's columns can
    # still be charted as their Sum / Average, and the rows counted.
    assert kds.validate_entry({**no_name, "differences": []}, BCB_COLUMNS, []) is None
    assert kds.validate_entry({**no_name, "split_column": "NOPE"}, BCB_COLUMNS, []) is not None
    sql, params = kds.fetch_sql(no_name, ["Price Gap - Total"], D1, D2, BCB_COLUMNS)
    assert "COUNT(*)" not in sql and sql.count("%s") == len(params)
    long_no_name = {k: v for k, v in EVENTS.items() if k != "split_column"}
    assert kds.validate_entry(long_no_name, EVENTS_COLUMNS, []) is not None


def test_one_number_column_needs_no_second_column():
    one = {**BCB, "differences": [{"name": "Final", "column_a": "FINAL", "column_b": "", "filters": []}]}
    assert kds.validate_entry(one, BCB_COLUMNS, []) is None
    assert kds.difference_info(one, BCB_COLUMNS)["Final - Total"]["kind"] == "number"
    assert kds.clean_entry(one)["differences"][0]["column_b"] == ""
    sql, params = kds.fetch_sql(one, ["Final - Total", "Final - Count"], D1, D2, BCB_COLUMNS)
    assert "SUM(FINAL)" in sql and "COUNT(FINAL)" in sql and sql.count("%s") == len(params)
    no_b_key = {**BCB, "differences": [{"name": "Final", "column_a": "FINAL"}]}
    assert kds.validate_entry(no_b_key, BCB_COLUMNS, []) is None
    assert kds.fetch_sql(no_b_key, ["Final - Total"], D1, D2, BCB_COLUMNS)
    a_date_alone = {**BCB, "differences": [{"name": "When", "column_a": "CALL_AT", "column_b": "", "filters": []}]}
    assert kds.validate_entry(a_date_alone, BCB_COLUMNS, []) is not None


def test_count_rows_metric_needs_no_column():
    z001 = {"name": " Z001 Deliveries ", "kind": "count", "column_a": "", "column_b": "",
            "filters": [{"column": "VALUATION_STATUS", "operator": "equals", "value": "Z001"}]}
    counts = {k: v for k, v in BCB.items() if k != "split_column"}
    counts["differences"] = [z001, BCB["differences"][0]]
    assert kds.validate_entry(counts, BCB_COLUMNS, []) is None
    assert kds.validate_entry({**counts, "differences": [{**z001, "column_a": "FINAL"}]}, BCB_COLUMNS, []) is not None
    assert kds.difference_metrics(counts) == ["Z001 Deliveries", "Mins To Call - Total", "Mins To Call - Count"]
    assert kds.count_metrics(counts) == ["Z001 Deliveries"]
    assert "Z001 Deliveries" not in kds.difference_info(counts, BCB_COLUMNS)
    cleaned = kds.clean_entry(counts)["differences"]
    assert cleaned[0] == {"name": "Z001 Deliveries", "kind": "count", "column_a": "", "column_b": "", "filters": z001["filters"]}
    assert cleaned[1]["kind"] == "difference"
    sql, params = kds.fetch_sql(counts, ["Z001 Deliveries"], D1, D2, BCB_COLUMNS)
    assert "COUNT(*) AS V" in sql and "VALUATION_STATUS" in sql and sql.count("%s") == len(params)


def test_metric_kind_is_worked_out_for_older_saved_metrics():
    assert kds.metric_kind({"column_a": "FINAL", "column_b": ""}) == "average"
    assert kds.metric_kind({"column_a": "CALL_AT", "column_b": "CREATED_AT"}) == "difference"
    assert kds.metric_kind({"column_a": "", "column_b": ""}) == "count"
    assert kds.metric_kind({"kind": "count", "column_a": "FINAL"}) == "count"
    one = {**BCB, "differences": [{"name": "Final", "kind": "average", "column_a": "FINAL", "column_b": "SYSTEM", "filters": []}]}
    assert kds.validate_entry(one, BCB_COLUMNS, []) is not None


def test_is_true_and_is_false_filters_need_no_value():
    flagged = {**BCB, "differences": [{"name": "Final", "column_a": "FINAL", "column_b": "", "filters": [
        {"column": "VALUATION_STATUS", "operator": "is_false", "value": "old text"},
        {"column": "VALUATION_STATUS", "operator": "is_true", "value": ""}]}]}
    assert kds.validate_entry(flagged, BCB_COLUMNS, []) is None
    assert [f["value"] for f in kds.clean_entry(flagged)["differences"][0]["filters"]] == ["", ""]
    sql, params = kds.fetch_sql(flagged, ["Final - Total"], D1, D2, BCB_COLUMNS)
    assert "TRY_TO_BOOLEAN(TO_VARCHAR(VALUATION_STATUS)) = FALSE" in sql
    assert "TRY_TO_BOOLEAN(TO_VARCHAR(VALUATION_STATUS)) = TRUE" in sql
    assert sql.count("%s") == len(params)


def test_only_a_records_table_can_have_differences():
    wide = {**SUMMARY, "differences": BCB["differences"]}
    assert kds.validate_entry(wide, SUMMARY_COLUMNS, []) is not None
    assert kds.validate_entry({**EVENTS, "differences": BCB["differences"]}, EVENTS_COLUMNS, []) is not None


def test_difference_kind_follows_the_column_types():
    assert kds.difference_kind("DATE", "DATE") == "days"
    assert kds.difference_kind("TIMESTAMP_NTZ", "DATE") == "minutes"
    assert kds.difference_kind("NUMBER", "FLOAT") == "number"
    assert kds.difference_kind("NUMBER", "DATE") is None


def test_each_difference_is_a_total_and_a_count_metric():
    assert kds.difference_metrics(BCB) == ["Mins To Call - Total", "Mins To Call - Count", "Price Gap - Total", "Price Gap - Count"]
    assert kds.difference_info(BCB, BCB_COLUMNS)["Mins To Call - Total"] == {"count": "Mins To Call - Count", "kind": "minutes"}
    assert kds.difference_info(BCB, BCB_COLUMNS)["Price Gap - Total"]["kind"] == "number"
    assert kds.difference_metrics(SUMMARY) == []


def test_clean_entry_trims_differences_and_drops_them_from_other_shapes():
    cleaned = kds.clean_entry(BCB)
    assert cleaned["differences"][0]["name"] == "Mins To Call"
    assert cleaned["differences"][1]["filters"][0]["value"] == "DEAD"
    assert "differences" not in kds.clean_entry({**BCB, "differences": []})


def test_difference_sql_keeps_status_counts_and_binds_values_in_order():
    sql, params = kds.fetch_sql(BCB, ["DEAD", "Mins To Call - Total", "Price Gap - Count"], D1, D2, BCB_COLUMNS)
    assert "COUNT(*)" in sql
    assert "SUM(DATEDIFF(MINUTE, CREATED_AT, CALL_AT))" in sql
    assert "COUNT((FINAL - SYSTEM))" in sql
    assert "SUM((FINAL - SYSTEM))" not in sql and "COUNT(DATEDIFF" not in sql
    assert sql.count("%s") == len(params)
    day1, day2 = str(D1), str(D2)
    assert params == [day1, day2, day1, day2, "DEAD", "Mins To Call - Total", day1, day2, "Price Gap - Count", day1, day2, " DEAD "]


def test_difference_alone_reads_without_a_status_count():
    sql, params = kds.fetch_sql(BCB, ["Mins To Call - Count"], D1, D2, BCB_COLUMNS)
    assert "COUNT(*)" not in sql and "COUNT(DATEDIFF(MINUTE, CREATED_AT, CALL_AT))" in sql
    assert sql.count("%s") == len(params)
    assert params == [str(D1), str(D2), "Mins To Call - Count", str(D1), str(D2)]


def test_difference_rows_become_daily_figures_with_0_on_a_day_with_none():
    metrics = ["Mins To Call - Total", "Mins To Call - Count"]
    rows = [(D1, "Mins To Call - Total", 90), (D1, "Mins To Call - Count", 3), (D2, None, None)]
    out = kds.rows_to_by_date(BCB, metrics, rows, ["D", "S", "V"])
    assert out[D1] == {"Mins To Call - Total": 90, "Mins To Call - Count": 3}
    assert out[D2] == {"Mins To Call - Total": 0, "Mins To Call - Count": 0}


def test_metric_references_finds_one_metric_wherever_a_chart_uses_it():
    layout = {"rows": [{"source": "ds_bcb", "metric": "Gap - Total", "divide_by": {"source": "ds_bcb", "metric": "Gap - Count"}},
                       {"metric": "Gap - Total"}, {"source": "ds_other", "metric": "Gap - Total"}]}
    assert kds.metric_references(layout, "ds_bcb", "Gap - Total") == 1
    assert kds.metric_references(layout, "ds_bcb", "Gap - Count") == 1
    assert kds.metric_references(layout, "ds_bcb", "Other") == 0


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
