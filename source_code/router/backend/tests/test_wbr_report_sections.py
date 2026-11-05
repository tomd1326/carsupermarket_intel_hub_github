"""Weekly Business Review Report view: the sections (routers/wbr_report_sections.py)
and the settings behind them.

The risks covered: a weekly figure divided like a month, a part-done month shown as
a low weekly average, a part-month N-1 shown as if whole, N-1 worked out from the
default recon cost, percent budgets a hundred times too big, the wrong month's budget
on a week, and one failing section taking the others down.

Run from source_code/router/backend:
    poetry run pytest tests/test_wbr_report_sections.py
"""

import datetime
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from routers import wbr_report as wr
from routers import wbr_report_data as data
from routers import wbr_report_sections as sec

K = data.volume_key


def meta(**over):
    report = wr.report_periods(datetime.date(2026, 10, 25), 3, 5)
    return {"update_date": "2026-10-25", **report, "n1_available": True, "autoview_start": "2025-11",
            "weeks_per_month": 4.2, "warnings": [], **over}


def settings(**over):
    return {**wr.clean_chart_settings(None), **over}


def margin(rows):
    frame = pd.DataFrame(rows, columns=["invoice_posted_month", "delivery_week_end", "invoice_posted_date",
                                        "order_acknowledged_month", "order_week_end", "invoice_status",
                                        "supplier_channel", "deliveries", "gpu_msb_pq", "gpu_m_pq"])
    return frame


def row(month="2026-09", week="2026-09-27", date="2026-09-25", order_month="2026-09", order_week="2026-09-27",
        status="STATUS_PAID", channel="B2B2C", deliveries=1, msb=1000.0, gpu_m=500.0):
    return [month, week, date, order_month, order_week, status, channel, deliveries, msb, gpu_m]


def data_for(**over):
    return sec.ReportData(meta=over.pop("meta", meta()), settings=over.pop("settings", settings()), **over)


def period(d, key):
    return next(p for p in d.table_periods if p["key"] == key)


# ── Weekly averages ──────────────────────────────────────────────────────────

def test_a_complete_month_divides_by_the_settings_and_a_week_by_nothing():
    d = data_for(margin=margin([row() for _ in range(300)]))
    assert sec.deliveries_per_week(d, period(d, "2026-09")) == pytest.approx(300 / (30 / 7))
    assert sec.deliveries_per_week(d, period(d, "2026-W39")) == 300
    assert sec._per_week(d, period(d, "2026-09"), 420) == pytest.approx(100)


def test_the_current_month_divides_by_the_weeks_so_far():
    d = data_for()
    assert sec._per_week(d, d.current, 350) == pytest.approx(350 / (25 / 7))


def test_net_orders_leave_out_cancelled_and_gross_orders_do_not():
    d = data_for(margin=margin([row(), row(status="STATUS_CANCELLED"), row(status="STATUS_ACKNOWLEDGED")]))
    assert sec.net_orders(d, period(d, "2026-09")) == 2
    assert sec.gross_orders(d, period(d, "2026-09")) == 3


def test_no_data_is_blank_not_zero():
    d = data_for()
    assert sec.deliveries(d, period(d, "2026-09")) is None
    assert sec.net_orders(d, period(d, "2026-09")) is None
    assert sec.purchases_count(d, period(d, "2026-09")) is None


def test_purchases_count_only_b2c_and_group_by_their_week_end():
    frame = pd.DataFrame([["2026-09", "2026-09-27", "B2C", "B2B2C"], ["2026-09", "2026-09-27", "B2B", "B2B2C"],
                          ["2026-09", "2026-09-20", "B2C", "B2B2C"]],
                         columns=["purchase_month", "purchase_week_end", "sales_channel", "supplier_channel"])
    d = data_for(purchases=frame)
    assert sec.purchases_count(d, period(d, "2026-09")) == 2
    assert sec.purchases_count(d, period(d, "2026-W39")) == 1


# ── Recon ────────────────────────────────────────────────────────────────────

def recon_row(date, goole=1, hull=2, sites=3):
    day = datetime.date.fromisoformat(date)
    return {"date": date, "year_month": date[:7], "week_end": (day + datetime.timedelta(days=7 - day.isoweekday())).isoformat(),
            sec.RECON_GOOLE: goole, sec.RECON_HULL: hull, sec.RECON_SITES: sites}


