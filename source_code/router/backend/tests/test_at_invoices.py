"""Autotrader invoice lines turned into per-day figures (utils/at_invoices.py).

Figures are the September 2026 invoice ME0000097437, which Tom checked by hand:
the lines excluding PPC total 154,745.24.

Run from source_code/router/backend:
    poetry run pytest tests/test_at_invoices.py
"""

import datetime
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from utils import at_invoices


def _line(line_type, start, end, slots, charge):
    return {"line_type": line_type, "date_from": datetime.date.fromisoformat(start),
            "date_to": datetime.date.fromisoformat(end), "slots": slots, "charge": Decimal(charge)}


SEPTEMBER = [
    _line("base", "2026-09-01", "2026-09-20", 920, "98397.07"),
    _line("base", "2026-09-21", "2026-09-24", 850, "18438.20"),
    _line("base", "2026-09-25", "2026-09-30", 900, "28992.60"),
    _line("finance", "2026-09-01", "2026-09-20", 920, "1527.20"),
    _line("finance", "2026-09-21", "2026-09-24", 850, "284.47"),
    _line("finance", "2026-09-25", "2026-09-30", 900, "448.20"),
    _line("connect", "2026-09-01", "2026-09-30", None, "1580.00"),
    _line("tactical", "2026-09-18", "2026-09-20", 80, "1283.44"),
    _line("tactical", "2026-09-21", "2026-09-21", 80, "433.84"),
    _line("tactical", "2026-09-24", "2026-09-24", 85, "460.96"),
    _line("tactical", "2026-09-25", "2026-09-30", 90, "2899.26"),
]


def test_flex_rounds_half_up_to_a_whole_slot():
    assert at_invoices.flex_slots(920) == 92
    assert at_invoices.flex_slots(1035) == 104
    assert at_invoices.flex_slots(1067) == 107
    assert at_invoices.flex_slots(1115) == 112


def test_days_add_back_to_the_invoice():
    days = at_invoices.daily_figures(SEPTEMBER)
    assert len(days) == 30
    total = sum(v for d in days.values() for k, v in d.items() if k.startswith("cost_"))
    assert round(total, 2) == 154745.24


def test_finance_adds_cost_but_not_slots():
    day = at_invoices.daily_figures(SEPTEMBER)[datetime.date(2026, 9, 18)]
    assert day["base"] == 920
    assert day["tactical"] == 80
    assert round(day["cost_finance"], 2) == round(1527.20 / 20, 2)


def test_cost_per_slot_per_day_matches_the_sheet():
    day = at_invoices.daily_figures(SEPTEMBER)[datetime.date(2026, 9, 1)]
    assert round(day["cost_base"] / (920 + at_invoices.flex_slots(920)), 3) == 4.862


def test_invoice_rows_split_at_every_change_and_add_back():
    rows = at_invoices.rows_from_lines(SEPTEMBER)
    # Changes on 18th (tactical on), 21st (base 850), 22nd (tactical off),
    # 24th (tactical on), 25th (base 900).
    assert [(r["from"], r["to"]) for r in rows] == [
        ("2026-09-01", "2026-09-17"), ("2026-09-18", "2026-09-20"), ("2026-09-21", "2026-09-21"),
        ("2026-09-22", "2026-09-23"), ("2026-09-24", "2026-09-24"), ("2026-09-25", "2026-09-30")]
    assert all(r["source"] == "invoice" and r["flex_pct"] == 10 for r in rows)
    # Each row's costs are for its own days, so they add straight up to the invoice.
    total = sum(r[k] for r in rows for k in ("me_cost", "finance_cost", "tactical_cost", "connect_cost"))
    assert round(total, 2) == 154745.24


def test_gaps_between_invoices_are_reported():
    october_2025 = [_line("base", "2025-10-10", "2025-10-31", 1000, "100.00")]
    cover = at_invoices.coverage(october_2025 + SEPTEMBER)
    assert cover["from"] == "2025-10-10"
    assert cover["to"] == "2026-09-30"
    assert cover["gaps"] == [["2025-11-01", "2026-08-31"]]
