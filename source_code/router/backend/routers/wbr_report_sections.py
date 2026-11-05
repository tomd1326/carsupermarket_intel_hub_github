"""Weekly Business Review > Report view: one function per section, turning the data
wbr_report_data.py reads into the tables wbr_report.js shows. The charts are KPI
charts, chosen for each section on the page, so nothing here builds one.

Everything here is pure, so it can be tested with small frames. Formulas follow the
old review spreadsheet's tabs; where a figure could not be matched to its formula
the plan's discrepancy list says why. Numbers are sent raw: the page formats them.
"""
import datetime
import logging
import math
import os
from dataclasses import dataclass, field

import pandas as pd

from .wbr_report_data import add_months, autoview_costs_usable, month_end_proxy, volume_key

logger = logging.getLogger(__name__)

SPACER = {"kind": "spacer", "key": "-", "label": "-"}
CANCELLED = "STATUS_CANCELLED"
DAYS_PER_MONTH = 365 / 12

# (supplier_channel, name on the page). Kept in code: these are the values in the data.
CHANNELS = (("B2B2C", "B2B2C"), ("C2B2C - PX", "C2B2C - PX"),
            ("C2B2C - Other", "C2B2C - Other"), ("Consignment", "Consignment"), ("Buy back", "Buy Back"))

STOCK = "stock new"
RECON_GOOLE, RECON_HULL, RECON_SITES = "Goole Sent to Trans.", "Hull Sent to Trans.", "Sites Live"

# Budget lines, as (section, label) on the Budget tab.
B_DELIVERIES = ("P&L", "Net Deliveries B2C")


def _clean(value):
    """A plain number for the page: blank for nothing, NaN or infinity."""
    if value is None or isinstance(value, str):
        return value
    number = float(value)
    return number if math.isfinite(number) else None


def _ratio(top, bottom):
    return top / bottom if top is not None and bottom else None


def _sum_all(*values):
    """The sum, or blank if any part is missing: a part figure would look like a real total."""
    return None if any(v is None for v in values) else sum(values)


# ── The review's periods ─────────────────────────────────────────────────────

def _month_period(month: str) -> dict:
    return {"kind": "month", "key": month, "label": month, "month": month}


def _week_period(week: dict) -> dict:
    return {"kind": "week", "key": week["key"], "label": week["label"], "end": week["end"],
            "start": week["start"], "month": week["end"][:7],
            "n1_start": week["n1_start"], "n1_end": week["n1_end"]}


@dataclass
class ReportData:
    """The review's data and settings, with its rows grouped once so each section
    can ask for a period's rows without searching the whole frame again."""
    meta: dict
    settings: dict
    margin: pd.DataFrame = field(default_factory=pd.DataFrame)
    purchases: pd.DataFrame = field(default_factory=pd.DataFrame)
    daily: pd.DataFrame = field(default_factory=pd.DataFrame)
    recon: list = field(default_factory=list)
    volume: dict = field(default_factory=lambda: {"weeks": [], "rows": {}})
    accounts: dict = field(default_factory=dict)
    budget: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    _groups: dict = field(default_factory=dict)

    def __post_init__(self):
        review = self.meta["update_date"]
        self.months = [_month_period(m) for m in self.meta["months"]]
        self.current = _month_period(self.meta["current_month"])
        self.weeks = [_week_period(w) for w in self.meta["weeks"]]
        self.chart_periods = self.months + [SPACER] + self.weeks
        self.table_periods = self.months + [self.current, SPACER] + self.weeks
        self.review_day = int(review[8:10])
        self.weeks_per_month = float(self.meta["weeks_per_month"])
        self.deliveries_weeks_per_month = float(self.settings["constants"]["deliveries_days_per_month"]) / 7
        self.volume_weeks = [w for w in self.volume["weeks"] if w <= review]
        # Rows of zeros for dates to come are not data, so recon starts at its first real day.
        fields = (RECON_GOOLE, RECON_HULL, RECON_SITES)
        self.recon_rows = [r for r in self.recon if r["date"] <= review]
        real = [r["date"] for r in self.recon_rows if any(r.get(f) for f in fields)]
        self.recon_start = min(real) if real else None

    def rows(self, frame_name: str, column: str, value: str) -> pd.DataFrame:
        frame = getattr(self, frame_name)
        if frame.empty:
            return frame
        key = (frame_name, column)
        if key not in self._groups:
            self._groups[key] = {k: g for k, g in frame.groupby(frame[column].astype(str).str[:10])}
        return self._groups[key].get(value, frame.iloc[0:0])


def _delivered(d: ReportData, p: dict) -> pd.DataFrame:
    if p["kind"] == "month":
        return d.rows("margin", "invoice_posted_month", p["month"])
    return d.rows("margin", "delivery_week_end", p["end"])


def _ordered(d: ReportData, p: dict) -> pd.DataFrame:
    if p["kind"] == "month":
        return d.rows("margin", "order_acknowledged_month", p["month"])
    return d.rows("margin", "order_week_end", p["end"])


def _bought(d: ReportData, p: dict) -> pd.DataFrame:
    if p["kind"] == "month":
        return d.rows("purchases", "purchase_month", p["month"])
    return d.rows("purchases", "purchase_week_end", p["end"])


def _advertised(d: ReportData, p: dict) -> pd.DataFrame:
    if p["kind"] == "month":
        return d.rows("purchases", "date_first_advertised_month", p["month"])
    return d.rows("purchases", "advertised_week_end", p["end"])


def _channel(rows: pd.DataFrame, channel) -> pd.DataFrame:
    """A channel's rows. A name ending * takes every channel starting with it, like the
    spreadsheet's C2B2C* for PX and Other together."""
    if channel is None or rows.empty:
        return rows
    if channel.endswith("*"):
        return rows[rows["supplier_channel"].astype(str).str.startswith(channel[:-1])]
    return rows[rows["supplier_channel"] == channel]


def _mean(rows: pd.DataFrame, column: str):
    return None if rows.empty else _clean(rows[column].mean())


def _weeks_in(d: ReportData, p: dict, per_month: float) -> float:
    """What a period's count is divided by to give a weekly average. The current month
    is part done, so it uses the weeks so far: the fixed figure would understate it."""
    if p["kind"] == "week":
        return 1
    return d.review_day / 7 if p["month"] == d.current["month"] else per_month


# ── Figures ──────────────────────────────────────────────────────────────────

def deliveries(d: ReportData, p: dict, channel=None):
    if d.margin.empty:
        return None
    return float(_channel(_delivered(d, p), channel)["deliveries"].sum())


def deliveries_per_week(d: ReportData, p: dict):
    return _ratio(deliveries(d, p), _weeks_in(d, p, d.deliveries_weeks_per_month))


def gross_orders(d: ReportData, p: dict, channel=None):
    return None if d.margin.empty else float(len(_channel(_ordered(d, p), channel)))


def net_orders(d: ReportData, p: dict, channel=None):
    if d.margin.empty:
        return None
    rows = _channel(_ordered(d, p), channel)
    return float((rows["invoice_status"] != CANCELLED).sum())


def _per_week(d: ReportData, p: dict, value):
    return _ratio(value, _weeks_in(d, p, d.weeks_per_month))


def purchases_count(d: ReportData, p: dict, channel=None):
    if d.purchases.empty:
        return None
    rows = _channel(_bought(d, p), channel)
    return float((rows["sales_channel"] == "B2C").sum())


