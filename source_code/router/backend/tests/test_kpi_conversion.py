"""Moving the saved KPI charts and data sources to the new method
(utils/kpi_conversion.py), and the metrics it moves them to.

The risks covered: a column or filter that is not in the table reaching the SQL,
a converted chart showing a different legend name or drawing in another order, a
source that failed its comparison being changed anyway, and running it twice.

Run from source_code/router/backend:
    poetry run pytest tests/test_kpi_conversion.py
"""

import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from utils import kpi_conversion as conv
from utils import kpi_data_sources as kds

D1, D2 = datetime.date(2026, 10, 1), datetime.date(2026, 10, 2)
COLUMNS = {"SOLD_ON": "DATE", "CREATED_AT": "TIMESTAMP_NTZ", "CALLED_AT": "TIMESTAMP_NTZ", "PRICE": "NUMBER",
           "STATUS": "TEXT", "SUPPLIER": "TEXT"}
Z001 = {"column": "SUPPLIER", "operator": "equals", "value": "Z001"}
SALES = {"id": "ds_sales", "name": "Sales", "table": "S.SALES", "shape": "records", "date_column": "SOLD_ON",
         "split_column": "STATUS", "name_order": ["SOLD", "OPEN"],
         "differences": [{"name": "Margin", "kind": "average", "column_a": "PRICE", "column_b": "", "filters": [Z001]},
                         {"name": "Delivered", "kind": "count", "column_a": "", "column_b": "", "filters": [Z001]}]}
EVENTS = {"id": "ds_events", "name": "Events", "table": "S.EVENTS", "shape": "long", "date_column": "SOLD_ON",
          "split_column": "STATUS", "value_column": "PRICE"}
WIDE = {"id": "ds_wide", "name": "Wide", "table": "S.WIDE", "shape": "wide", "date_column": "SOLD_ON"}
PLAIN = {"id": "ds_plain", "name": "Plain", "table": "S.PLAIN", "shape": "records", "date_column": "SOLD_ON"}


def line(row_id, source, metric, **changes):
    return {"id": row_id, "section": "Results", "source": source, "metric": metric, "agg": "sum", "title": row_id, **changes}


def plan(layout, names=None, skip=()):
    return conv.plan_conversion(layout, [SALES, EVENTS, WIDE], names or {}, {}, {},
                                lambda entry: ["OPEN", "SOLD"], skip=skip)


# ── Built metrics ──

def test_the_same_choices_always_give_the_same_metric():
    assert kds.query_metric("avg", "PRICE") == "col:PRICE:avg"
    assert kds.query_metric("count") == "rows:count"
    metric = kds.query_metric("avg", "CALLED_AT", "CREATED_AT", [Z001])
    assert metric == 'q:{"a":"CALLED_AT","agg":"avg","b":"CREATED_AT","f":[{"column":"SUPPLIER","operator":"equals","value":"Z001"}]}'
    assert kds.parse_query_metric(metric) == {"a": "CALLED_AT", "agg": "avg", "b": "CREATED_AT", "f": [Z001]}


def test_the_page_builds_the_same_metric_as_the_server():
    """kpi.js buildQueryMetric writes the keys in this order, and each filter's."""
    js = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'static', 'js', 'kpi.js'), encoding='utf-8').read()
    assert "JSON.stringify({ a: count ? '' : q.a, agg: q.agg, b, f })" in js
    assert re.search(r"\{ column: x\.column, operator: x\.operator,\s+value:", js)


def test_a_name_that_is_not_a_built_metric_is_not_read_as_one():
    for name in ("DEAD", "q:not json", 'q:{"a":"X"}', "q:[1]", None, "Margin - Total"):
        assert kds.parse_query_metric(name) is None


def test_bad_built_metrics_are_refused():
    def error(metric):
        return kds.query_metric_error(metric, PLAIN, COLUMNS)
    assert error(kds.query_metric("avg", "PRICE", filters=[Z001])) is None
    assert error(kds.query_metric("sum", "NOPE; DROP TABLE X")) is not None
    assert error(kds.query_metric("sum", "PRICE", "NOPE")) is not None
    assert error(kds.query_metric("sum", "STATUS")) is not None
    assert error(kds.query_metric("sum", "PRICE", "CREATED_AT")) is not None
    assert error(kds.query_metric("count", filters=[{"column": "NOPE", "operator": "equals", "value": "x"}])) is not None
    assert error(kds.query_metric("count", filters=[{"column": "STATUS", "operator": "drop", "value": "x"}])) is not None
    assert error(kds.query_metric("count", filters=[{"column": "STATUS", "operator": "equals", "value": " "}])) is not None
    assert kds.query_metric_error(kds.query_metric("count", filters=[Z001]), WIDE, COLUMNS) is not None


