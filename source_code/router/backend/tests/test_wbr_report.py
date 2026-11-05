"""Weekly Business Review Report view: the review's dates and the settings checks
in routers/wbr_report.py, and the new number settings' defaults.

The risks covered: the wrong weeks or months around New Year, week 53 having no
N-1, a closed month read wrongly as a forecast, a bad date being saved, and the
Report view reaching non-admins before it is turned on.

Run from source_code/router/backend:
    poetry run pytest tests/test_wbr_report.py
"""

import datetime
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from routers import kpi
from routers import wbr_report as wr

D = datetime.date


def numbers(**changes):
    return {**kpi._kpi_settings({}), **changes}


def text(**changes):
    base = {"update_date": "", "last_closed_month": "2026-09", "autoview_start": "2025-11",
            "sections": wr.clean_sections(None), "chart_settings": wr.clean_chart_settings(None)}
    return {**base, **changes}


def test_last_sunday():
    assert wr.last_sunday(D(2026, 10, 25)) == D(2026, 10, 25)
    assert wr.last_sunday(D(2026, 10, 26)) == D(2026, 10, 25)
    assert wr.last_sunday(D(2026, 10, 31)) == D(2026, 10, 25)


def test_add_months_crosses_years():
    assert wr.add_months("2026-01", -1) == "2025-12"
    assert wr.add_months("2026-10", -19) == "2025-03"
    assert wr.add_months("2026-12", 1) == "2027-01"


def test_periods_match_the_sheet():
    p = wr.report_periods(D(2026, 10, 25), 3, 5)
    assert p["months"] == ["2026-07", "2026-08", "2026-09"]
    assert p["current_month"] == "2026-10"
    assert [w["label"] for w in p["weeks"]] == ["W39", "W40", "W41", "W42", "W43"]
    assert p["weeks"][-1]["start"] == "2026-10-19"
    assert p["weeks"][-1]["end"] == "2026-10-25"
    assert p["n1_months"]["2026-07"] == "2025-07"


def test_week_of_29_dec_2025_is_2026_w01():
    week = wr.report_periods(D(2026, 1, 4), 3, 1)["weeks"][0]
    assert week["key"] == "2026-W01"
    assert week["start"] == "2025-12-29"
    assert week["n1_start"] == "2024-12-30"


def test_weeks_cross_new_year():
    p = wr.report_periods(D(2027, 1, 10), 3, 3)
    assert [w["key"] for w in p["weeks"]] == ["2026-W52", "2026-W53", "2027-W01"]
    assert p["months"] == ["2026-10", "2026-11", "2026-12"]
    assert p["current_month"] == "2027-01"


def test_week_53_has_no_n1():
    week = wr.report_periods(D(2027, 1, 3), 3, 1)["weeks"][0]
    assert week["key"] == "2026-W53"
    assert week["n1_start"] is None and week["n1_end"] is None


def test_month_kinds_use_the_last_closed_month():
    kinds = wr.month_kinds(["2026-08", "2026-09", "2026-10"], "2026-09")
    assert kinds == {"2026-08": "actual", "2026-09": "actual", "2026-10": "fcst"}
    assert wr.month_kinds(["2026-12", "2027-01"], "2026-12") == {"2026-12": "actual", "2027-01": "fcst"}


def test_n1_window():
    today = D(2026, 11, 2)
    assert wr.n1_in_dbt_window(D(2026, 11, 1), 6, today)
    assert wr.n1_in_dbt_window(D(2026, 10, 25), 6, today)
    assert not wr.n1_in_dbt_window(D(2026, 8, 30), 6, today)


@pytest.mark.parametrize("bad", ["2026-13", "2026-9", "Oct-26", "2026-10-01"])
def test_bad_months_are_rejected(bad):
    with pytest.raises(ValueError):
        wr.clean_month(bad, "Last Closed Month")


def test_blank_month_is_allowed():
    assert wr.clean_month("", "Last Closed Month") == ""
    assert wr.clean_month("2026-09", "Last Closed Month") == "2026-09"


@pytest.mark.parametrize("bad", ["25/10/2026", "2026-10-26", "not a date"])
def test_bad_update_dates_are_rejected(bad):
    with pytest.raises(ValueError):
        wr.clean_update_date(bad)


