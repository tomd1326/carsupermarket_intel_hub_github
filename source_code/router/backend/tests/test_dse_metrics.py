"""DSE page rows and month maths (utils/dse_metrics.py).

The risks covered: a column that is not in the table (or not a number)
reaching the SQL, a wide row carrying long-only fields, a table with two rows
for one date being shown as if it had one, month columns counting the wrong
days at a month end or in a leap year, and today's NULL being read as 0.

Run from source_code/router/backend:
    poetry run pytest tests/test_dse_metrics.py
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from utils import dse_metrics as dm

D = datetime.date
SUMMARY_COLUMNS = {"SNAPSHOT_DATE": "DATE", "VALUATIONS": "NUMBER", "STOCK_RATE": "FLOAT", "NOTE": "TEXT"}
EVENTS_COLUMNS = {"DT": "DATE", "EVENT_NAME": "TEXT", "EVENT_VOLUME": "NUMBER"}


def wide_row(**changes):
    row = {"id": "r_abc", "label": "Valuations", "section": "Enquiries",
           "table": "PRODUCT_SLT_KPIS.DAILY_SUMMARY", "shape": "wide", "date_column": "SNAPSHOT_DATE",
           "metric_column": "VALUATIONS", "agg": "sum", "fmt": "num", "decimals": 0, "shade": "none"}
    row.update(changes)
    return row


def long_row(**changes):
    row = {"id": "r_evt", "label": "Calls", "section": "", "table": "COMPUTE_UX_TRACKING.GA4_DAILY_EVENTS",
           "shape": "long", "date_column": "DT", "split_column": "EVENT_NAME", "metric_name": "call_click",
           "value_column": "EVENT_VOLUME", "agg": "sum", "fmt": "num", "decimals": 0, "shade": "none"}
    row.update(changes)
    return row


# ── Row checks ──────────────────────────────────────────────────────────────

def test_good_rows_pass():
    assert dm.validate_row(wide_row(), SUMMARY_COLUMNS) is None
    assert dm.validate_row(long_row(), EVENTS_COLUMNS) is None


def test_wide_row_column_must_be_a_number_column_of_the_table():
    assert "not a number column" in dm.validate_row(wide_row(metric_column="NOTE"), SUMMARY_COLUMNS)
    assert "not a number column" in dm.validate_row(wide_row(metric_column="MISSING"), SUMMARY_COLUMNS)
    assert "not a number column" in dm.validate_row(wide_row(metric_column="SNAPSHOT_DATE"), SUMMARY_COLUMNS)


def test_wide_row_rejects_long_fields():
    assert "no name or value column" in dm.validate_row(wide_row(value_column="VALUATIONS"), SUMMARY_COLUMNS)


def test_long_row_rejects_metric_column_and_needs_a_name():
    assert "not a metric column" in dm.validate_row(long_row(metric_column="EVENT_VOLUME"), EVENTS_COLUMNS)
    assert "choose the name" in dm.validate_row(long_row(metric_name=""), EVENTS_COLUMNS)


def test_changed_table_is_caught():
    assert "date column" in dm.validate_row(wide_row(date_column="GONE"), SUMMARY_COLUMNS)
    assert "was not found" in dm.validate_row(wide_row(), {})


def test_only_compute_and_product_tables():
    assert "COMPUTE_ or PRODUCT_" in dm.validate_row(wide_row(table="SOURCE_MARKETING.DAILY_SPEND"), SUMMARY_COLUMNS)
    assert "COMPUTE_ or PRODUCT_" in dm.validate_row(wide_row(table="PRODUCT_X.T; DROP"), SUMMARY_COLUMNS)


def test_bad_fields_and_values():
    assert dm.validate_row_shape(wide_row(extra=1)) == "Bad row"
    assert dm.validate_row_shape(wide_row(id="bad id")) == "Bad row id"
    assert "Sum or Average" in dm.validate_row_shape(wide_row(agg="max"))
    assert "decimals" in dm.validate_row_shape(wide_row(decimals=5))
    assert "decimals" in dm.validate_row_shape(wide_row(decimals=True))


def test_repeated_ids_rejected():
    assert dm.validate_rows([wide_row(), wide_row()]) == "Two rows have the same id"
    assert dm.validate_rows([wide_row(), long_row()]) is None


def test_clean_row_drops_other_shape_fields():
    assert "split_column" not in dm.clean_row(wide_row(split_column=""))
    assert "metric_column" not in dm.clean_row(long_row())


def test_group_keys():
    a = wide_row(id="r_a")
    b = wide_row(id="r_b", metric_column="STOCK_RATE")
    assert dm.group_key(a) == dm.group_key(b)
    assert dm.group_key(long_row()) != dm.group_key(long_row(value_column="OTHER"))
    assert dm.metric_key(a) == "VALUATIONS"
    assert dm.metric_key(long_row()) == "call_click"


def test_duplicate_dates_found():
    rows = [(D(2026, 10, 1), 1), (D(2026, 10, 2), 2), (datetime.datetime(2026, 10, 2, 9), 3)]
    assert dm.duplicate_dates(rows) == [D(2026, 10, 2)]
    assert dm.duplicate_dates(rows[:2]) == []


# ── Settings ────────────────────────────────────────────────────────────────

def test_settings():
    assert dm.validate_settings(dict(dm.DEFAULT_SETTINGS)) is None
    assert dm.validate_settings({"font_size": 99}) is not None
    assert dm.validate_settings({"weekend_bg": "red"}) is not None
    assert dm.validate_settings({"other": 1}) == "Bad settings"
    assert dm.settings_with_defaults({"font_size": 14, "junk": 1})["font_size"] == 14


# ── Date range maths ────────────────────────────────────────────────────────

def daily(first, values):
    return {first + datetime.timedelta(days=i): v for i, v in enumerate(values)}


def test_default_range_is_the_last_30_days_up_to_yesterday():
    start, end, err = dm.parse_range(None, None, 30, D(2026, 10, 22))
    assert (start, end, err) == (D(2026, 9, 23), D(2026, 10, 22), None)
    assert (end - start).days + 1 == 30


def test_range_given_and_bad():
    assert dm.parse_range("2026-10-01", "2026-10-10", 30, D(2026, 10, 22)) == (D(2026, 10, 1), D(2026, 10, 10), None)
    assert dm.parse_range("2026-10-10", "2026-10-01", 30, D(2026, 10, 22))[2] is not None
    assert dm.parse_range("2025-01-01", "2026-10-01", 30, D(2026, 10, 22))[2] is not None
    assert dm.parse_range("nonsense", None, 30, D(2026, 10, 22))[2] is not None


def test_range_with_only_one_end():
    assert dm.parse_range(None, "2026-10-10", 30, D(2026, 10, 22)) == (D(2026, 9, 11), D(2026, 10, 10), None)
    start, end, _ = dm.parse_range("2026-10-01", None, 30, D(2026, 10, 22))
    assert (start, end) == (D(2026, 10, 1), D(2026, 10, 22))


def test_days_year_shift_and_leap_day():
    assert len(dm.range_days(D(2026, 2, 20), D(2026, 3, 5))) == 14
    assert dm.shift_year(D(2028, 2, 29)) == D(2027, 2, 28)
    assert dm.shift_year(D(2026, 10, 1)) == D(2025, 10, 1)
    assert dm.fetch_range(D(2026, 9, 23), D(2026, 10, 22)) == (D(2025, 9, 23), D(2026, 10, 22))
    # a range over a year long reaches back further for the period before it
    start, end = dm.fetch_range(D(2026, 1, 1), D(2026, 12, 31))
    assert start == D(2025, 1, 1) and end == D(2026, 12, 31)


def test_sum_row_with_null_today():
    start, end = D(2026, 10, 1), D(2026, 10, 3)
    data = daily(start, [10, 20, None])                            # today NULL, not 0
    data.update(daily(D(2026, 9, 28), [100, 5, 5]))                # 28, 29, 30 Sep
    data.update(daily(D(2025, 10, 1), [4, 6, 100]))
    v = dm.row_values(data, start, end, "sum", "same_days")
    assert v["days"] == [10.0, 20.0, None]
    assert v["days_counted"] == 2
    assert v["mtd"] == 30 and v["mtd_avg"] == 15
    assert v["prev_mtd"] == 10 and v["prev_mtd_avg"] == 5          # the 2 days before 1 Oct: 29 and 30 Sep
    assert v["ly_mtd"] == 10
    assert v["ly_compare"] == 200


def test_whole_range_compare():
    start, end = D(2026, 10, 1), D(2026, 10, 3)
    data = daily(start, [10, 20])
    data.update(daily(D(2026, 9, 28), [1, 2, 5]))                  # the 3 days before 1 Oct
    v = dm.row_values(data, start, end, "sum", "full_month")
    assert v["prev_mtd"] == 8


def test_avg_row():
    data = daily(D(2026, 10, 1), [10, 20])
    v = dm.row_values(data, D(2026, 10, 1), D(2026, 10, 2), "avg", "same_days")
    assert v["mtd"] == 15 and v["mtd_avg"] == 15


def test_zero_kept_and_no_data():
    v = dm.row_values(daily(D(2026, 10, 1), [0, 0]), D(2026, 10, 1), D(2026, 10, 2), "sum", "same_days")
    assert v["mtd"] == 0 and v["days_counted"] == 2
    assert v["ly_compare"] is None                                  # nothing last year
    empty = dm.row_values({}, D(2026, 10, 1), D(2026, 10, 2), "sum", "same_days")
    assert empty["mtd"] is None and empty["prev_mtd"] is None and empty["days_counted"] == 0


def test_last_year_zero_gives_no_compare():
    data = daily(D(2026, 10, 1), [5])
    data.update(daily(D(2025, 10, 1), [0]))
    assert dm.row_values(data, D(2026, 10, 1), D(2026, 10, 1), "sum", "same_days")["ly_compare"] is None