def recon_sum(d: ReportData, p: dict, column: str):
    """A period's recon count. Blank if it starts before the file does, so a part
    month is never shown as if it were a whole one."""
    start = f"{p['month']}-01" if p["kind"] == "month" else p["start"]
    if not d.recon_start or start < d.recon_start:
        return None
    if p["kind"] == "month":
        rows = [r for r in d.recon_rows if r["year_month"] == p["month"]]
    else:
        rows = [r for r in d.recon_rows if r["week_end"] == p["end"]]
    return float(sum(r.get(column) or 0 for r in rows))


def recon_per_week(d: ReportData, p: dict, *columns):
    parts = [recon_sum(d, p, c) for c in columns]
    return _per_week(d, p, _sum_all(*parts))


def volume_mean(d: ReportData, p: dict, key: tuple):
    """A weekly row over a period: the mean of its weeks, by the month of each week's
    Sunday. Blank cells are left out, as AVERAGEIFS does."""
    row = d.volume["rows"].get(key)
    if not row:
        return None
    weeks = [w for w in d.volume_weeks if w[:7] == p["month"]] if p["kind"] == "month" else [p["end"]]
    values = [row[w] for w in weeks if row.get(w) is not None]
    return sum(values) / len(values) if values else None


def stock_at(d: ReportData, p: dict, key: tuple):
    """A stock row at a period's end. A month uses the week standing in for its end."""
    row = d.volume["rows"].get(key)
    if not row:
        return None
    week = month_end_proxy(d.volume_weeks, p["month"]) if p["kind"] == "month" else p["end"]
    return row.get(week) if week else None


def _retail(label: str, block: str = "", measure: str = "units") -> tuple:
    return volume_key(STOCK, label, "retail", measure, block)


def total_b2c(d: ReportData, p: dict):
    keys = (_retail("Trade hold"), _retail("Ordered"), _retail("On sale stock Total", "on_sale"),
            _retail("In prep Total", "in_prep"), _retail("Upstream stock Total", "upstream"))
    return _sum_all(*[stock_at(d, p, k) for k in keys])


SLEEPING_BLOCKS = (
    ("upstream", "Upstream Stock", (("B2B", "B2B"), ("C2B", "C2B"), ("PX", "PX"), ("Consignment", "Consignment"))),
    ("in_prep", "In Prep", (("Sutton Fields", "Sutton Fields"), ("Goole", "Goole"), ("Sites", "Sites"),
                            ("Subcontractor offsite", "Subcontractor"))),
    ("on_sale", "On Sale Stock", (("B2B", "B2B"), ("C2B", "C2B"), ("PX", "PX"), ("Consignment", "Consignment"))),
)


def sleeping_block(d: ReportData, p: dict, block: str, labels):
    return _sum_all(*[stock_at(d, p, _retail(label, block, "sleeping")) for label, _ in labels])


def sleeping_total(d: ReportData, p: dict):
    blocks = [sleeping_block(d, p, block, labels) for block, _, labels in SLEEPING_BLOCKS]
    return _sum_all(stock_at(d, p, _retail("Trade hold", "", "sleeping")),
                    stock_at(d, p, _retail("Ordered", "", "sleeping")), *blocks)


def coverage_days(d: ReportData, p: dict, units):
    """Stock as days of deliveries. Weeks divide by 7, months by the average month."""
    days = 7 if p["kind"] == "week" else DAYS_PER_MONTH
    return _ratio(units, _ratio(deliveries(d, p), days))


def refurb_days(d: ReportData, p: dict, site_key: str, block_label: str):
    days_of_work = _ratio(stock_at(d, p, _retail(block_label, "in_prep")), recon_per_week(d, p, site_key))
    return days_of_work * 7 if days_of_work is not None else None


def _budget(d: ReportData, line: tuple, month: str):
    return d.budget.get(line, {}).get(month)


# ── Tables ───────────────────────────────────────────────────────────────────

def _columns(periods: list, extra: tuple = ()) -> list:
    columns = []
    for p in periods:
        group = {"month": "months", "week": "weeks", "spacer": "spacer"}[p["kind"]]
        columns.append({"key": p["key"], "label": p["label"], "group": group})
    return columns + [{"key": key, "label": label, "group": "extra"} for key, label in extra]


def _line(label: str, periods: list, figure, fmt: str = "int", indent: int = 0, bold: bool = False, extra: dict = None) -> dict:
    values = {p["key"]: _clean(figure(p)) for p in periods if p["kind"] != "spacer"}
    return {"label": label, "indent": indent, "bold": bold, "format": fmt, "values": {**values, **(extra or {})}}


def _blank_line() -> dict:
    return {"label": "", "indent": 0, "bold": False, "format": "int", "values": {}}


def _table(table_id: str, title: str, periods: list, rows: list, extra: tuple = ()) -> dict:
    return {"id": table_id, "title": title, "columns": _columns(periods, extra), "rows": rows}


def add_trend(d: ReportData, line: dict) -> dict:
    """up or down, from the setting: last week against the week before (as the
    spreadsheet does) or against the average of the month columns (as the slides did)."""
    values = line["values"]
    weeks = [w["key"] for w in d.weeks]
    last = values.get(weeks[-1]) if weeks else None
    if d.settings["trend_basis"] == "months":
        months = [values.get(m["key"]) for m in d.months + [d.current]]
        months = [m for m in months if m is not None]
        base = sum(months) / len(months) if months else None
    else:
        base = values.get(weeks[-2]) if len(weeks) > 1 else None
    line["trend"] = "" if last is None or base is None else ("up" if last > base else "down")
    return line


def _trend_note(d: ReportData) -> str:
    if d.settings["trend_basis"] == "months":
        return "* Trend: Last Week Compared To The Average Of The Months Shown."
    return "* Trend: Last Week Compared To The Week Before."


def _coverage_stages(d: ReportData) -> list:
    def stock(key):
        return lambda p: stock_at(d, p, key)
    return [("upstream", "Upstream Stock", stock(_retail("Upstream stock Total", "upstream"))),
            ("in_prep", "In Prep", stock(_retail("In prep Total", "in_prep"))),
            ("for_sale", "For Sale", stock(_retail("On sale stock Total", "on_sale"))),
            ("sold_not_delivered", "Sold Not Delivered", stock(_retail("Ordered"))),
            ("total", "Total", lambda p: total_b2c(d, p))]


# ── Sections ─────────────────────────────────────────────────────────────────

def section_deliveries_objective(d: ReportData) -> dict:
    periods = d.table_periods
    budget = _budget(d, B_DELIVERIES, d.current["month"])
    mini = {"id": "deliveries_budget", "title": "", "columns": [{"key": "budget", "label": d.current["month"], "group": "extra"}],
            "rows": [{"label": "Net Deliveries Budget #", "indent": 0, "bold": False, "format": "int",
                      "values": {"budget": _clean(budget)}}]}
    names = {"B2B2C": "B2B2C", "C2B2C - PX": "C2B2C - PX", "C2B2C - Other": "Other C2B2C", "Consignment": "Consignment",
             "Buy back": "Buy Back"}
    rows = [_line(f"Deliveries {names[channel]}", periods, lambda p, c=channel: deliveries(d, p, c), indent=1)
            for channel, _ in CHANNELS]
    rows.append(_line("Net Deliveries B2C", periods, lambda p: deliveries(d, p), bold=True))
    rows += [_line(f"% Mix {names[channel]}", periods,
                   lambda p, c=channel: _ratio(deliveries(d, p, c), deliveries(d, p)), "pct0", indent=1)
             for channel, _ in CHANNELS]
    rows.append(_line("% Mix Deliveries", periods,
                      lambda p: _ratio(_sum_all(*[deliveries(d, p, c) for c, _ in CHANNELS]), deliveries(d, p)), "pct0", bold=True))
    return {"tables": [mini, _table("deliveries_channel", "Deliveries B2C By Channel", periods, rows)]}