def test_sections_keep_order_and_add_missing():
    out = wr.clean_sections([{"id": "pnl", "on": False}, {"id": "overview", "on": True}])
    assert out[0] == {"id": "pnl", "on": False}
    assert out[1]["id"] == "overview"
    assert len(out) == len(wr.SECTIONS)
    assert all(s["on"] for s in out[2:])


@pytest.mark.parametrize("bad", ["pnl", [{"id": "nope"}], [{"id": "pnl"}, {"id": "pnl"}], ["pnl"]])
def test_bad_sections_are_rejected(bad):
    with pytest.raises(ValueError):
        wr.clean_sections(bad)


def test_autoview_start_defaults():
    assert wr.clean_report_settings({})["wbr_report_autoview_start"] == "2025-11"


def test_report_is_off_by_default_and_settings_are_in_range():
    settings = kpi._kpi_settings({})
    assert settings["wbr_report_on"] == 0
    assert settings["wbr_report_weeks_per_month"] == 4.2
    assert settings["wbr_report_months"] == 3
    assert settings["wbr_report_weeks"] == 5
    assert kpi._kpi_settings({"wbr_report_months": 99})["wbr_report_months"] == 6
    assert kpi._kpi_settings({"wbr_report_weeks": 0})["wbr_report_weeks"] == 1


def test_build_report_blank_closed_month_warns():
    out = wr.build_report(numbers(), text(last_closed_month=""), D(2026, 11, 2))
    assert out["meta"]["last_closed_month"] == "2026-10"
    assert out["meta"]["update_date"] == "2026-11-01"
    assert any("Last Closed Month" in w for w in out["meta"]["warnings"])


def test_build_report_leaves_out_switched_off_sections():
    sections = [{"id": "pnl", "on": False}] + [s for s in wr.clean_sections(None) if s["id"] != "pnl"]
    out = wr.build_report(numbers(), text(sections=sections), D(2026, 11, 2))
    assert "pnl" not in [s["id"] for s in out["sections"]]
    assert len(out["sections"]) == len(wr.SECTIONS) - 1


def test_build_report_warns_when_n1_is_too_old():
    out = wr.build_report(numbers(wbr_report_months=6), text(update_date="2026-08-30"), D(2026, 11, 2))
    assert out["meta"]["n1_available"] is False
    assert any("N-1" in w for w in out["meta"]["warnings"])


def endpoint(monkeypatch, admin, on, update_date=""):
    monkeypatch.setattr(wr, "is_admin", lambda user: admin)
    monkeypatch.setattr(wr, "_read_settings", lambda: (numbers(wbr_report_on=on), text()))
    monkeypatch.setattr(wr, "add_sources", lambda report, refresh=False: {})
    return wr.weekly_business_review_report(None, update_date, 0, "someone")


def test_non_admin_is_blocked_until_turned_on(monkeypatch):
    assert endpoint(monkeypatch, admin=False, on=0)["status"] == "error"
    assert endpoint(monkeypatch, admin=False, on=1)["status"] == "ok"
    assert endpoint(monkeypatch, admin=True, on=0)["status"] == "ok"


def test_only_admin_can_choose_the_date(monkeypatch):
    chosen = "2026-10-25"
    assert endpoint(monkeypatch, admin=True, on=1, update_date=chosen)["meta"]["update_date"] == chosen
    assert endpoint(monkeypatch, admin=False, on=1, update_date=chosen)["meta"]["update_date"] != chosen
    assert endpoint(monkeypatch, admin=True, on=1, update_date="2026-10-26")["status"] == "error"


def test_only_admin_can_reload(monkeypatch):
    asked = []
    monkeypatch.setattr(wr, "_read_settings", lambda: (numbers(wbr_report_on=1), text()))
    monkeypatch.setattr(wr, "add_sources", lambda report, refresh=False: asked.append(refresh) or {})
    monkeypatch.setattr(wr, "is_admin", lambda user: False)
    wr.weekly_business_review_report(None, "", 1, "someone")
    monkeypatch.setattr(wr, "is_admin", lambda user: True)
    wr.weekly_business_review_report(None, "", 1, "someone")
    assert asked == [False, True]


def test_settings_endpoints_are_admin_only(monkeypatch):
    monkeypatch.setattr(wr, "is_admin", lambda user: False)
    assert wr.weekly_business_review_report_settings(None, "someone")["status"] == "error"
    assert wr.weekly_business_review_report_settings_save(None, {"settings": {}}, "someone")["status"] == "error"