def test_recon_before_the_file_starts_is_blank_not_part_of_a_month():
    d = data_for(recon=[recon_row("2025-10-13"), recon_row("2025-11-01"), recon_row("2026-09-21")])
    october = sec._month_period("2025-10")
    assert sec.recon_sum(d, october, sec.RECON_GOOLE) is None
    assert sec.recon_sum(d, sec._month_period("2025-11"), sec.RECON_GOOLE) == 1
    assert sec.recon_sum(d, period(d, "2026-W39"), sec.RECON_HULL) == 2


def test_recon_zero_rows_for_dates_to_come_do_not_count_and_dates_after_the_review_are_cut():
    d = data_for(recon=[recon_row("2026-09-21"), recon_row("2026-10-20", 5, 5, 5), recon_row("2026-10-28", 9, 9, 9),
                        recon_row("2026-09-01", 0, 0, 0)])
    assert d.recon_start == "2026-09-21"
    assert sec.recon_sum(d, d.current, sec.RECON_GOOLE) == 5


# ── Stock ────────────────────────────────────────────────────────────────────

def volume(rows, weeks=("2026-08-30", "2026-09-27", "2026-10-04", "2026-10-25")):
    return {"weeks": list(weeks), "rows": rows}


def test_a_month_of_stock_uses_the_week_standing_in_for_its_end():
    key = K("stock new", "Ordered", "retail", "units")
    d = data_for(volume=volume({key: {"2026-09-27": 1, "2026-10-04": 2}}))
    # 30 Sep: the week ending 4 days after it wins over the week 3 days before.
    assert sec.stock_at(d, period(d, "2026-09"), key) == 2
    assert sec.stock_at(d, period(d, "2026-W39"), key) == 1


def test_n1_stock_averages_a_months_two_end_weeks():
    key = K("stock", "For sale %")
    d = data_for(volume=volume({key: {"2025-07-27": 0.4, "2025-08-03": 0.5}}, weeks=("2025-07-27", "2025-08-03")))
    assert sec.stock_n1(d, sec._month_period("2025-07"), key) == pytest.approx(0.45)
    assert sec.stock_at(d, sec._month_period("2025-07"), key) == 0.5


def test_a_week_after_the_review_date_is_not_a_month_end():
    key = K("stock", "Total")
    d = data_for(volume=volume({key: {"2026-11-01": 9}}, weeks=("2026-11-01",)))
    assert sec.stock_at(d, d.current, key) is None


def test_total_is_blank_if_a_part_is_missing():
    keys = [sec._retail("Trade hold"), sec._retail("Ordered"), sec._retail("On sale stock Total", "on_sale"),
            sec._retail("In prep Total", "in_prep"), sec._retail("Upstream stock Total", "upstream")]
    week = {"2026-10-25": 10}
    d = data_for(volume=volume({k: week for k in keys}))
    assert sec.total_b2c(d, period(d, "2026-W43")) == 50
    d = data_for(volume=volume({k: week for k in keys[:-1]}))
    assert sec.total_b2c(d, period(d, "2026-W43")) is None


def test_coverage_divides_weeks_by_7_and_months_by_the_average_month():
    # The Sheet: W39 upstream 366 with 442 delivered = 5.8; Sep 399 with 1,832 = 6.6.
    d = data_for(margin=margin([row() for _ in range(442)]))
    assert sec.coverage_days(d, period(d, "2026-W39"), 366) == pytest.approx(5.8, abs=0.05)
    d = data_for(margin=margin([row() for _ in range(1832)]))
    assert sec.coverage_days(d, period(d, "2026-09"), 399) == pytest.approx(6.6, abs=0.05)


def test_refurb_days_use_the_sites_weekly_production():
    # The Sheet: W39 Sutton Fields 166 in prep, Hull 237 sent = 4.9 days.
    key = sec._retail("Sutton Fields", "in_prep")
    d = data_for(volume=volume({key: {"2026-09-27": 166}}), recon=[recon_row("2026-09-21", 1, 237, 1)])
    assert sec.refurb_days(d, period(d, "2026-W39"), sec.RECON_HULL, "Sutton Fields") == pytest.approx(4.9, abs=0.05)


# ── Budget ───────────────────────────────────────────────────────────────────

def test_percent_budgets_become_fractions():
    lines = data.budget_lines([("Cash Target", "% old stock", True, "2026-10", 10), ("P&L", "Net Deliveries B2C", False, "2026-10", 1891),
                               ("P&L", "No Months", False, None, None)])
    assert lines[("Cash Target", "% old stock")]["2026-10"] == pytest.approx(0.1)
    assert lines[("P&L", "Net Deliveries B2C")]["2026-10"] == 1891
    assert lines[("P&L", "No Months")] == {}