def _channel_rows(periods: list, figure, fmt: str = "int", total=None) -> list:
    rows = [_line(name, periods, lambda p, c=channel: figure(p, c), fmt, indent=1) for channel, name in CHANNELS]
    rows.append(_line("Total", periods, total or (lambda p: _sum_all(*[figure(p, c) for c, _ in CHANNELS])), fmt, bold=True))
    return rows


def _purchased_gpu(d: ReportData, p: dict, channel=None):
    return _mean(_channel(_bought(d, p), channel), "gpu_m")


def first_day_gpu(d: ReportData, p: dict, column: str):
    """GPU-M of stock on sale on a period's first day: the 1st of the month, or the
    Monday. If that day is missing a later day in the week is used, with a warning."""
    if d.daily.empty:
        return None
    if p["kind"] == "month":
        days = [f"{p['month']}-01"]
    else:
        first = datetime.date.fromisoformat(p["start"])
        days = [(first + datetime.timedelta(days=n)).isoformat() for n in range(7)]
    for day in days:
        rows = d.rows("daily", "date", day)
        if not rows.empty:
            if day != days[0]:
                d.warnings.append(f"Daily Summary has no row for {days[0]}, so {day} has been used.")
            return _clean(pd.to_numeric(rows[column], errors="coerce").iloc[0])
    return None


def section_volume_trends(d: ReportData) -> dict:
    periods = d.table_periods
    enquiry = (("Web Sessions", "k"), ("Web Leads", "int"), ("Classifieds", "int"), ("Valuations", "int"), ("Calls", "int"))
    bought = (("Total", "Total Cars Bought", 0), ("B2B", "B2B", 1), ("C2B", "C2B", 1), ("PX", "PX", 1), ("Consignment", "Consignment", 1))
    rows = [_line(label, periods, lambda p, k=volume_key("enquiry", label): volume_mean(d, p, k), fmt) for label, fmt in enquiry]
    rows.append(_blank_line())
    rows += [_line(name, periods, lambda p, k=volume_key("cars bought", label): volume_mean(d, p, k), indent=indent, bold=not indent)
             for label, name, indent in bought]
    rows.append(_line("Prepped Vehicles", periods, lambda p: recon_per_week(d, p, RECON_GOOLE, RECON_HULL, RECON_SITES), bold=True))
    rows += [_line(name, periods, lambda p, c=column: recon_per_week(d, p, c), indent=1)
             for name, column in (("Goole", RECON_GOOLE), ("Sutton Fields", RECON_HULL), ("Sites", RECON_SITES))]
    rows.append(_line("Net Orders", periods, lambda p: _per_week(d, p, net_orders(d, p)), bold=True))
    # 30/7 as on the slide and the chart, so this row and the chart agree.
    rows.append(_line("Net Deliveries B2C #", periods, lambda p: deliveries_per_week(d, p), bold=True))
    for row in rows:
        if row["label"]:
            add_trend(d, row)
    main = _table("average_volumes", "Average Volumes Per Week", periods, rows, (("trend", "Trend *"),))

    # The spreadsheet's 3&5 tab: complete months and weeks, without the current month.
    shown = d.chart_periods
    more_tables = [
        _table("gross_orders", "Gross Orders - Month Figures Are Weekly Averages", shown,
               _channel_rows(shown, lambda p, c: _per_week(d, p, gross_orders(d, p, c)))),
        _table("net_orders", "Net Orders - Month Figures Are Weekly Averages", shown,
               _channel_rows(shown, lambda p, c: _per_week(d, p, net_orders(d, p, c)))),
        _table("recon_units", "Reconditioned Units - Month Figures Are Weekly Averages", shown,
               [_line(name, shown, lambda p, c=column: recon_per_week(d, p, c), indent=1)
                for name, column in (("Goole", RECON_GOOLE), ("Hull", RECON_HULL), ("Sites", RECON_SITES))]
               + [_line("Total", shown, lambda p: recon_per_week(d, p, RECON_GOOLE, RECON_HULL, RECON_SITES), bold=True)]),
        _table("purchased_units", "Vehicles Purchased - Month Figures Are Weekly Averages", shown,
               _channel_rows(shown, lambda p, c: _per_week(d, p, purchases_count(d, p, c)))),
        _table("purchased_gpu", "Vehicles Purchased - GPU-M", shown,
               _channel_rows(shown, lambda p, c: _purchased_gpu(d, p, c), "gbp0", lambda p: _purchased_gpu(d, p))),
        _table("advertised_gpu", "Vehicles Advertised - GPU-M (First Day Of Period)", shown,
               [_line(name, shown, lambda p, c=column: first_day_gpu(d, p, c), "gbp0", indent=1)
                for name, column in (("B2B2C", "avg_gpu_m_b2b2c"), ("C2B2C - PX", "avg_gpu_m_c2b2c_px"),
                                     ("C2B2C - Other", "avg_gpu_m_c2b2c_other"), ("Consignment", "avg_gpu_m_consignment"))]
               + [_line("Total", shown, lambda p: first_day_gpu(d, p, "avg_gpu_m_stock_on_sale"), "gbp0", bold=True)]),
        # The spreadsheet's title says this leaves out cancelled orders, but its formula does not.
        _table("ordered_gpu", "Vehicles Ordered - GPU-M", shown,
               _channel_rows(shown, lambda p, c: _mean(_channel(_ordered(d, p), c), "gpu_m"), "gbp0",
                             lambda p: _mean(_ordered(d, p), "gpu_m"))),
        _table("adverts_published", "Adverts Published - Month Figures Are Weekly Averages", shown,
               _channel_rows(shown, lambda p, c: None if d.purchases.empty
                             else _per_week(d, p, float(len(_channel(_advertised(d, p), c)))))),
    ]
    return {"tables": [main], "auto_notes": [_trend_note(d)], "more": {"tables": more_tables}}


def _stock_rows(d: ReportData, periods: list) -> list:
    def stock(key):
        return lambda p: stock_at(d, p, key)

    def parts(block, labels):
        return [_line(name, periods, stock(_retail(label, block)), indent=1) for label, name in labels]
    channels = (("B2B", "B2B"), ("C2B", "C2B"), ("PX", "PX"), ("Consignment", "Consignment"))
    return (parts("upstream", channels)
            + [_line("Upstream Stock", periods, stock(_retail("Upstream stock Total", "upstream")), bold=True)]
            + parts("in_prep", (("Sutton Fields", "Sutton Fields"), ("Goole", "Goole"), ("Sites", "Sites"),
                                ("Subcontractor offsite", "Subcontractor")))
            + [_line("In Prep", periods, stock(_retail("In prep Total", "in_prep")), bold=True)]
            + parts("on_sale", channels + (("To price", "To Price"),))
            + [_line("On Sale Stock", periods, stock(_retail("On sale stock Total", "on_sale")), bold=True),
               _line("Sold Not Delivered", periods, stock(_retail("Ordered")), bold=True),
               _line("Trade Hold", periods, stock(_retail("Trade hold")), bold=True),
               _line("Total B2C", periods, lambda p: total_b2c(d, p), bold=True)])


