"""Vehicle metrics: checks and SQL. Nothing here reads Snowflake.
Run from source_code/router/backend:
    poetry run pytest tests/test_vehicle_metrics.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from utils import cars_bought as cb
from utils import vehicle_metrics as vm

COLUMNS = {"AT_PTM": "NUMBER", "CAP_ID": "NUMBER", "SALE_DATE": "DATE", "SOLD_AT": "TIMESTAMP_NTZ", "MAKE": "TEXT",
           "MODEL": "TEXT", "STATUS": "TEXT", "PRICE_BAND": "TEXT"}


def _metric(**changes):
    metric = {"id": "m_abc", "label": " % AT PTM ", "description": "", "enabled": True, "table": "PRODUCT_X.SALES",
              "column": "AT_PTM", "calculation": "avg", "ignore_zero": False, "date_column": "SALE_DATE", "date_mode": "rolling",
              "days": 90, "date_from": "", "date_to": "", "filters": [], "match": [{"field": "cap_id", "column": "CAP_ID"}],
              "fallback": [], "min_rows": 3, "format": "pct", "decimals": 1}
    metric.update(changes)
    return metric


def test_a_good_metric_passes_and_is_cleaned():
    assert vm.validate(_metric(), COLUMNS) is None
    out = vm.clean(_metric())
    assert out["label"] == "% AT PTM" and out["days"] == 90 and "date_from" not in out


def test_checks_catch_what_would_go_wrong():
    bad = [
        (_metric(label=" "), "label"),
        (_metric(table="SOURCE_X.RAW"), "COMPUTE_ or PRODUCT_"),
        (_metric(table="PRODUCT_X.SALES; DROP TABLE"), "COMPUTE_ or PRODUCT_"),
        (_metric(column="NOPE"), "choose a column"),
        (_metric(column="STATUS"), "not a number column"),
        (_metric(date_column="STATUS"), "not a date column"),
        (_metric(date_column=""), "date column"),
        (_metric(days=0), "number of days"),
        (_metric(date_mode="fixed"), "From or To"),
        (_metric(date_mode="fixed", date_from="2026-05-01", date_to="2026-01-01"), "after the To"),
        (_metric(date_mode="fixed", date_from="01/05/2026"), "YYYY-MM-DD"),
        (_metric(match=[]), "match each vehicle"),
        (_metric(match=[{"field": "cap_id", "column": ""}]), "match column"),
        (_metric(match=[{"field": "password", "column": "CAP_ID"}]), "bad Match On"),
        (_metric(fallback=[{"field": "make", "column": "NOPE"}]), "match column"),
        (_metric(filters=[{"column": "NOPE", "operator": "equals", "value": "x"}]), "filter column"),
        (_metric(filters=[{"column": "STATUS", "operator": "equals", "value": " "}]), "enter a value"),
        (_metric(filters=[{"column": "STATUS", "operator": "drop", "value": "x"}]), "bad filter"),
        (_metric(min_rows=0), "fewest rows"),
        (_metric(extra=1), "Bad metric"),
    ]
    for metric, words in bad:
        err = vm.validate(metric, COLUMNS)
        assert err and words.lower() in err.lower(), (words, err)


def test_a_text_column_can_be_counted():
    assert vm.validate(_metric(column="STATUS", calculation="count"), COLUMNS) is None


def test_a_missing_table_is_reported():
    assert "not found" in vm.validate(_metric(), {})


def test_read_sql_uses_parameters_for_every_value():
    metric = _metric(ignore_zero=True, filters=[
        {"column": "STATUS", "operator": "equals", "value": "SOLD'; DROP"},
        {"column": "PRICE_BAND", "operator": "in_list", "value": "<10k, 10-12.5k"},
        {"column": "MODEL", "operator": "contains", "value": "Golf"},
        {"column": "AT_PTM", "operator": "at_least", "value": "90"},
        {"column": "MAKE", "operator": "is_not_blank", "value": ""}])
    sql, params = vm.read_sql(metric, metric["match"], "PROD_DB")
    assert "AVG(AT_PTM)" in sql and "FROM PROD_DB.PRODUCT_X.SALES" in sql and "AT_PTM <> 0" in sql
    assert "DATEADD(DAY, -%s, CURRENT_DATE())" in sql and "GROUP BY 1" in sql
    assert "DROP" not in sql
    assert params == [90, "SOLD'; DROP", "<10k", "10-12.5k", "Golf", "90"]
    assert sql.count("%s") == len(params)


def test_read_sql_fixed_dates_and_no_dates():
    fixed = _metric(date_mode="fixed", date_from="2026-01-01", date_to="")
    sql, params = vm.read_sql(fixed, fixed["match"], "DB")
    assert "SALE_DATE::DATE >= %s" in sql and "<=" not in sql and params == ["2026-01-01"]
    whole = _metric(date_mode="all", date_column="")
    sql, params = vm.read_sql(whole, whole["match"], "DB")
    assert "SALE_DATE" not in sql and params == []


def test_key_is_the_same_on_both_sides_for_two_match_columns():
    pairs = [{"field": "make", "column": "MAKE"}, {"field": "model", "column": "MODEL"}]
    sql, _ = vm.read_sql(_metric(), pairs, "DB")
    assert "UPPER(TRIM(TO_VARCHAR(MAKE))) || '|' || UPPER(TRIM(TO_VARCHAR(MODEL)))" in sql
    assert vm.vehicle_key({"make": " Ford", "model": "focus "}, pairs) == "FORD|FOCUS"
    assert vm.vehicle_key({"make": "Ford", "model": ""}, pairs) is None


def test_figure_uses_the_match_then_the_fallback_then_nothing():
    metric = _metric(fallback=[{"field": "make", "column": "MAKE"}], min_rows=3)
    figures, fallback = {"104415": [96.5, 12], "999": [50.0, 2]}, {"FORD": [97.0, 40]}
    hit = vm.figure_for({"cap_id": 104415, "make": "Ford"}, metric, figures, fallback)
    assert (hit["value"], hit["rows"], hit["matched"], hit["level"]) == (96.5, 12, "CAP ID", "match")
    hit = vm.figure_for({"cap_id": 999, "make": "Ford"}, metric, figures, fallback)
    assert (hit["value"], hit["matched"], hit["level"]) == (97.0, "Make", "fallback")
    assert vm.figure_for({"cap_id": 999, "make": "Kia"}, metric, figures, fallback) is None
    assert vm.figure_for({"cap_id": None, "make": None}, metric, figures, fallback) is None


def test_rows_to_figures():
    assert vm.rows_to_figures([("1", 2, 3), (None, 1, 1), ("2", None, 0)]) == {"1": [2.0, 3], "2": [None, 0]}


FIELDS = cb.add_sheet_fields(list(cb.DEFAULT_CUSTOM_FIELDS))
SAVED = vm.clean(_metric())


def test_a_saved_metric_goes_on_the_table_and_comes_off_when_deleted():
    start = [{"column": "VRM", "label": "Reg", "format": "text", "decimals": 0}]
    added = cb.table_columns_after_metric_save(start, FIELDS, [SAVED])
    assert [c["column"] for c in added] == ["VRM", "METRIC:M_ABC"]
    assert added[1]["label"] == "% AT PTM" and added[1]["format"] == "pct"
    renamed = cb.table_columns_after_metric_save(added, FIELDS, [{**SAVED, "label": "New", "decimals": 2}])
    assert (renamed[1]["label"], renamed[1]["decimals"]) == ("New", 2)
    assert [c["column"] for c in cb.table_columns_after_metric_save(added, FIELDS, [])] == ["VRM"]


def test_table_columns_accept_a_metric_only_while_it_exists():
    cols = [{"column": "METRIC:M_ABC", "label": "", "format": "pct", "decimals": 1}]
    assert cb.clean_table_columns(cols, FIELDS, [SAVED])[0]["format"] == "pct"
    assert cb.clean_table_columns(cols, FIELDS, []) == []


def test_est_prep_cost_is_on_the_default_table():
    assert "CUSTOM:EST_PREP_COST" in [c["column"] for c in cb.read_table_columns([], FIELDS)]