def test_a_weeks_budget_uses_the_month_of_its_sunday():
    # The Sheet: W39 ends 27 Sep, so 490; W40 ends 4 Oct, so 427.
    d = data_for(budget={sec.B_DELIVERIES: {"2026-09": 2100, "2026-10": 1891}})
    assert sec._budget_per_week(d, sec.B_DELIVERIES, period(d, "2026-W39")) == pytest.approx(490)
    assert sec._budget_per_week(d, sec.B_DELIVERIES, period(d, "2026-W40")) == pytest.approx(427)


def test_the_band_chart_keeps_the_budget_going_across_the_gap():
    d = data_for(budget={sec.B_DELIVERIES: {"2026-09": 2100, "2026-10": 1891}}, margin=margin([row()]))
    chart = sec.chart_net_deliveries(d)
    gap = chart["labels"].index("-")
    assert chart["series"]["budget"][gap] == chart["series"]["budget"][gap - 1]
    assert chart["series"]["actual"][gap] is None
    assert chart["axis"] == {"min": 0, "max": 800}


# ── N-1 ──────────────────────────────────────────────────────────────────────

def test_monthly_n1_comes_from_the_accounts_and_weekly_from_the_real_dates():
    d = data_for(accounts={"2025-09": {"Net Deliveries B2C": 300}},
                 margin=margin([row(week="2025-09-28", month="2025-09")]))
    series = sec.chart_net_deliveries(d)["series"]["n1"]
    assert series[2] == pytest.approx(300 / (30 / 7))
    assert series[4] == 1


def test_weekly_gpu_n1_is_blank_before_autoview_data_starts():
    frame = margin([row(week="2025-09-28", month="2025-09", msb=900.0)])
    series = sec.chart_gpu_msb(data_for(margin=frame))["series"]["n1"]
    assert series[4] is None
    series = sec.chart_gpu_msb(data_for(margin=frame, meta=meta(autoview_start="2025-01")))["series"]["n1"]
    assert series[4] == 900


def test_n1_from_the_sales_data_is_blank_when_outside_the_data_kept():
    frame = margin([row(week="2025-09-28", month="2025-09")])
    assert sec.chart_net_deliveries(data_for(margin=frame, meta=meta(n1_available=False)))["series"]["n1"][4] is None


def test_week_53_has_no_n1():
    report = wr.report_periods(datetime.date(2027, 1, 3), 3, 5)
    week = next(w for w in report["weeks"] if w["label"] == "W53")
    assert sec.n1_period(sec._week_period(week)) is None


# ── Trend ────────────────────────────────────────────────────────────────────

def trend_line(d, weeks, months=(100, 100, 100, 100)):
    keys = [m["key"] for m in d.months + [d.current]]
    values = dict(zip(keys, months))
    values.update(dict(zip([w["key"] for w in d.weeks], weeks)))
    return sec.add_trend(d, {"values": values})["trend"]


def test_trend_compares_with_the_week_before_by_default():
    # The Sheet's Calls row: 1,919 against 1,852 is up, though the months average 1,965.
    d = data_for()
    assert trend_line(d, [1, 1, 1, 1852, 1919], (1974, 1995, 1977, 1914)) == "up"


def test_trend_can_compare_with_the_months_shown():
    d = data_for(settings=settings(trend_basis="months"))
    assert trend_line(d, [1, 1, 1, 1852, 1919], (1974, 1995, 1977, 1914)) == "down"


def test_trend_is_blank_without_figures():
    assert trend_line(data_for(), [1, 1, 1, None, 5]) == ""


# ── Sections ─────────────────────────────────────────────────────────────────

def report_for(sections):
    return {"meta": meta(), "sections": [{"id": s, "title": s, "tables": [], "charts": [], "notes": [],
                                          "more": {"tables": [], "charts": []}} for s in sections]}


def test_channel_mix_adds_up_to_all_deliveries():
    frame = margin([row(), row(channel="C2B2C - PX"), row(channel="C2B2C - Other"), row(channel="Consignment")])
    out = sec.section_deliveries_objective(data_for(margin=frame))
    rows = {r["label"]: r["values"] for r in out["tables"][1]["rows"]}
    assert rows["Net Deliveries B2C"]["2026-09"] == 4
    assert rows["% Mix B2B2C"]["2026-09"] == pytest.approx(0.25)
    assert rows["% Mix Deliveries"]["2026-09"] == pytest.approx(1)