def _sleeping_rows(d: ReportData, periods: list) -> list:
    targets = d.settings["sleeping_targets"]
    rows = []
    for block, name, labels in SLEEPING_BLOCKS:
        rows += [_line(shown, periods, lambda p, k=_retail(label, block, "sleeping"): stock_at(d, p, k), indent=1)
                 for label, shown in labels]
        rows.append(_line(name, periods, lambda p, b=block, l=labels: sleeping_block(d, p, b, l), bold=True,
                          extra={"target": targets[block]}))
    rows += [
        _line("Sold Not Delivered", periods, lambda p: stock_at(d, p, _retail("Ordered", "", "sleeping")), bold=True,
              extra={"target": targets["sold_not_delivered"]}),
        _line("Trade Hold", periods, lambda p: stock_at(d, p, _retail("Trade hold", "", "sleeping")), bold=True,
              extra={"target": targets["trade_hold"]}),
        _line("Total B2C", periods, lambda p: sleeping_total(d, p), bold=True),
    ]
    return rows


def section_inventories(d: ReportData) -> dict:
    # Complete months and weeks: the current month's end has not happened yet.
    periods = d.chart_periods
    targets = d.settings["coverage_targets"]
    rotation = [_line(name, periods, lambda p, u=units: coverage_days(d, p, u(p)), "days", bold=key == "total",
                      extra={"target": targets[key]})
                for key, name, units in _coverage_stages(d)]
    refurb = [_line(name, periods, lambda p, c=column, l=label: refurb_days(d, p, c, l), "days", indent=1)
              for name, column, label in (("Sutton Fields", RECON_HULL, "Sutton Fields"),
                                          ("Goole", RECON_GOOLE, "Goole"), ("Sites", RECON_SITES, "Sites"))]
    stock_key = lambda label: volume_key("stock", label)  # noqa: E731
    slide_rows = [_line(label, periods, lambda p, k=stock_key(source): stock_at(d, p, k), fmt, bold=label == "Total Stock")
                  for label, source, fmt in (
                      ("Awaiting Arrival", "Awaiting Arrival", "int"), ("Trade", "Trade", "int"), ("In Prep", "In Prep", "int"),
                      ("Held", "Held", "int"), ("To Price", "To Price", "int"), ("For Sale", "For sale", "int"),
                      ("For Sale %", "For sale %", "pct0"), ("Sold Not Delivered", "Sold not Delivered", "int"),
                      ("Courtesy", "Courtesy", "int"), ("Total Stock", "Total", "int"))]
    return {
        "tables": [_table("stock_b2c", "Stock B2C (Units)", periods, _stock_rows(d, periods)),
                   _table("rotation_b2c", "Rotation B2C (Days)", periods, rotation, (("target", "Target"),)),
                   _table("sleeping_stock", "Sleeping Stock", periods, _sleeping_rows(d, periods), (("target", "Target"),)),
                   _table("rotation_refurb", "Rotation Refurb (Days)", periods, refurb)],
        "link": {"section": "glossary", "label": "Click Here For Definitions"},
        "more": {"tables": [_table("stocks_status", "Stocks By Status", periods, slide_rows)]},
    }


# ── GPU-M ────────────────────────────────────────────────────────────────────

CHANNEL_GROUPS = (("B2B2C", "B2B2C"), ("C2B2C*", "C2B2C - All"), ("Consignment", "Consignment"), ("Buy back", "Buy Back"))
PAYMENT_RATE, PAYMENT_ZERO = "Rate", "0_percent"


def margin_mean(d: ReportData, p: dict, column: str, channel=None):
    return _mean(_channel(_delivered(d, p), channel), column)


def gpu_b_per_delivery(d: ReportData, p: dict):
    if d.margin.empty:
        return None
    return _ratio(float(_delivered(d, p)["gpu_b"].sum()), deliveries(d, p))


def section_gpu_m_channel(d: ReportData) -> dict:
    periods = d.table_periods
    names = {"B2B2C": "B2B2C", "C2B2C - PX": "C2B2C - PX", "C2B2C - Other": "C2B2C - OTH", "Consignment": "Consignment",
             "Buy back": "Buy Back"}
    rows = [_line(f"GPU-M {names[channel]}", periods, lambda p, c=channel: margin_mean(d, p, "gpu_m", c), "int", indent=1)
            for channel, _ in CHANNELS]
    rows.append(_line("GPU-M", periods, lambda p: margin_mean(d, p, "gpu_m"), bold=True))
    for row in rows:
        add_trend(d, row)
    main = _table("gpu_m_channel", "GPU-M By Channel", periods, rows, (("trend", "Trend *"),))

    def group_rows(title, column, fmt="int"):
        lines = [_line(f"{title} {name}", periods, lambda p, c=channel: margin_mean(d, p, column, c), fmt, indent=1)
                 for channel, name in CHANNEL_GROUPS]
        return lines + [_line(title, periods, lambda p: margin_mean(d, p, column), fmt, bold=True)]

    def check(p):
        """Should be nil: each measure's channels, weighted by mix, give its total, and M + S + B give MSB."""
        total = deliveries(d, p)
        if not total:
            return None
        gap = 0.0
        for column in ("gpu_m", "gpu_s", "dis"):
            split = sum((margin_mean(d, p, column, c) or 0) * deliveries(d, p, c) / total for c, _ in CHANNEL_GROUPS)
            gap += abs(split - (margin_mean(d, p, column) or 0))
        parts = (margin_mean(d, p, "gpu_m") or 0) + (margin_mean(d, p, "gpu_s") or 0) + (gpu_b_per_delivery(d, p) or 0)
        return gap + abs((margin_mean(d, p, "gpu_msb") or 0) - parts)

    block = (
        [_line(f"Deliveries {name}", periods, lambda p, c=channel: deliveries(d, p, c), indent=1) for channel, name in CHANNEL_GROUPS]
        + [_line("Net Deliveries B2C", periods, lambda p: deliveries(d, p), bold=True)]
        + [_line(f"% {name}", periods, lambda p, c=channel: _ratio(deliveries(d, p, c), deliveries(d, p)), "pct0", indent=1)
           for channel, name in CHANNEL_GROUPS]
        + group_rows("GPU-MS", "gpu_ms") + group_rows("GPU-M", "gpu_m") + group_rows("GPU-S", "gpu_s")
        + group_rows("Aging", "dis")
        + [_line("GPU-B (£ / Delivery B2C)", periods, lambda p: gpu_b_per_delivery(d, p), bold=True),
           _line("GPU-MSB", periods, lambda p: margin_mean(d, p, "gpu_msb"), bold=True),
           _line("Check (Should Be Nil)", periods, check, "gbp0")])
    return {"tables": [main], "auto_notes": [_trend_note(d)],
            "more": {"tables": [_table("gpu_msb_channel", "GPU-MSB By Channel", periods, block)]}}


