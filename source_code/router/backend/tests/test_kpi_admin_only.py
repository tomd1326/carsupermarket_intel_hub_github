"""KPI charts marked Admins Only, and a ratio's own Sum / Average for its
bottom (routers/kpi.py).

The risks covered: an Admins Only chart, its notes or its review items
reaching another user through the layout; another user reading or changing
an Admins Only chart's targets or vehicles through the target popup's
endpoints (open to everyone while Settings > Targets allows it); and the
layout check letting a bad admin_only or divide_agg through, or refusing a
layout saved before either existed.

Run from source_code/router/backend:
    poetry run pytest tests/test_kpi_admin_only.py
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from routers import kpi

ADMIN = "boss"
OTHER = "someone"
SECTIONS = ["Results"]


def row(row_id="r1", **changes):
    base = {"id": row_id, "type": "custom", "section": "Results", "source": "snowflake",
            "metric": "ORDERS", "agg": "sum", "title": "Orders"}
    return {**base, **changes}


def layout(*rows):
    return {"rows": list(rows)}


@pytest.fixture
def stored(monkeypatch):
    """The kpi_layout values, held in memory, with one admin."""
    values = {
        "layout": layout(row("live"), row("draft", admin_only=True, title="Draft")),
        "notes": [{"id": "n1", "date": "2026-10-01", "title": "All charts", "row_id": None},
                  {"id": "n2", "date": "2026-10-01", "title": "On the draft", "row_id": "draft"}],
        "wbr_items": [{"id": "w1", "ref": "live", "period": "m"}, {"id": "w2", "ref": "draft", "period": "w"}],
        "wbr_deck": {"slides": [{"id": "s1", "title": "", "subtitle": "", "items": [
            {"id": "i1", "kind": "chart", "ref": "live", "period": "m", "x": 0, "y": 0, "w": 4, "h": 4},
            {"id": "i2", "kind": "chart", "ref": "draft", "period": "m", "x": 4, "y": 0, "w": 4, "h": 4}]}]},
        "sections": SECTIONS,
    }
    monkeypatch.setattr(kpi, "is_admin", lambda user: user == ADMIN)
    monkeypatch.setattr(kpi, "_read_kpi_layout_values", lambda keys: {k: values[k] for k in keys if k in values})
    monkeypatch.setattr(kpi, "_read_kpi_layout_value", lambda key: values.get(key))
    monkeypatch.setattr(kpi, "_section_locks", lambda sections, stored=None: {})
    monkeypatch.setattr(kpi, "_write_kpi_layout_value", lambda key, value: values.__setitem__(key, value))
    return values


# ── The layout check ──

def test_layout_saved_before_the_new_fields_still_passes():
    assert kpi._validate_layout(layout(row(), row("r2", divide_by={"source": "snowflake", "metric": "STOCK_ON_SALE"})),
                                SECTIONS) is None


@pytest.mark.parametrize("value", [True, False])
def test_admin_only_true_or_false_passes(value):
    assert kpi._validate_layout(layout(row(admin_only=value)), SECTIONS) is None


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_admin_only_must_be_true_or_false(value):
    assert kpi._validate_layout(layout(row(admin_only=value)), SECTIONS) == "bad admin_only"


@pytest.mark.parametrize("agg", ["sum", "avg"])
def test_divide_agg_on_a_ratio_passes(agg):
    ratio = row(divide_by={"source": "snowflake", "metric": "STOCK_ON_SALE"}, divide_agg=agg)
    assert kpi._validate_layout(layout(ratio), SECTIONS) is None


def test_divide_agg_needs_a_divide_by():
    assert kpi._validate_layout(layout(row(divide_agg="avg")), SECTIONS) == "divide_agg needs divide_by"


def test_divide_agg_must_be_sum_or_average():
    ratio = row(divide_by={"source": "snowflake", "metric": "STOCK_ON_SALE"}, divide_agg="max")
    assert kpi._validate_layout(layout(ratio), SECTIONS) == "bad divide_agg: max"


# ── What the layout sends ──

def test_other_users_are_not_sent_admin_only_charts(stored):
    out = kpi.kpi_layout_get(None, user=OTHER)
    assert out["status"] == "ok"
    assert [r["id"] for r in out["layout"]["rows"]] == ["live"]
    assert [n["id"] for n in out["notes"]] == ["n1"]
    assert [i["id"] for i in out["wbr_items"]] == ["w1"]
    assert [i["id"] for i in out["wbr_deck"]["slides"][0]["items"]] == ["i1"]
    # Nothing in the reply names the draft chart.
    assert "draft" not in repr(out).lower()


def test_the_admin_is_sent_every_chart(stored):
    out = kpi.kpi_layout_get(None, user=ADMIN)
    assert [r["id"] for r in out["layout"]["rows"]] == ["live", "draft"]
    assert len(out["notes"]) == 2
    assert len(out["wbr_items"]) == 2
    assert len(out["wbr_deck"]["slides"][0]["items"]) == 2


def test_nothing_is_dropped_when_no_chart_is_admin_only():
    plain = layout(row("a"), row("b"))
    notes = [{"id": "n", "row_id": "a"}]
    assert kpi._drop_admin_only(plain, notes, [], None) == (plain, notes, [], None)


# ── Target and vehicle endpoints ──

def test_chart_refused_only_for_admin_only_charts_and_other_users(stored):
    assert kpi._chart_refused(OTHER, "draft") == "That chart no longer exists - reload the page"
    assert kpi._chart_refused(OTHER, "live") is None
    assert kpi._chart_refused(ADMIN, "draft") is None
    assert kpi._chart_refused(OTHER, None) is None


def test_row_target_save_refuses_admin_only_chart_for_other_users(stored):
    out = kpi.kpi_row_target_save(None, {"row_id": "draft", "target_mode": "fixed", "value": 5}, user=OTHER)
    assert out == {"status": "error", "message": "That chart no longer exists - reload the page"}
    # The chart was not changed.
    assert "target_mode" not in next(r for r in stored["layout"]["rows"] if r["id"] == "draft")


def test_row_target_save_still_works_on_a_released_chart(stored):
    ratio = row("ratio", divide_by={"source": "snowflake", "metric": "STOCK_ON_SALE"})
    stored["layout"] = layout(ratio)
    out = kpi.kpi_row_target_save(None, {"row_id": "ratio", "target_mode": "fixed", "value": 5}, user=OTHER)
    assert out["status"] == "ok"
    assert out["row"]["ratio_target"] == 5


def test_target_dates_read_refuses_admin_only_chart_for_other_users(stored):
    out = kpi.kpi_get_target_dates(None, row_id="draft", user=OTHER)
    assert out == {"status": "error", "message": "That chart no longer exists - reload the page"}


def test_target_dates_save_refuses_admin_only_chart_for_other_users(stored):
    class Request:
        async def json(self):
            return {"row_id": "draft", "rows": [{"date": "2026-10-01", "value": 1}]}

    out = asyncio.run(kpi.kpi_save_target_dates(Request(), user=OTHER))
    assert out == {"status": "error", "message": "That chart no longer exists - reload the page"}


def test_vehicles_refuses_admin_only_chart_for_other_users(stored):
    out = kpi.kpi_vehicles(None, row_id="draft", start="2026-10-01", end="2026-10-07", user=OTHER)
    assert out == {"status": "error", "message": "Chart not found - reload the page"}