def test_month_to_date_stops_at_the_review_date_and_budget_runs_to_month_end():
    frame = margin([row(month="2026-10", date="2026-10-01"), row(month="2026-10", date="2026-10-20")])
    d = data_for(margin=frame, budget={sec.B_DELIVERIES: {"2026-10": 3100}})
    series = {s["name"]: s["data"] for s in sec.chart_mtd_deliveries(d)["series"]}
    assert series["Actual"][0] == 1 and series["Actual"][24] == 2 and series["Actual"][25] is None
    assert series["Budget Run Rate"][30] == pytest.approx(3100)


def test_every_section_builds_with_no_data_at_all():
    report = report_for([s for s, _ in wr.SECTIONS])
    sec.fill_sections(report, {}, settings())
    built = {s["id"]: s for s in report["sections"]}
    assert built["overview"]["charts"] and built["inventories"]["tables"]
    assert built["pnl"]["tables"] == []
    assert not report["meta"]["warnings"]


def test_a_section_that_fails_is_a_warning_and_the_others_still_build(monkeypatch):
    def broken(_d):
        raise RuntimeError("boom")
    monkeypatch.setitem(sec.BUILDERS, "volumes", broken)
    report = report_for(["volumes", "overview"])
    sec.fill_sections(report, {}, settings())
    assert any("boom" in w for w in report["meta"]["warnings"])
    assert report["sections"][1]["charts"]


def test_an_unknown_supplier_channel_is_a_warning():
    report = report_for(["overview"])
    sec.fill_sections(report, {"margin": margin([row(channel="New Channel")])}, settings())
    assert any("New Channel" in w for w in report["meta"]["warnings"])


def test_the_saved_note_and_the_trend_footnote_both_show():
    report = report_for(["volume_trends"])
    chosen = settings()
    chosen["notes"] = {"volume_trends": "Typed Note"}
    sec.fill_sections(report, {}, chosen)
    assert report["sections"][0]["notes"][0] == "Typed Note"
    assert "Trend" in report["sections"][0]["notes"][1]


def test_nan_reaches_the_page_as_blank():
    assert sec._clean(float("nan")) is None
    assert sec._clean(float("inf")) is None
    assert sec._clean("<10 Days") == "<10 Days"


# ── Settings ─────────────────────────────────────────────────────────────────

def test_chart_settings_fill_in_what_is_missing():
    out = wr.clean_chart_settings({"charts": {"gpu_msb": {"max": 6000}}})
    assert out["charts"]["gpu_msb"]["max"] == 6000
    assert out["charts"]["gpu_msb"]["min"] == -4000
    assert out["charts"]["net_orders"]["max"] == 700


def test_chart_settings_ignore_a_chart_no_longer_there():
    assert "gone" not in wr.clean_chart_settings({"charts": {"gone": {"max": 1}}})["charts"]


@pytest.mark.parametrize("raw", [
    {"charts": {"gpu_msb": {"min": 10, "max": 5}}},
    {"charts": {"gpu_msb": {"max": "lots"}}},
    {"coverage_targets": {"total": "x"}},
    {"constants": {"deliveries_days_per_month": 12}},
    {"trend_basis": "sideways"},
    {"appearance": {"actual": "blue"}},
    {"appearance": {"chart_height": 5000}},
])
def test_bad_chart_settings_are_rejected(raw):
    with pytest.raises(ValueError):
        wr.clean_chart_settings(raw)


def test_a_saved_section_title_replaces_the_default_on_the_report():
    chart_settings = wr.clean_chart_settings({"titles": {"overview": "Summary"}})
    text = {"update_date": "2026-10-25", "last_closed_month": "2026-09", "autoview_start": "2025-11",
            "sections": wr.clean_sections(None), "chart_settings": chart_settings}
    numbers = {"wbr_report_months": 3, "wbr_report_weeks": 5, "wbr_report_weeks_per_month": 4.2,
               "wbr_report_recon_default": 495, "wbr_report_recon_floor": 100}
    out = wr.build_report(numbers, text, datetime.date(2026, 11, 2))
    assert out["sections"][0]["title"] == "Summary"
    assert out["meta"]["appearance"]["chart_height"] == 240


def test_a_title_left_as_the_default_is_not_saved_as_a_change():
    assert wr.clean_chart_settings({"titles": {"overview": "1- Overview"}})["titles"] == {}