def test_a_refused_built_metric_never_reaches_the_sql():
    bad = kds.query_metric("sum", "PRICE", filters=[{"column": "NOPE; DROP", "operator": "equals", "value": "x"}])
    good = kds.query_metric("count", filters=[Z001])
    sql, params = kds.fetch_sql(PLAIN, [bad, good], D1, D2, COLUMNS)
    assert "NOPE" not in sql
    assert "TO_VARCHAR(SUPPLIER) = %s" in sql and sql.count("%s") == len(params) and "Z001" in params


def test_a_difference_of_times_averages_in_minutes_with_its_row_count():
    metric = kds.query_metric("avg", "CALLED_AT", "CREATED_AT", [Z001])
    sql, params = kds.fetch_sql(PLAIN, [metric], D1, D2, COLUMNS)
    assert "AVG(DATEDIFF(MINUTE, CREATED_AT, CALLED_AT))" in sql and "COUNT(DATEDIFF(MINUTE, CREATED_AT, CALLED_AT))" in sql
    assert metric + kds.WEIGHT_SUFFIX in params and sql.count("%s") == len(params)
    rows = [(D1, metric, 90.0), (D1, metric + kds.WEIGHT_SUFFIX, 2), (D2, None, None)]
    by_date = kds.rows_to_by_date(PLAIN, [metric], rows, ["D", "S", "V"])
    assert by_date[D1][metric] == 90.0 and by_date[D1][metric + kds.WEIGHT_SUFFIX] == 2
    assert by_date[D2][metric] is None


def test_built_metrics_are_found_anywhere_in_a_layout():
    metric = kds.query_metric("count", filters=[Z001])
    layout = {"rows": [{"id": "a", "source": "ds_plain", "metric": metric},
                       {"id": "b", "series": [{"source": "ds_x", "metric": "PLAIN", "split_at": {"source": "ds_plain", "metric": metric}}]}]}
    assert kds.built_metrics_used(layout) == {("ds_plain", metric)}


# ── Converting ──

def test_every_series_gets_its_kind():
    row = {"id": "s", "section": "Results", "chart_type": "stacked", "source": "ds_wide", "metric": "A", "title": "s",
           "series": [{"source": "ds_wide", "metric": "A"}, {"source": "ds_wide", "metric": "B", "axis": "secondary"},
                      {"source": "ds_wide", "metric": "C", "kind": "line"}]}
    out = plan({"rows": [row]})
    assert [s["kind"] for s in out["layout"]["rows"][0]["series"]] == ["column", "line", "line"]
    assert out["summary"]["series_marked"] == 2


def test_names_become_counts_and_sums_and_keep_their_legend_name():
    count, total = kds.query_metric("count", filters=[{"column": "STATUS", "operator": "equals", "value": "SOLD"}]), \
        kds.query_metric("sum", "PRICE", filters=[{"column": "STATUS", "operator": "equals", "value": "Lead"}])
    out = plan({"rows": [line("a", "ds_sales", "SOLD"), line("b", "ds_events", "Lead")]}, names={"ds_events|Lead": "Leads"})
    assert [r["metric"] for r in out["layout"]["rows"]] == [count, total]
    assert out["metric_names"][f"ds_sales|{count}"] == "SOLD"
    assert out["metric_names"][f"ds_events|{total}"] == "Leads"


def test_a_ratio_of_a_total_over_its_own_count_becomes_the_average():
    row = line("a", "ds_sales", "Margin - Total", agg="avg", ratio_as="dec", divide_agg=None,
               divide_by={"source": "ds_sales", "metric": "Margin - Count"})
    out = plan({"rows": [row]})["layout"]["rows"][0]
    assert out["metric"] == kds.query_metric("avg", "PRICE", filters=[Z001])
    assert not {"divide_by", "divide_agg", "ratio_as"} & set(out)


def test_a_percent_ratio_or_one_with_its_own_target_stays_a_ratio():
    divide = {"source": "ds_sales", "metric": "Margin - Count"}
    rows = [line("a", "ds_sales", "Margin - Total", ratio_as="pct", divide_by=divide),
            line("b", "ds_sales", "Margin - Total", target_mode="fixed", ratio_target=5, divide_by=divide)]
    for out in plan({"rows": rows})["layout"]["rows"]:
        assert out["metric"] == kds.query_metric("sum", "PRICE", filters=[Z001])
        assert kds.parse_query_metric(out["divide_by"]["metric"])["agg"] == "count"


