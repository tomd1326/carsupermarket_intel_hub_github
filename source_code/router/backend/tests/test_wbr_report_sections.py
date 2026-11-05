"""Weekly Business Review Report view: the sections (routers/wbr_report_sections.py)
and the settings behind them.

The risks covered: a weekly figure divided like a month, a part-done month shown as
a low weekly average, percent budgets a hundred times too big, a breakdown that does
not add up to GPU-M, and one failing section taking the others down.

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
    return {"update_date": "2026-10-25", **report, "autoview_start": "2025-11",
            "weeks_per_month": 4.2, "last_closed_month": "2026-09", "warnings": [], **over}


def settings(**over):
    return {**wr.clean_chart_settings(None), **over}


def margin(rows):
    frame = pd.DataFrame(rows, columns=["invoice_posted_month", "delivery_week_end", "invoice_posted_date",
                                        "order_acknowledged_month", "order_week_end", "invoice_status",
                                        "supplier_channel", "deliveries", "gpu_msb", "gpu_m"])
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


def test_a_month_with_two_end_weeks_takes_the_later():
    key = K("stock", "For sale %")
    d = data_for(volume=volume({key: {"2025-07-27": 0.4, "2025-08-03": 0.5}}, weeks=("2025-07-27", "2025-08-03")))
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
    return {"meta": meta(), "sections": [{"id": s, "title": s, "tables": [], "notes": [], "more": {"tables": []}}
                                         for s in sections]}


def test_channel_mix_adds_up_to_all_deliveries():
    frame = margin([row(), row(channel="C2B2C - PX"), row(channel="C2B2C - Other"), row(channel="Consignment")])
    out = sec.section_deliveries_objective(data_for(margin=frame))
    rows = {r["label"]: r["values"] for r in out["tables"][1]["rows"]}
    assert rows["Net Deliveries B2C"]["2026-09"] == 4
    assert rows["% Mix B2B2C"]["2026-09"] == pytest.approx(0.25)
    assert rows["% Mix Deliveries"]["2026-09"] == pytest.approx(1)


def test_every_section_builds_with_no_data_at_all():
    report = report_for([s for s, _ in wr.SECTIONS])
    sec.fill_sections(report, {}, settings())
    built = {s["id"]: s for s in report["sections"]}
    for section, _ in wr.SECTIONS:
        # These two hold only the KPI charts chosen for them.
        assert built[section]["tables"] or section in ("overview", "volumes"), section
    assert not report["meta"]["warnings"]


def test_a_section_that_fails_is_a_warning_and_the_others_still_build(monkeypatch):
    def broken(_d):
        raise RuntimeError("boom")
    monkeypatch.setitem(sec.BUILDERS, "pnl", broken)
    report = report_for(["pnl", "financing"])
    sec.fill_sections(report, {}, settings())
    assert any("boom" in w for w in report["meta"]["warnings"])
    assert report["sections"][1]["tables"]


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

def test_settings_fill_in_what_is_missing():
    out = wr.clean_chart_settings({"coverage_targets": {"total": 40}})
    assert out["coverage_targets"]["total"] == 40
    assert out["coverage_targets"]["upstream"] == 5


def test_settings_saved_for_the_old_report_charts_are_ignored():
    out = wr.clean_chart_settings({"charts": {"gpu_msb": {"y_max": 1}}, "data_labels": False, "appearance": {"actual": "blue"}})
    assert "charts" not in out and "data_labels" not in out
    assert "actual" not in out["appearance"]


@pytest.mark.parametrize("raw", [
    {"coverage_targets": {"total": "x"}},
    {"constants": {"deliveries_days_per_month": 12}},
    {"trend_basis": "sideways"},
    {"appearance": {"chart_height": 5000}},
    {"appearance": {"chart_width": 50}},
])
def test_bad_chart_settings_are_rejected(raw):
    with pytest.raises(ValueError):
        wr.clean_chart_settings(raw)


def test_a_saved_section_title_replaces_the_default_on_the_report():
    chart_settings = wr.clean_chart_settings({"titles": {"overview": "Summary"}})
    text = {"update_date": "2026-10-25", "last_closed_month": "2026-09", "autoview_start": "2025-11",
            "sections": wr.clean_sections(None), "chart_settings": chart_settings}
    numbers = {"wbr_report_months": 3, "wbr_report_weeks": 5, "wbr_report_weeks_per_month": 4.2}
    out = wr.build_report(numbers, text, datetime.date(2026, 11, 2))
    assert out["sections"][0]["title"] == "Summary"
    assert out["meta"]["appearance"]["chart_height"] == 335


def test_a_title_left_as_the_default_is_not_saved_as_a_change():
    assert wr.clean_chart_settings({"titles": {"overview": "1- Overview"}})["titles"] == {}


# ── The KPI charts chosen for each section ───────────────────────────────────

def test_report_charts_keep_only_the_fields_saved_and_drop_empty_sections():
    out = wr.clean_report_charts({"overview": [{"id": "r1", "ref": "cmv", "period": "m", "title": " Total Stock ", "extra": 1}],
                                  "pnl": []})
    assert out == {"overview": [{"id": "r1", "ref": "cmv", "period": "m", "title": "Total Stock"}]}


def test_the_same_kpi_chart_can_be_in_two_sections():
    out = wr.clean_report_charts({"overview": [{"id": "r1", "ref": "cmv", "period": "m"}],
                                  "volumes": [{"id": "r2", "ref": "cmv", "period": "m"}]})
    assert set(out) == {"overview", "volumes"}


@pytest.mark.parametrize("raw", [
    None,
    {"nowhere": []},
    {"overview": "x"},
    {"overview": ["x"]},
    {"overview": [{"id": "r1", "ref": "cmv", "period": "y"}]},
    {"overview": [{"id": "r1", "ref": "cmv", "period": "d"}]},
    {"overview": [{"id": "", "ref": "cmv", "period": "m"}]},
    {"overview": [{"id": "r1", "ref": "cmv", "period": "m"}, {"id": "r2", "ref": "cmv", "period": "m"}]},
    {"overview": [{"id": "r1", "ref": "cmv", "period": "m"}], "pnl": [{"id": "r1", "ref": "other", "period": "m"}]},
    {"overview": [{"id": f"r{n}", "ref": f"c{n}", "period": "m"} for n in range(41)]},
])
def test_bad_report_charts_are_rejected(raw):
    with pytest.raises(ValueError):
        wr.clean_report_charts(raw)


def test_the_recon_file_feeds_kpi_metrics_without_its_header_lists_being_read_as_renames(monkeypatch):
    from routers import kpi
    recon = {"source_key": "wbr_recon", "name": "WBR Reconditioning", "date_header": None,
             "column_names": {"Sites Live": ["Sites Live", "Sites"]}}
    spend = {"source_key": "spend", "name": "Daily Spend", "date_header": "Day", "column_names": {}}
    sps = kpi.sharepoint_sources
    monkeypatch.setattr(sps, "sources_of_type", lambda kind: [recon] if kind == "wbr_recon" else [spend])
    monkeypatch.setattr(sps, "header_names", lambda key: {"Date": ["Date"], "Sites Live": ["Sites Live"]})
    sources = kpi._kpi_sharepoint_sources()
    assert sources[0] is spend
    chosen = sources[1]
    assert chosen["date_header"] == "Date" and chosen["column_names"] == {}
    today = datetime.date.today().isoformat()
    parsed = kpi._parse_kpi_copy(chosen, [["Date", "Sites Live", "Note"], [today, 12, "Text"]])
    assert parsed["columns"] == ["Sites Live"]
    assert parsed["by_date"][today] == {"Sites Live": 12.0}


# ── Margin sections, through the same steps as the real data ─────────────────

def raw_row(**over):
    base = {column: 0 for column in data._MARGIN_NUMBERS}
    base.update({"invoice_status": "STATUS_PAID", "order_acknowledged_month": "2026-09", "order_acknowledged_date": "2026-09-20",
                 "invoice_posted_month": "2026-09", "invoice_posted_date": "2026-09-25", "supplier_name": "ACME",
                 "supplier_channel": "B2B2C", "finance_company": "Black Horse", "deal_site_clean": "Hessle", "payment_type": "Cash",
                 "delivery_volume": 1, "purchase_price_ex_vat": 10000, "recon_cost": 500, "gpu_m": -500})
    base.update(over)
    return base


def frame_of(*rows):
    return data.margin_frame(pd.DataFrame(rows), "2026-10-25")


def by_label(table):
    return {r["label"]: r["values"] for r in table["rows"]}


def test_c2b2c_all_takes_px_and_other_but_not_buy_back():
    frame = frame_of(raw_row(supplier_channel="C2B2C - PX"), raw_row(supplier_channel="C2B2C - Other"),
                     raw_row(supplier_channel="Buy back"), raw_row())
    d = data_for(margin=frame)
    assert sec.deliveries(d, period(d, "2026-09"), "C2B2C*") == 2
    assert sec.deliveries(d, period(d, "2026-09"), "Buy back") == 1


def test_the_channel_mix_counts_buy_back_so_it_adds_up():
    frame = frame_of(raw_row(), raw_row(supplier_channel="Buy back"))
    rows = by_label(sec.section_deliveries_objective(data_for(margin=frame))["tables"][1])
    assert rows["% Mix Deliveries"]["2026-09"] == pytest.approx(1)


def test_gpu_s_lines_add_up_and_admin_fees_are_left_out():
    frame = frame_of(raw_row(finance_commission=500, product_margin_warranty=100, product_margin_gap_rti_insurance=10,
                             product_margin_premia_gap_rti_insurance=20, product_margin_smart_plus_insurance=30,
                             product_margin_service_mot_plan=40, product_margin_services_mots_carried_over=1,
                             product_margin_ceramic_vehicle_protection=50, product_margin_anti_bacterial_protection=2,
                             product_margin_ev_charger=3, product_margin_mats=4, total_product_margin=260,
                             std_finance_debit_backs=-60, product_margin_admin_fee=164, gpu_s=700))
    d = data_for(margin=frame)
    p = period(d, "2026-09")
    products = sum(sec.nature(d, p, columns) for _, columns in sec.PRODUCT_LINES)
    assert products == 260
    assert products + sec.nature(d, p, sec.FINANCING) + sec.nature(d, p, sec.STANDARD_COSTS) == sec.margin_mean(d, p, "gpu_s")


def test_finance_penetration_leaves_out_0_percent_deals():
    frame = frame_of(raw_row(payment_type="Rate", finance_commission=1200), raw_row(payment_type="Cash"),
                     raw_row(payment_type="0_percent"), raw_row(payment_type="Cash"))
    d = data_for(margin=frame)
    p = period(d, "2026-09")
    assert sec.take_rate(d, p) == pytest.approx(1 / 3)
    assert sec.margin_per_finance_contract(d, p) == 1200


def test_a_product_counts_as_taken_if_any_of_its_columns_is_not_nil():
    frame = frame_of(raw_row(product_margin_premia_gap_rti_insurance=20), raw_row())
    d = data_for(margin=frame)
    assert sec.taken_share(d, period(d, "2026-09"), sec.GAP_RTI) == pytest.approx(0.5)


def financing_frame():
    oct_ = {"invoice_posted_month": "2026-10", "invoice_posted_date": "2026-10-20"}
    rows = [raw_row(**oct_, deal_site_clean="Hull", payment_type="Rate", finance_company="Big") for _ in range(3)]
    rows += [raw_row(**oct_, deal_site_clean="Hessle", payment_type="Rate", finance_company="Small")]
    rows += [raw_row(**oct_, deal_site_clean="Hessle", payment_type="Cash")]
    return frame_of(*rows)


def test_sites_are_ranked_by_the_months_deliveries_and_finance_companies_add_to_100():
    chosen = settings()
    chosen["constants"] = {**chosen["constants"], "finance_companies_shown": 1}
    rows = by_label(sec.section_financing(data_for(margin=financing_frame(), settings=chosen))["tables"][0])
    labels = list(rows)
    assert labels.index("1 - Hull") < labels.index("2 - Hessle")
    assert rows["1 - Hull"]["2026-10"] == 1
    assert rows["2 - Hessle"]["2026-10"] == pytest.approx(0.5)
    assert rows["Big"]["2026-10"] == pytest.approx(0.75)
    assert rows["Other"]["2026-10"] == pytest.approx(0.25)
    assert rows["Total"]["2026-10"] == 1


def test_top_suppliers_rank_on_the_month_and_before_takes_earlier_months():
    oct_ = {"invoice_posted_month": "2026-10", "invoice_posted_date": "2026-10-20"}
    frame = frame_of(raw_row(**oct_, supplier_name="A", gpu_m=500),
                     raw_row(**oct_, supplier_name="A", gpu_m=500),
                     raw_row(**oct_, supplier_name="B", gpu_m=500),
                     raw_row(**oct_, supplier_name="C", gpu_m=500),
                     raw_row(supplier_name="A", invoice_posted_month="2026-06", invoice_posted_date="2026-06-10", gpu_m=1500),
                     raw_row(supplier_name="A", invoice_posted_month="2025-06", invoice_posted_date="2025-06-10", gpu_m=8500),
                     raw_row(**oct_, supplier_channel="Consignment", supplier_name="Z"))
    chosen = settings()
    chosen["constants"] = {**chosen["constants"], "top_suppliers": 1}
    table = sec.section_gpu_m_b2b2c(data_for(margin=frame, settings=chosen))["tables"][1]
    rows = by_label(table)
    assert list(rows) == ["A", "Other", "B2B2C"]
    assert rows["A"]["deliv"] == 2 and rows["Other"]["deliv"] == 2 and rows["B2B2C"]["deliv"] == 4
    assert rows["A"]["share"] == pytest.approx(0.5)
    assert rows["A"]["before"] == pytest.approx(rows["A"]["gpu_m"] + 1000)
    assert table["rows"][0]["formats"]["share"] == "pct0"


def breakdown_row(**over):
    return raw_row(**{"sale_price_ex_vat": 15000, "purchase_price_ex_vat": 12000, "product_margin_admin_fee": 164, "discount_ex_vat": 10,
                      "margin_vat_cost": 200, "finance_subsidy": 300, "chassis_margin_after_subsidy": 2654, "other_costs": 20,
                      "std_late_costs": -100, "finance_commission": 500, "product_margin_warranty": 100, "total_product_margin": 100,
                      "std_finance_debit_backs": -50, "gpu_b": 30, "gpu_m": 2034, "gpu_s": 550, "gpu_ms": 2584, "gpu_msb": 2614, **over})


def test_breakdown_lines_add_up_and_only_autoview_lines_are_blank_before_it_starts():
    frame = frame_of(breakdown_row(),
                     raw_row(invoice_posted_month="2025-11", invoice_posted_date="2025-11-10", sale_price_ex_vat=11000,
                             chassis_margin_after_subsidy=1000, gpu_m=500))
    rows = by_label(sec.section_breakdown(data_for(margin=frame))["tables"][0])
    assert rows["Check (Should Be Nil)"]["2026-09"] == pytest.approx(0)
    assert rows["GPU-M"]["2026-09"] == 2034
    assert rows["Apex Vs AutoView Difference £/U"]["2026-09"] == 0
    assert rows["GPU-B"]["2026-09"] == 30
    late = data_for(margin=frame, meta=meta(autoview_start="2026-01"))
    rows = by_label(sec.section_breakdown(late)["tables"][0])
    assert rows["GPU-M"]["2025-11"] == 500
    assert rows["Total Costs Of Sales"]["2025-11"] == -500
    assert rows["AV - Recon Costs £/U"]["2025-11"] is None
    assert rows["Chassis Margin £/U"]["2025-11"] == 1000
    assert rows["Check (Should Be Nil)"]["2025-11"] == pytest.approx(0)


def test_breakdown_shows_apex_costs_autoview_does_not_hold_and_still_adds_up():
    frame = frame_of(breakdown_row(recon_cost=None, gpu_m=2100))
    rows = by_label(sec.section_breakdown(data_for(margin=frame))["tables"][0])
    assert rows["AV - Recon Costs £/U"]["2026-09"] == 0
    assert rows["Apex Vs AutoView Difference £/U"]["2026-09"] == -434    # 2100 - 2654 costs -554, of which 120 is in AutoView or standard
    assert rows["GPU-M"]["2026-09"] == 2100
    assert rows["Check (Should Be Nil)"]["2026-09"] == pytest.approx(0)


# ── P&L: the Sheet's figures for 2026-10 and 2026-08 ─────────────────────────

OCT = {"Net Deliveries B2C": 1800, "GPU-M (£/unit)": 760.30, "GPU-S (£/unit)": 770.94, "GPU-B (£/unit)": 22.44, "GPU-MSB (£/unit)": 1553.68,
       "Revenue k£": 30482.52, "COCA k£": -510, "Sales costs k£": -1574.05, "HQ costs k£": -408.59, "Depreciation k£": -106.44,
       "IFRS impacts to EBIT k£": 183, "IFRS impacts to EBIT/EBITDA k£": -149}
AUG = {"Net Deliveries B2C": 1866, "GPU-MSB (£/unit)": 1488, "GPU-M (£/unit)": 703, "GPU-S (£/unit)": 748, "GPU-B (£/unit)": 37,
       "Revenue k£": 30680, "COCA k£": -602, "Sales costs k£": -1597, "HQ costs k£": -415, "Depreciation k£": -103,
       "IFRS impacts to EBIT k£": 160, "IFRS impacts to EBIT/EBITDA k£": -133}


def test_pnl_estimate_gives_the_sheets_figures():
    column = sec.pnl_column([OCT], current_is_estimate=True)
    assert column["EBITDA k£"] == pytest.approx(487, abs=1)
    assert column["EBIT k£"] == pytest.approx(232, abs=1)
    assert column["Overheads k£"] == pytest.approx(-1800, abs=1)
    assert column["SG&A"] == pytest.approx(-2310, abs=1)
    assert column["GPU-MS (£/unit)"] == pytest.approx(1531, abs=1)
    assert column["EBITDA %"] == pytest.approx(0.016, abs=0.0005)


def test_pnl_closed_month_uses_the_sheets_other_overheads_formula():
    column = sec.pnl_column([AUG])
    assert column["Overheads k£"] == pytest.approx(-1822, abs=1)
    # Within 2: these inputs are the Sheet's rounded figures.
    assert column["EBIT k£"] == pytest.approx(88, abs=2)
    assert column["EBITDA k£"] == pytest.approx(323, abs=2)


def test_pnl_budget_takes_ebit_from_the_budget_lines():
    column = sec.pnl_column([{**OCT, "EBIT k£": 404, "EBITDA k£": 614}], budget=True)
    assert column["EBIT k£"] == 404 and column["EBITDA k£"] == 614


def test_pnl_year_to_date_adds_k_lines_and_weights_unit_lines():
    column = sec.pnl_column([{**OCT, "Net Deliveries B2C": 100, "GPU-M (£/unit)": 100}, {**OCT, "Net Deliveries B2C": 300, "GPU-M (£/unit)": 200}])
    assert column["Net Deliveries B2C"] == 400
    assert column["GPU-M (£/unit)"] == 175
    assert column["COCA k£"] == -1020


def test_pnl_is_blank_not_wrong_when_a_line_is_missing():
    column = sec.pnl_column([{"Net Deliveries B2C": 100}])
    assert column["EBIT k£"] is None and column["Overheads k£"] is None and column["EBITDA %"] is None


def test_the_financial_year_starts_in_the_setting_month():
    assert sec._fiscal_months(data_for()) == ["2026-10"]
    report = wr.report_periods(datetime.date(2027, 1, 31), 3, 5)
    d = data_for(meta={**meta(), **report, "update_date": "2027-01-31"})
    assert sec._fiscal_months(d) == ["2026-10", "2026-11", "2026-12", "2027-01"]
    chosen = settings()
    chosen["constants"] = {**chosen["constants"], "fiscal_year_start_month": 1}
    assert sec._fiscal_months(data_for(settings=chosen)) == [f"2026-{m:02d}" for m in range(1, 11)]


def test_sensitivity_is_centred_on_the_estimate_and_ties_to_it():
    estimate = sec.pnl_column([OCT], current_is_estimate=True)
    table = sec.sensitivity_table(data_for(), estimate)
    assert [c["label"] for c in table["columns"]] == ["1,650", "1,700", "1,750", "1,800", "1,850", "1,900", "1,950"]
    assert len(table["rows"]) == 15
    middle = table["rows"][7]
    assert middle["bold"] and middle["values"]["d1800"] == pytest.approx(487, abs=1)
    assert sec.sensitivity_table(data_for(), {})["rows"] == []


# ── Glossary ─────────────────────────────────────────────────────────────────

def test_the_standard_glossary_loads_into_tables():
    tables = sec.glossary_tables(sec.default_glossary())
    assert [t["title"] for t in tables] == ["Margin Lines", "Inventories Status", "Sources Of Data"]
    assert [c["label"] for c in tables[0]["columns"]] == ["Source", "Definition"]
    assert tables[0]["label_heading"] == "Line"
    assert all(t["text"] for t in tables)


def test_a_typed_glossary_replaces_the_standard_one():
    chosen = settings(glossary="# My Words\nTerm | Meaning\nGPU | Margin")
    out = sec.section_glossary(data_for(settings=chosen))
    assert out["tables"][0]["title"] == "My Words"
    assert out["tables"][0]["rows"][0] == {"label": "GPU", "indent": 0, "bold": False, "format": "int", "values": {"c0": "Margin"}}


def test_the_standard_glossary_is_saved_as_blank_so_a_later_change_to_it_shows():
    assert wr.clean_chart_settings({"glossary": sec.default_glossary()})["glossary"] == ""
    assert wr.clean_chart_settings({"glossary": "Typed"})["glossary"] == "Typed"


def test_constants_outside_their_range_are_rejected():
    with pytest.raises(ValueError):
        wr.clean_chart_settings({"constants": {"fiscal_year_start_month": 13}})