def section_gpu_m_b2b2c(d: ReportData) -> dict:
    """The biggest B2B2C suppliers of the review's month. 'Before' takes every earlier
    month back to the AutoView Costs Start setting."""
    month = d.current["month"]
    autoview_start = d.meta["autoview_start"]
    base = _table("base_deliveries", "Base Net Deliveries B2C", d.table_periods,
                  [_line("Net Deliveries B2C", d.table_periods, lambda p: deliveries(d, p))])
    weeks = d.weeks
    columns = [{"key": "before", "label": f"GPU-M {autoview_start} To Before {month}", "group": "extra"},
               {"key": "deliv", "label": f"Deliv. {month}", "group": "extra"},
               {"key": "share", "label": "%", "group": "extra"},
               {"key": "gpu_m", "label": f"GPU-M {month}", "group": "extra"},
               {"key": "-", "label": "-", "group": "spacer"}] + _columns(weeks)
    formats = {"before": "gbp0", "deliv": "int", "share": "pct0", "gpu_m": "gbp0", **{w["key"]: "gbp0" for w in weeks}}
    if d.margin.empty:
        return {"tables": [base, {"id": "top_suppliers", "title": "Top Suppliers B2B2C", "columns": columns, "rows": []}]}

    b2b2c = d.margin[d.margin["supplier_channel"] == "B2B2C"]
    posted = b2b2c["invoice_posted_month"].astype(str)
    earlier = b2b2c[b2b2c["invoice_posted_month"].notna() & (posted < month) & (posted >= autoview_start)]
    this_month = b2b2c[b2b2c["invoice_posted_month"].astype(str) == month]
    total = float(this_month["deliveries"].sum())
    ranked = this_month.groupby("supplier_name")["deliveries"].sum().sort_values(ascending=False, kind="stable")
    shown = list(ranked.index[:int(d.settings["constants"]["top_suppliers"])])

    def row(label, picked, bold=False):
        def rows_of(frame):
            return frame if picked is None else frame[frame["supplier_name"] == picked]
        here = rows_of(this_month)
        values = {"before": _mean(rows_of(earlier), "gpu_m"), "deliv": float(here["deliveries"].sum()),
                  "share": _ratio(float(here["deliveries"].sum()), total), "gpu_m": _mean(here, "gpu_m")}
        for week in weeks:
            values[week["key"]] = _mean(rows_of(b2b2c[b2b2c["delivery_week_end"] == week["end"]]), "gpu_m")
        return {"label": label, "indent": 0 if bold else 1, "bold": bold, "format": "int", "formats": formats,
                "values": {k: _clean(v) for k, v in values.items()}}

    rows = [row(str(name), name) for name in shown]
    others = total - sum(float(ranked[name]) for name in shown)
    rows.append({"label": "Other", "indent": 1, "bold": False, "format": "int", "formats": formats,
                 "values": {"deliv": others, "share": _ratio(others, total)}})
    rows.append(row("B2B2C", None, bold=True))
    top = {"id": "top_suppliers", "title": f"Top {len(shown)} Suppliers B2B2C", "columns": columns, "rows": rows}
    return {"tables": [base, top]}


# ── GPU-S ────────────────────────────────────────────────────────────────────

GAP_RTI = ("product_margin_gap_rti_insurance", "product_margin_premia_gap_rti_insurance")
OTHER_PRODUCTS = ("product_margin_services_mots_carried_over", "product_margin_mats")
ANTI_BAC, EV_CHARGER = ("product_margin_anti_bacterial_protection",), ("product_margin_ev_charger",)
WARRANTY, CERAMIC = ("product_margin_warranty",), ("product_margin_ceramic_vehicle_protection",)
SMART, SERVICE_PLAN = ("product_margin_smart_plus_insurance",), ("product_margin_service_mot_plan",)
FINANCING = ("finance_commission",)
STANDARD_COSTS = ("gpu_s_cost",)
# Every product line. Together they are total_product_margin (a test holds them to it).
PRODUCT_LINES = (("Warranty Margin", WARRANTY), ("Ceramic Vh Protection", CERAMIC), ("GAP/RTI Insurance", GAP_RTI),
                 ("SMART Plus", SMART), ("Service/MOT Plan", SERVICE_PLAN), ("Anti-Bac", ANTI_BAC),
                 ("EV Charger", EV_CHARGER), ("Other Products", OTHER_PRODUCTS))


def nature(d: ReportData, p: dict, columns: tuple):
    """A line's £ per delivery. Some lines are more than one column."""
    rows = _delivered(d, p)
    return _sum_all(*[_mean(rows, column) for column in columns]) if not rows.empty else None


def taken_share(d: ReportData, p: dict, columns: tuple):
    """Share of deliveries with this product: any of its columns not nil."""
    rows = _delivered(d, p)
    total = deliveries(d, p)
    if rows.empty or not total:
        return None
    return float((rows[list(columns)].fillna(0) != 0).any(axis=1).sum()) / total


def per_contract(d: ReportData, p: dict, columns: tuple):
    share, total = taken_share(d, p, columns), deliveries(d, p)
    value = nature(d, p, columns)
    return _ratio(value * total, share * total) if None not in (share, total, value) else None


def paid_by(d: ReportData, p: dict, kind: str):
    if d.margin.empty:
        return None
    rows = _delivered(d, p)
    return float(rows[rows["payment_type"] == kind]["deliveries"].sum())


def take_rate(d: ReportData, p: dict):
    """Commissioned finance share of deliveries, leaving out 0% deals."""
    total, zero = deliveries(d, p), paid_by(d, p, PAYMENT_ZERO)
    return _ratio(paid_by(d, p, PAYMENT_RATE), total - zero) if total is not None and zero is not None else None


def margin_per_finance_contract(d: ReportData, p: dict):
    rows = _delivered(d, p)
    return _ratio(float(rows["finance_commission"].sum()), paid_by(d, p, PAYMENT_RATE)) if not rows.empty else None


def section_gpu_s_nature(d: ReportData) -> dict:
    periods = d.table_periods

    def pound(label, columns, **kw):
        return _line(f"{label} £/U", periods, lambda p: nature(d, p, columns), **kw)

    def share(columns):
        return _line("% Penetration On B2C Deliveries", periods, lambda p: taken_share(d, p, columns), "pct0", indent=1)

    def contract(columns):
        return _line("£ / Contract", periods, lambda p: per_contract(d, p, columns), "int", indent=1)

    rows = [
        _line("Net Deliveries B2C", periods, lambda p: deliveries(d, p), bold=True),
        pound("Financing Commissioned", FINANCING),
        _line("% Penetration On B2C Deliveries", periods, lambda p: _ratio(paid_by(d, p, PAYMENT_RATE), deliveries(d, p)), "pct0", indent=1),
        _line("% Penetration On Deliv. Excl. 0%", periods, lambda p: take_rate(d, p), "pct0", indent=1),
        _line("£ / Contract", periods, lambda p: margin_per_finance_contract(d, p), "int", indent=1),
        _line("0% Interest Rate: % Penetration On B2C Deliveries", periods,
              lambda p: _ratio(paid_by(d, p, PAYMENT_ZERO), deliveries(d, p)), "pct0"),
        pound("Warranty Margin", WARRANTY), share(WARRANTY), contract(WARRANTY),
        pound("Ceramic Vh Protection", CERAMIC), share(CERAMIC), contract(CERAMIC),
        pound("GAP/RTI Insurance", GAP_RTI), pound("SMART Plus", SMART), pound("Service/MOT Plan", SERVICE_PLAN),
        pound("Anti-Bac", ANTI_BAC), pound("EV Charger", EV_CHARGER), pound("Other Products", OTHER_PRODUCTS),
        pound("STD - Internal Costs Of Services", STANDARD_COSTS),
        _line("GPU S £/U", periods, lambda p: margin_mean(d, p, "gpu_s"), bold=True),
        _line("Admin Fees £/U (Included In Chassis Margin)", periods, lambda p: margin_mean(d, p, "product_margin_admin_fee")),
    ]
    return {"tables": [_table("gpu_s_nature", "GPU-S By Nature", periods, rows)]}


# ── Financing ────────────────────────────────────────────────────────────────

def _ranked(rows: pd.DataFrame, column: str, limit: int) -> list:
    """The column's values with the most deliveries in the review's month."""
    if rows.empty:
        return []
    return list(rows.groupby(column)["deliveries"].sum().sort_values(ascending=False, kind="stable").index[:limit])