def test_a_count_rows_metric_keeps_its_filters():
    out = plan({"rows": [line("a", "ds_sales", "Delivered")]})["layout"]["rows"][0]
    assert out["metric"] == kds.query_metric("count", filters=[Z001])


def test_series_are_stored_in_the_order_they_draw():
    row = {"id": "s", "section": "Results", "chart_type": "stacked", "source": "ds_sales", "metric": "OPEN", "title": "s",
           "series": [{"source": "ds_sales", "metric": "OPEN"}, {"source": "ds_sales", "metric": "SOLD"}]}
    out = plan({"rows": [row]})["layout"]["rows"][0]
    # The source's Value Order says SOLD first.
    assert [kds.parse_query_metric(s["metric"])["f"][0]["value"] for s in out["series"]] == ["SOLD", "OPEN"]
    assert out["metric"] == out["series"][0]["metric"]


def test_sources_become_just_a_table_and_a_date_column():
    out = plan({"rows": []})
    by_id = {s["id"]: s for s in out["data_sources"]}
    assert by_id["ds_sales"] == {"id": "ds_sales", "name": "Sales", "table": "S.SALES", "shape": "records", "date_column": "SOLD_ON"}
    assert by_id["ds_events"]["shape"] == "records" and "value_column" not in by_id["ds_events"]
    assert by_id["ds_wide"] == WIDE


def test_a_skipped_source_and_its_charts_stay_exactly_as_they_are():
    row = line("a", "ds_sales", "SOLD")
    out = plan({"rows": [row]}, skip=("ds_sales",))
    assert out["layout"]["rows"][0]["metric"] == "SOLD"
    assert next(s for s in out["data_sources"] if s["id"] == "ds_sales") == SALES
    assert "ds_sales" not in out["summary"]["sources_converted"]


def test_running_it_twice_changes_nothing_more():
    first = plan({"rows": [line("a", "ds_sales", "SOLD"), line("b", "ds_sales", "Delivered")]})
    again = conv.plan_conversion(first["layout"], first["data_sources"], first["metric_names"], {}, {}, lambda e: [])
    assert again["layout"] == first["layout"] and again["data_sources"] == first["data_sources"]
    assert again["metric_names"] == first["metric_names"]
    assert again["summary"]["charts_on_new_metrics"] == [] and again["summary"]["series_marked"] == 0


def test_old_charts_saved_back_over_new_ones_are_caught():
    built = kds.query_metric("count", filters=[Z001])
    layout = {"rows": [line("ok", "ds_plain", built), line("wide", "ds_wide", "A"), line("sold", "ds_sales", "SOLD"),
                       line("old", "ds_plain", "DEAD"),
                       {"id": "s", "series": [{"source": "ds_plain", "metric": "Margin - Total"}],
                        "metric": built, "source": "ds_plain"}]}
    assert kds.old_metrics_on_tables(layout, [PLAIN, WIDE, SALES]) == ["DEAD", "Margin - Total"]
    assert kds.old_metrics_on_tables({"rows": [line("ok", "ds_plain", built)]}, [PLAIN]) == []


def test_repairing_keeps_a_chart_added_since_and_needs_the_old_sources():
    """The old names are converted using the sources as they were before."""
    after_overwrite = {"rows": [line("a", "ds_sales", "Delivered"), line("new", "ds_wide", "A")]}
    out = conv.plan_conversion(after_overwrite, [SALES, EVENTS, WIDE], {}, {}, {}, lambda e: [])
    assert [r["id"] for r in out["layout"]["rows"]] == ["a", "new"]
    assert out["layout"]["rows"][0]["metric"] == kds.query_metric("count", filters=[Z001])
    assert out["layout"]["rows"][1]["metric"] == "A"


def test_metric_pairs_lists_what_is_compared_before_writing():
    metric = kds.query_metric("avg", "PRICE", filters=[Z001])
    rows = [line("a", "ds_sales", "Margin - Total", divide_by={"source": "ds_sales", "metric": "Margin - Count"}),
            line("b", "ds_wide", "A"), line("c", "snowflake", "ORDERS"), line("d", "ds_sales", "SOLD")]
    pairs = conv.metric_pairs({"rows": rows}, {s["id"]: s for s in (SALES, WIDE)})
    assert list(pairs) == ["ds_sales"]
    assert pairs["ds_sales"][("Margin - Total", "Margin - Count")] == metric
    assert kds.parse_query_metric(pairs["ds_sales"]["SOLD"])["agg"] == "count"
    json.dumps({str(k): v for k, v in pairs["ds_sales"].items()})
