"""Combo charts and column aggregates on the KPI page.

The risks covered: a chart saved before series kinds existed drawing
differently (a secondary-axis series must still be a line, the rest
columns), a bad kind getting into the layout, a one-row-per-record table's
Sum / Average / Count metrics reaching the SQL with a column that is not in
the table, and an Average's row count not coming back with it (which would
make a month the average of the daily averages).

Run from source_code/router/backend:
    poetry run pytest tests/test_kpi_combo_charts.py
"""

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from routers import kpi
from utils import kpi_data_sources as kds

SALES_COLUMNS = {"INVOICE_DATE": "DATE", "VRM": "TEXT", "PURCHASE_PRICE": "NUMBER", "SALE_PRICE": "FLOAT",
                 "CHASSIS_MARGIN": "NUMBER", "SITE": "TEXT"}
SALES = {"id": "ds_sales", "name": "Sales", "table": "PRODUCT_SALES_PERFORMANCE.TRADE_SALES",
         "shape": "records", "date_column": "INVOICE_DATE"}
D1, D2 = datetime.date(2026, 10, 1), datetime.date(2026, 10, 2)
SECTIONS = ["Results"]


def series(**changes):
    return {"source": "ds_sales", "metric": "col:PURCHASE_PRICE:avg", "label": "", "colour": None, **changes}


def stacked(*rows_series, **changes):
    return {"id": "r1", "type": "custom", "section": "Results", "chart_type": "stacked", "source": "ds_sales",
            "metric": "col:PURCHASE_PRICE:avg", "agg": "sum", "title": "Prices", "series": list(rows_series), **changes}


@pytest.fixture(autouse=True)
def sources(monkeypatch):
    monkeypatch.setattr(kpi, "_data_sources", lambda: [SALES])


# ── Column aggregates ──

def test_records_table_needs_no_name_column_or_difference():
    assert kds.validate_entry(SALES, SALES_COLUMNS, []) is None


def test_every_number_column_is_offered_as_sum_and_average_plus_the_row_count():
    assert kds.column_metrics(SALES, SALES_COLUMNS) == [
        "rows:count",
        "col:PURCHASE_PRICE:sum", "col:PURCHASE_PRICE:avg",
        "col:SALE_PRICE:sum", "col:SALE_PRICE:avg",
        "col:CHASSIS_MARGIN:sum", "col:CHASSIS_MARGIN:avg",
    ]


def test_metric_names_read_plainly():
    assert kds.metric_label("col:PURCHASE_PRICE:avg") == "Average Of PURCHASE_PRICE"
    assert kds.metric_label("col:PURCHASE_PRICE:sum") == "Sum Of PURCHASE_PRICE"
    assert kds.metric_label("rows:count") == "Count Of Rows"
    assert kds.metric_label("Sold") == "Sold"


def test_only_a_listed_column_reaches_the_sql():
    sql, params = kds.fetch_sql(SALES, ["col:PURCHASE_PRICE:avg", "col:VRM:sum", "col:NOPE:sum"], D1, D2, SALES_COLUMNS)
    assert "AVG(PURCHASE_PRICE)" in sql
    assert "VRM" not in sql and "NOPE" not in sql


def test_an_average_comes_with_its_row_count():
    sql, params = kds.fetch_sql(SALES, ["col:PURCHASE_PRICE:avg", "rows:count"], D1, D2, SALES_COLUMNS)
    assert "AVG(PURCHASE_PRICE)" in sql and "COUNT(PURCHASE_PRICE)" in sql and "COUNT(*)" in sql
    assert "col:PURCHASE_PRICE:avg#w" in params
    rows = [(D1, "col:PURCHASE_PRICE:avg", 9000.0), (D1, "col:PURCHASE_PRICE:avg#w", 3), (D1, "rows:count", 4),
            (D2, None, None)]
    by_date = kds.rows_to_by_date(SALES, ["col:PURCHASE_PRICE:avg", "rows:count"], rows, ["D", "S", "V"])
    assert by_date[D1] == {"col:PURCHASE_PRICE:avg": 9000.0, "col:PURCHASE_PRICE:avg#w": 3, "rows:count": 4}
    # A day with rows but no figure for the column: no average, 0 rows counted.
    assert by_date[D2] == {"col:PURCHASE_PRICE:avg": None, "rows:count": 0}


def test_parse_column_metric():
    assert kds.parse_column_metric("col:SALE_PRICE:sum") == ("SALE_PRICE", "sum")
    assert kds.parse_column_metric("rows:count") == ("*", "count")
    assert kds.parse_column_metric("col:sale_price:sum") is None
    assert kds.parse_column_metric("Sold") is None
    assert kds.parse_column_metric(None) is None


# ── Series kinds ──

@pytest.mark.parametrize("kind", ["column", "line"])
def test_a_series_kind_passes(kind):
    assert kpi._validate_layout({"rows": [stacked(series(kind=kind), series(metric="col:SALE_PRICE:avg", kind="line", axis="secondary"))]},
                                SECTIONS) is None


def test_a_series_saved_without_a_kind_still_passes():
    assert kpi._validate_layout({"rows": [stacked(series(), series(axis="secondary"))]}, SECTIONS) is None


def test_a_bad_series_kind_is_refused():
    assert kpi._validate_layout({"rows": [stacked(series(kind="area"))]}, SECTIONS) == "bad series kind"


# ── Layout history ──

class FakeCursor:
    """Records each statement; a statement containing fail_on raises, as a
    database error would. Everything after it is still recorded, so a test
    can see the save carried on."""

    def __init__(self, fail_on=None, history_rows=1):
        self.statements, self.fail_on, self.history_rows = [], fail_on, history_rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.statements.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("history table problem")

    def fetchone(self):
        return (self.history_rows,)


class FakeConnection:
    def __init__(self, cursor):
        self.fake_cursor, self.committed = cursor, False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self.fake_cursor

    def commit(self):
        self.committed = True


def _save_layout(monkeypatch, cursor):
    connection = FakeConnection(cursor)
    monkeypatch.setattr(kpi, "get_prod_pricer_connection", lambda: connection)
    monkeypatch.setattr(kpi, "_layout_history_table_ready", True)
    kpi._write_kpi_layout_values({"layout": {"rows": []}}, saved_by="boss")
    return connection


def test_a_layout_save_keeps_a_copy_in_the_history(monkeypatch):
    cursor = FakeCursor()
    connection = _save_layout(monkeypatch, cursor)
    assert connection.committed
    assert any("INSERT INTO kpi_layout_history" in s for s in cursor.statements)
    assert any("RELEASE SAVEPOINT" in s for s in cursor.statements)
    assert "INSERT INTO kpi_layout " in cursor.statements[-1]


def test_a_history_failure_does_not_stop_the_layout_saving(monkeypatch):
    cursor = FakeCursor(fail_on="INSERT INTO kpi_layout_history")
    connection = _save_layout(monkeypatch, cursor)
    assert connection.committed
    assert any("ROLLBACK TO SAVEPOINT" in s for s in cursor.statements)
    # Gone back to the savepoint before the layout itself is written.
    rollback = next(i for i, s in enumerate(cursor.statements) if "ROLLBACK TO SAVEPOINT" in s)
    assert "INSERT INTO kpi_layout " in cursor.statements[-1] and rollback < len(cursor.statements) - 1