def section_financing(d: ReportData) -> dict:
    periods = d.table_periods
    constants = d.settings["constants"]
    month_rows = _delivered(d, d.current)
    sites = _ranked(month_rows, "deal_site_clean", int(constants["sites_shown"]))
    rate_rows = month_rows[month_rows["payment_type"] == PAYMENT_RATE] if not month_rows.empty else month_rows
    companies = _ranked(rate_rows, "finance_company", int(constants["finance_companies_shown"]))

    def site_rate(p, site):
        rows = _delivered(d, p)
        if rows.empty:
            return None
        rows = rows[rows["deal_site_clean"] == site]
        return _ratio(float(rows[rows["payment_type"] == PAYMENT_RATE]["deliveries"].sum()),
                      float(rows["deliveries"].sum() - rows[rows["payment_type"] == PAYMENT_ZERO]["deliveries"].sum()))

    def company_share(p, company):
        rows = _delivered(d, p)
        contracts = paid_by(d, p, PAYMENT_RATE)
        if rows.empty or not contracts:
            return None
        rows = rows[rows["payment_type"] == PAYMENT_RATE]
        picked = rows["finance_company"] == company if company else ~rows["finance_company"].isin(companies)
        return float(rows[picked]["deliveries"].sum()) / contracts

    rows = [_line("Deliveries B2C Excl. 0% Interest Contracts", periods,
                  lambda p: _sum_all(deliveries(d, p), None if d.margin.empty else -paid_by(d, p, PAYMENT_ZERO))),
            _line("Commissioned Financing Contracts", periods, lambda p: paid_by(d, p, PAYMENT_RATE))]
    rows += [_line(f"{i} - {site}", periods, lambda p, s=site: site_rate(p, s), "pct0", indent=1)
             for i, site in enumerate(sites, start=1)]
    rows.append(_line("% Penetration On Deliv. Excl. 0%", periods, lambda p: take_rate(d, p), "pct0", bold=True))
    rows += [_line(str(company), periods, lambda p, c=company: company_share(p, c), "pct0", indent=1) for company in companies]
    rows.append(_line("Other", periods, lambda p: company_share(p, None), "pct0", indent=1))
    rows.append(_line("Total", periods, lambda p: _ratio(paid_by(d, p, PAYMENT_RATE), paid_by(d, p, PAYMENT_RATE)), "pct0", bold=True))
    return {"tables": [_table("financing", "% Penetration On Deliv. Excl. 0%", periods, rows)]}


# ── Breakdown ────────────────────────────────────────────────────────────────

# (label, column, definition shown on hover, uses AutoView costs). Definitions are from
# the spreadsheet's Ctrl tab. A line using AutoView costs is blank before they start.
BREAKDOWN = (
    ("Selling Price £/U", "sale_price_ex_vat", "", False),
    ("Net - Admin Fee", "product_margin_admin_fee", "", False),
    ("AV - Purchase Price", "purchase_price_ex_vat", "", False),
    ("APEX - Vehicle Discount", "discount_ex_vat", "Staff discount: family & friends + commercial discount", False),
    ("Calcul - VAT Margin", "margin_vat_cost", "", False),
    ("APEX - Finance Subsidy", "finance_subsidy", "0% deals: extra margin on the car should cover the finance subsidy", False),
)
BREAKDOWN_COSTS = (
    ("AV - Recon Costs £/U", "recon_cost", "", True),
    ("AV - Other Costs £/U", "other_costs", "", True),
    ("Apex Vs AutoView Difference £/U", "costs_unmatched",
     "Apex holds a car's costs as one number. This is what AutoView's recon and other costs do not account for", True),
    ("STD - Prep Centre (Rent) £/U", "std_prep_centre_rent", "", False),
    ("STD - Transport Salaries £/U", "std_transport_salaries", "30-40 drivers internal", False),
    ("STD - External Transport Costs £/U", "std_external_transport_costs", "Trucks rental + 3rd party company to transport", False),
    ("STD - Delivery Sale £/U", "std_delivery_sale", "Home delivery (% rate): ~20% penetration rate", False),
    ("STD - Late Costs £/U", "std_late_costs", "Goodwill", False),
    ("STD - Damage Claim / Rebates / Other", "std_damage_claims_rebates_other", "Rebates: supplier damage / deal RFA with suppliers", False),
    ("STD - Credited Deals Carried Over", "std_credited_deals_carried_over", "", False),
    ("STD - Subcontracted Valet Costs", "std_subcontracted_valet_costs", "", False),
    ("STD - Write Downs", "std_write_downs", "", False),
)
BREAKDOWN_SERVICES = (
    ("APEX - Financing Margin", FINANCING), ("APEX - Warranty Margin", WARRANTY), ("APEX - GAP/RTI Insurance", GAP_RTI),
    ("APEX - SMART Plus", SMART), ("APEX - Service/MOT Plan", SERVICE_PLAN), ("APEX - Ceramic Vh Protection", CERAMIC),
    ("APEX - Anti-Bac", ANTI_BAC), ("APEX - EV Charger", EV_CHARGER), ("APEX - Other Products", OTHER_PRODUCTS),
    ("STD - GPU-S Costs", STANDARD_COSTS),
)


def section_breakdown(d: ReportData) -> dict:
    months = [_month_period(add_months(d.current["month"], n)) for n in range(-11, 1)]

    def costed(p, column):
        """A figure using AutoView costs, blank before they start."""
        return margin_mean(d, p, column) if autoview_costs_usable(p["month"], d.meta["autoview_start"]) else None

    def plain(label, column, hint, uses_costs):
        figure = (lambda p: costed(p, column)) if uses_costs else (lambda p: margin_mean(d, p, column))
        line = _line(label, months, figure, indent=1)
        line["hint"] = hint
        return line

    def check(p):
        """Should be nil: each block's lines add up to its total. Recon and other costs
        are blank before AutoView starts, so the costs block is only checked after."""
        if _delivered(d, p).empty:
            return None
        mean = lambda column: margin_mean(d, p, column) or 0  # noqa: E731
        chassis = sum(mean(line[1]) for line in BREAKDOWN) - mean("chassis_margin_after_subsidy")
        costs = sum(mean(line[1]) for line in BREAKDOWN_COSTS) - mean("total_cos")
        if not autoview_costs_usable(p["month"], d.meta["autoview_start"]):
            costs = 0
        margin = mean("chassis_margin_after_subsidy") + mean("total_cos") - mean("gpu_m")
        services = sum(nature(d, p, columns) or 0 for _, columns in BREAKDOWN_SERVICES) - mean("gpu_s")
        return abs(chassis) + abs(costs) + abs(margin) + abs(services)

    rows = [_line("Deliveries", months, lambda p: deliveries(d, p), bold=True)]
    rows += [plain(*line) for line in BREAKDOWN]
    rows.append(_line("Chassis Margin £/U", months, lambda p: margin_mean(d, p, "chassis_margin_after_subsidy"), bold=True))
    rows += [plain(*line) for line in BREAKDOWN_COSTS]
    rows.append(_line("Total Costs Of Sales", months, lambda p: margin_mean(d, p, "total_cos"), bold=True))
    rows.append(_line("GPU-M", months, lambda p: margin_mean(d, p, "gpu_m"), bold=True))
    rows += [_line(label, months, lambda p, c=columns: nature(d, p, c), indent=1) for label, columns in BREAKDOWN_SERVICES]
    rows.append(_line("GPU S £/U", months, lambda p: margin_mean(d, p, "gpu_s"), bold=True))
    rows.append(_line("GPU MS £/U", months, lambda p: margin_mean(d, p, "gpu_ms"), bold=True))
    rows.append(_line("GPU-B", months, lambda p: gpu_b_per_delivery(d, p), indent=1))
    rows.append(_line("GPU-MSB", months, lambda p: margin_mean(d, p, "gpu_msb"), bold=True))
    rows.append(_line("Check (Should Be Nil)", months, check, "gbp0"))
    return {"tables": [_table("breakdown_gpu_ms", "Breakdown GPU-MS", months, rows)]}


# ── Glossary ─────────────────────────────────────────────────────────────────

_GLOSSARY_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "wbr_report_glossary.md")


def default_glossary() -> str:
    with open(_GLOSSARY_FILE, encoding="utf-8") as f:
        return f.read().strip()


def glossary_tables(text: str) -> list:
    """The glossary's text as tables. '# Title' starts a table, the next line is its
    headings, and each line after it is a row, with cells split by |."""
    tables, table, keys = [], None, []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            table = {"id": f"glossary_{len(tables)}", "title": line.lstrip("# ").strip(), "columns": [], "rows": [], "text": True}
            tables.append(table)
            keys = None
            continue
        if table is None:
            table = {"id": f"glossary_{len(tables)}", "title": "Glossary", "columns": [], "rows": [], "text": True}
            tables.append(table)
            keys = None
        cells = [cell.strip() for cell in line.split("|")]
        if keys is None:
            table["title"] = table["title"] or cells[0]
            keys = cells[1:]
            table["columns"] = [{"key": f"c{i}", "label": label, "group": "extra"} for i, label in enumerate(keys)]
            table["label_heading"] = cells[0]
            continue
        table["rows"].append({"label": cells[0], "indent": 0, "bold": False, "format": "int",
                              "values": {f"c{i}": cell for i, cell in enumerate(cells[1:])}})
    return tables


def section_glossary(d: ReportData) -> dict:
    return {"tables": glossary_tables(d.settings.get("glossary") or default_glossary())}


# ── P&L ──────────────────────────────────────────────────────────────────────

_K_LINES = ("COCA k£", "Sales costs k£", "HQ costs k£", "Depreciation k£", "IFRS impacts to EBIT k£",
            "IFRS impacts to EBIT/EBITDA k£", "Revenue k£")
_UNIT_LINES = ("GPU-MSB (£/unit)", "GPU-M (£/unit)", "GPU-S (£/unit)", "GPU-B (£/unit)")


def _budget_line(d: ReportData, month: str) -> dict:
    labels = ("Net Deliveries B2C",) + _UNIT_LINES + _K_LINES + ("EBIT k£", "EBITDA k£")
    return {label: _budget(d, ("P&L", label), month) for label in labels}


def _fiscal_months(d: ReportData) -> list:
    """The months of the financial year so far, up to the review's month."""
    month = d.current["month"]
    start = int(d.settings["constants"]["fiscal_year_start_month"])
    number = int(month[5:7])
    back = (number - start) % 12
    return [add_months(month, n) for n in range(-back, 1)]


def pnl_column(lines: list, current_is_estimate: bool = False, budget: bool = False) -> dict:
    """One column of the P&L from one or more months' lines (the last is the current
    month when current_is_estimate). The k£ lines add up; the £/unit lines are
    weighted by deliveries. The spreadsheet's overheads for the current month's
    estimate leave out depreciation and the second IFRS line, unlike its other columns."""
    def total(name):
        return _sum_all(*[line.get(name) for line in lines]) if lines else None

    units = total("Net Deliveries B2C")

    def per_unit(name):
        parts = [(line.get(name), line.get("Net Deliveries B2C")) for line in lines]
        if not lines or not units or any(v is None or u is None for v, u in parts):
            return None
        return sum(v * u for v, u in parts) / units

    def k_of(value):
        return value * units / 1000 if value is not None and units is not None else None

    msb, gpu_m, gpu_s, gpu_b = [per_unit(name) for name in _UNIT_LINES]
    gross = k_of(msb)
    coca, sales, hq, depreciation = total("COCA k£"), total("Sales costs k£"), total("HQ costs k£"), total("Depreciation k£")
    ifrs_ebit, ifrs_ebitda, revenue = total("IFRS impacts to EBIT k£"), total("IFRS impacts to EBIT/EBITDA k£"), total("Revenue k£")

    overheads = []
    for i, line in enumerate(lines):
        parts = [line.get(n) for n in ("Sales costs k£", "HQ costs k£", "IFRS impacts to EBIT k£")]
        if not (current_is_estimate and i == len(lines) - 1):
            second = line.get("IFRS impacts to EBIT/EBITDA k£")
            parts += [line.get("Depreciation k£"), -second if second is not None else None]
        overheads.append(_sum_all(*parts))
    overhead_total = _sum_all(*overheads) if overheads else None

    if budget:
        ebit, ebitda = total("EBIT k£"), total("EBITDA k£")
    else:
        ebitda = _sum_all(gross, sales, hq, ifrs_ebit, coca)
        ebit = _sum_all(gross, sales, hq, depreciation, ifrs_ebit, ifrs_ebitda, coca)
    return {
        "Net Deliveries B2C": units, "Revenue (£/unit)": _ratio(revenue * 1000, units) if revenue is not None else None,
        "GPU-MSB (£/unit)": msb, "GPU-M (£/unit)": gpu_m, "GPU-S (£/unit)": gpu_s, "GPU-B (£/unit)": gpu_b,
        "GPU-MS (£/unit)": msb - gpu_b if msb is not None and gpu_b is not None else None,
        "SG&A": _sum_all(coca, overhead_total), "COCA k£": coca, "COCA £/u": _ratio(coca * 1000, units) if coca is not None else None,
        "Overheads k£": overhead_total, "Revenue k£": revenue, "GPU-M k£": k_of(gpu_m), "GPU-S k£": k_of(gpu_s), "GPU-B k£": k_of(gpu_b),
        "Gross Profit k£": gross, "Sales costs k£": sales, "HQ costs k£": hq, "Depreciation k£": depreciation,
        "IFRS impacts to EBIT k£": ifrs_ebit, "IFRS impacts to EBIT/EBITDA k£": ifrs_ebitda,
        "EBITDA k£": ebitda, "EBIT k£": ebit, "EBITDA %": _ratio(ebitda, revenue),
    }


_PNL_ROWS = (
    ("Net Deliveries B2C", "Net Deliveries B2C", "int", 0, True),
    ("Growth Deliveries B2C %", "growth", "pct0", 0, False),
    ("Revenues (£/U)", "Revenue (£/unit)", "gbp0", 0, False),
    ("GPU-MSB (£/Unit)", "GPU-MSB (£/unit)", "gbp0", 0, True),
    ("GPU-M (£/Unit)", "GPU-M (£/unit)", "gbp0", 1, False),
    ("GPU-S (£/Unit)", "GPU-S (£/unit)", "gbp0", 1, False),
    ("GPU-B (£/Unit)", "GPU-B (£/unit)", "gbp0", 1, False),
    ("GPU-MS (£/Unit)", "GPU-MS (£/unit)", "gbp0", 0, False),
    ("SG&A Incl. IFRS (k£)", "SG&A", "gbp0", 0, True),
    ("COCA k£", "COCA k£", "gbp0", 1, False),
    ("COCA £/U", "COCA £/u", "gbp0", 1, False),
    ("Overheads k£", "Overheads k£", "gbp0", 1, False),
    ("Revenues (k£)", "Revenue k£", "gbp0", 0, False),
    ("Gross Profit (k£)", "Gross Profit k£", "gbp0", 0, True),
    ("GPU-M k£", "GPU-M k£", "gbp0", 1, False),
    ("GPU-S k£", "GPU-S k£", "gbp0", 1, False),
    ("GPU-B k£", "GPU-B k£", "gbp0", 1, False),
    ("Sales Costs k£", "Sales costs k£", "gbp0", 1, False),
    ("HQ Costs k£", "HQ costs k£", "gbp0", 1, False),
    ("Depreciation k£", "Depreciation k£", "gbp0", 1, False),
    ("IFRS Impacts To EBIT k£", "IFRS impacts to EBIT k£", "gbp0", 1, False),
    ("IFRS Impacts To EBIT/EBITDA k£", "IFRS impacts to EBIT/EBITDA k£", "gbp0", 1, False),
    ("EBITDA (k£)", "EBITDA k£", "gbp0", 0, True),
    ("EBIT (k£)", "EBIT k£", "gbp0", 0, True),
    ("EBITDA %", "EBITDA %", "pct", 0, False),
)


def _growth(d: ReportData, months: list):
    now = _sum_all(*[d.accounts.get(m, {}).get("Net Deliveries B2C") for m in months])
    before = _sum_all(*[d.accounts.get(add_months(m, -12), {}).get("Net Deliveries B2C") for m in months])
    return now / before - 1 if now is not None and before else None


def _pnl_table(d: ReportData) -> tuple:
    month = d.current["month"]
    earlier = [add_months(month, n) for n in (-2, -1)]
    fiscal = _fiscal_months(d)
    last_closed = d.meta["last_closed_month"]

    def accounts_lines(months):
        return [d.accounts.get(m, {}) for m in months]

    columns = {m: pnl_column(accounts_lines([m])) for m in earlier}
    columns["estimate"] = pnl_column(accounts_lines([month]), current_is_estimate=True)
    columns["budget"] = pnl_column([_budget_line(d, month)], budget=True)
    columns["ytd_estimate"] = pnl_column(accounts_lines(fiscal), current_is_estimate=True)
    columns["ytd_budget"] = pnl_column([_budget_line(d, m) for m in fiscal], budget=True)
    for key, months in [(m, [m]) for m in earlier] + [("estimate", [month]), ("ytd_estimate", fiscal)]:
        columns[key]["growth"] = _growth(d, months)

    at_date = {"Net Deliveries B2C": deliveries(d, d.current),
               "GPU-MSB (£/unit)": margin_mean(d, d.current, "gpu_msb"), "GPU-M (£/unit)": margin_mean(d, d.current, "gpu_m"),
               "GPU-S (£/unit)": margin_mean(d, d.current, "gpu_s"), "GPU-B (£/unit)": gpu_b_per_delivery(d, d.current)}

    def difference(key, a, b):
        here, there = columns[a].get(key), columns[b].get(key)
        return here - there if here is not None and there is not None else None

    rows = []
    for label, key, fmt, indent, bold in _PNL_ROWS:
        values = {"at_date": _clean(at_date.get(key)), **{m: _clean(columns[m].get(key)) for m in earlier},
                  "estimate": _clean(columns["estimate"].get(key)), "budget": _clean(columns["budget"].get(key)),
                  "diff": _clean(difference(key, "estimate", "budget")),
                  "ytd_estimate": _clean(columns["ytd_estimate"].get(key)), "ytd_budget": _clean(columns["ytd_budget"].get(key)),
                  "ytd_diff": _clean(difference(key, "ytd_estimate", "ytd_budget"))}
        rows.append({"label": label, "indent": indent, "bold": bold, "format": fmt, "values": values})
    heads = [{"key": "at_date", "label": "At Date", "group": "extra"}]
    heads += [{"key": m, "label": f"{m} {'Actual' if m <= last_closed else 'Fcst'}", "group": "extra"} for m in earlier]
    heads += [{"key": "estimate", "label": f"{month} Estimate", "group": "extra"}, {"key": "budget", "label": f"{month} Budget", "group": "extra"},
              {"key": "diff", "label": "Diff", "group": "extra"}, {"key": "-", "label": "-", "group": "spacer"},
              {"key": "ytd_estimate", "label": f"YTD From {fiscal[0]} Estimate", "group": "extra"},
              {"key": "ytd_budget", "label": "YTD Budget", "group": "extra"}, {"key": "ytd_diff", "label": "YTD Diff", "group": "extra"}]
    return {"id": "pnl", "title": "EBIT Monthly Summary", "columns": heads, "rows": rows}, columns["estimate"]


def sensitivity_table(d: ReportData, estimate: dict) -> dict:
    """The estimate's result at other margins and volumes, around the month's own figures."""
    constants = d.settings["constants"]
    msb, units, coca, overheads = (estimate.get(k) for k in ("GPU-MSB (£/unit)", "Net Deliveries B2C", "COCA £/u", "Overheads k£"))
    table = {"id": "sensitivity", "title": "EBIT Sensitivity: GPU-MSB Against Deliveries", "columns": [], "rows": []}
    if None in (msb, units, coca, overheads):
        return table
    step_units, step_msb = constants["sensitivity_deliveries_step"], constants["sensitivity_gpu_step"]
    middle = round(units / step_units) * step_units
    volumes = [middle + n * step_units for n in range(-3, 4)]
    table["columns"] = [{"key": f"d{int(v)}", "label": f"{int(v):,}", "group": "extra"} for v in volumes]
    for n in range(-7, 8):
        margin = msb + n * step_msb
        values = {f"d{int(v)}": _clean(margin * v / 1000 + coca * v / 1000 + overheads) for v in volumes}
        table["rows"].append({"label": f"£{margin:,.0f}", "indent": 0, "bold": n == 0, "format": "gbp0", "values": values})
    return table


def section_pnl(d: ReportData) -> dict:
    main, estimate = _pnl_table(d)
    return {"tables": [main, sensitivity_table(d, estimate)]}


# Overview and Average Weekly Volumes have no tables: they hold only the KPI charts chosen for them.
BUILDERS = {
    "pnl": section_pnl,
    "deliveries_objective": section_deliveries_objective,
    "volume_trends": section_volume_trends,
    "inventories": section_inventories,
    "gpu_m_channel": section_gpu_m_channel,
    "gpu_m_b2b2c": section_gpu_m_b2b2c,
    "gpu_s_nature": section_gpu_s_nature,
    "financing": section_financing,
    "breakdown_gpu_ms": section_breakdown,
    "glossary": section_glossary,
}


def fill_sections(report: dict, sources: dict, settings: dict) -> None:
    """Build each section that is switched on. A section that fails becomes a warning,
    so the others still show."""
    d = ReportData(meta=report["meta"], settings=settings, **sources)
    channels = {channel for channel, _ in CHANNELS}
    if not d.margin.empty:
        unknown = set(d.margin["supplier_channel"].dropna().unique()) - channels
        if unknown:
            d.warnings.append("Supplier channels the report does not split out: " + ", ".join(sorted(unknown)))
    for section in report["sections"]:
        builder = BUILDERS.get(section["id"])
        note = settings["notes"].get(section["id"], "")
        built = {}
        if builder:
            try:
                built = builder(d)
            except Exception as e:
                logger.exception("Weekly Business Review report: section %s failed", section["id"])
                d.warnings.append(f"{section['title']} could not be built: {e}")
        section.update({k: v for k, v in built.items() if k != "auto_notes"})
        section["notes"] = ([note] if note else []) + built.get("auto_notes", [])
    report["meta"]["warnings"].extend(dict.fromkeys(d.warnings))
