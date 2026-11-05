"""Weekly Business Review > Report view: one function per section, turning the data
wbr_report_data.py reads into the tables and charts wbr_report.js draws.

Everything here is pure, so it can be tested with small frames. Formulas follow the
old review spreadsheet's tabs; where a figure could not be matched to its formula
the plan's discrepancy list says why. Numbers are sent raw: the page formats them.
"""
import calendar
import datetime
import logging
import math
from dataclasses import dataclass, field

import pandas as pd

from .wbr_report_data import add_months, autoview_costs_usable, month_end_proxy, month_end_weeks, volume_key

logger = logging.getLogger(__name__)

SPACER = {"kind": "spacer", "key": "-", "label": "-"}
CANCELLED = "STATUS_CANCELLED"
DAYS_PER_MONTH = 365 / 12

# (supplier_channel, name on the page). Kept in code: these are the values in the data.
CHANNELS = (("B2B2C", "B2B2C"), ("C2B2C - PX", "C2B2C - PX"),
            ("C2B2C - Other", "C2B2C - Other"), ("Consignment", "Consignment"))

STOCK = "stock new"
RECON_GOOLE, RECON_HULL, RECON_SITES = "Goole Sent to Trans.", "Hull Sent to Trans.", "Sites Live"

# Budget lines, as (section, label) on the Budget tab.
B_DELIVERIES = ("P&L", "Net Deliveries B2C")
B_GPU_MSB = ("P&L", "GPU-MSB (£/unit)")
B_PURCHASES = ("Volume Target", "Purchases")
B_PREP = ("Volume Target", "Prep Center Prod")
B_SITES = ("Volume Target", "Recon Prod Sites")
B_ORDERS = ("Volume Target", "Net Orders")
B_DAYS = ("Cash Target", "Days of inventories")
B_FOR_SALE = ("Cash Target", "% inventories for sale")
B_OLD = ("Cash Target", "% old stock")


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


def _days_in(month: str) -> int:
    return calendar.monthrange(int(month[:4]), int(month[5:7]))[1]


# ── The review's periods ─────────────────────────────────────────────────────

def _month_period(month: str) -> dict:
    return {"kind": "month", "key": month, "label": month, "month": month}


def _week_period(week: dict) -> dict:
    return {"kind": "week", "key": week["key"], "label": week["label"], "end": week["end"],
            "start": week["start"], "month": week["end"][:7],
            "n1_start": week["n1_start"], "n1_end": week["n1_end"]}


def n1_period(p: dict):
    """The same period a year earlier. None for a week 53, which has no match."""
    if p["kind"] == "month":
        return _month_period(add_months(p["month"], -12))
    if not p["n1_end"]:
        return None
    return {"kind": "week", "end": p["n1_end"], "start": p["n1_start"], "month": p["n1_end"][:7]}


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
    return rows if channel is None or rows.empty else rows[rows["supplier_channel"] == channel]


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


def stock_n1(d: ReportData, p: dict, key: tuple):
    """A stock row a year earlier: a month with two end weeks averages them, as the
    spreadsheet's N-1 does, while its own months take the later week."""
    row = d.volume["rows"].get(key)
    if not row:
        return None
    weeks = month_end_weeks(d.volume_weeks, p["month"]) if p["kind"] == "month" else [p["end"]]
    values = [row[w] for w in weeks if row.get(w) is not None]
    return sum(values) / len(values) if values else None


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


def _budget_per_week(d: ReportData, line: tuple, p: dict):
    """A month's budget as a week: the month's figure / its days x 7, using the month
    the week's Sunday falls in."""
    value = _budget(d, line, p["month"])
    return value / _days_in(p["month"]) * 7 if value is not None else None


def inventories_budget(d: ReportData, p: dict):
    weekly, days = _budget_per_week(d, B_DELIVERIES, p), _budget(d, B_DAYS, p["month"])
    return weekly / 7 * days if weekly is not None and days is not None else None


def _n1(d: ReportData, p: dict, figure):
    """A figure from the sales or purchases data for the period a year earlier."""
    earlier = n1_period(p)
    return figure(earlier) if earlier and d.meta["n1_available"] else None


def _n1_costs(d: ReportData, p: dict, figure):
    """The same for a figure using AutoView costs: blank before AutoView data starts,
    never worked out from the default recon cost."""
    earlier = n1_period(p)
    if not earlier:
        return None
    first = earlier["start"][:7] if earlier["kind"] == "week" else earlier["month"]
    if not autoview_costs_usable(first, d.meta["autoview_start"]):
        return None
    return _n1(d, p, figure)


def _accounts_n1(d: ReportData, p: dict, line: str):
    return d.accounts.get(add_months(p["month"], -12), {}).get(line)


# ── Tables and charts ────────────────────────────────────────────────────────

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


def band_chart(d: ReportData, chart_id: str, unit: str, actual, n1, budget, budget_label: str = "Budget") -> dict:
    """A line against a budget band, as the spreadsheet's charts: three months, a gap,
    then the weeks. The gap keeps the budget going so the band does not break."""
    config = d.settings["charts"][chart_id]
    series = {"actual": [], "n1": [], "budget": []}
    held = None
    for p in d.chart_periods:
        if p["kind"] == "spacer":
            series["actual"].append(None)
            series["n1"].append(None)
            series["budget"].append(held)
            continue
        held = _clean(budget(p))
        series["actual"].append(_clean(actual(p)))
        series["n1"].append(_clean(n1(p)))
        series["budget"].append(held)
    return {"id": chart_id, "type": "band", "title": config["title"], "unit": unit,
            "labels": [p["label"] for p in d.chart_periods], "series": series, "budget_label": budget_label,
            "axis": {"min": config["min"], "max": config["max"]}}


def _lines_chart(d: ReportData, chart_id: str, unit: str, labels: list, series: list) -> dict:
    return {"id": chart_id, "type": "lines", "title": d.settings["charts"][chart_id]["title"], "unit": unit,
            "labels": labels, "series": series}


def chart_net_deliveries(d: ReportData) -> dict:
    per_week = d.deliveries_weeks_per_month

    def n1(p):
        if p["kind"] == "month":
            return _ratio(_accounts_n1(d, p, "Net Deliveries B2C"), per_week)
        return _n1(d, p, lambda e: deliveries(d, e))
    return band_chart(d, "net_deliveries", "int", lambda p: deliveries_per_week(d, p), n1,
                      lambda p: _budget_per_week(d, B_DELIVERIES, p))


def chart_gpu_msb(d: ReportData) -> dict:
    def n1(p):
        if p["kind"] == "month":
            return _accounts_n1(d, p, "GPU-MSB (£/unit)")
        return _n1_costs(d, p, lambda e: _mean(_delivered(d, e), "gpu_msb_pq"))
    return band_chart(d, "gpu_msb", "gbp0", lambda p: _mean(_delivered(d, p), "gpu_msb_pq"), n1,
                      lambda p: _budget(d, B_GPU_MSB, p["month"]))


def _no_n1(_p):
    # The spreadsheet's formula for this points at a row that does not exist.
    return None


def chart_inventories(d: ReportData) -> dict:
    return band_chart(d, "inventories", "int", lambda p: total_b2c(d, p), _no_n1, lambda p: inventories_budget(d, p))


def chart_days_inventories(d: ReportData) -> dict:
    return band_chart(d, "days_inventories", "days", lambda p: coverage_days(d, p, total_b2c(d, p)), _no_n1,
                      lambda p: _budget(d, B_DAYS, p["month"]))


def chart_for_sale(d: ReportData) -> dict:
    on_sale = _retail("On sale stock Total", "on_sale")
    for_sale_pct = volume_key("stock", "For sale %")
    return band_chart(d, "for_sale_pct", "pct0", lambda p: _ratio(stock_at(d, p, on_sale), total_b2c(d, p)),
                      lambda p: _n1(d, p, lambda e: stock_n1(d, e, for_sale_pct)),
                      lambda p: _budget(d, B_FOR_SALE, p["month"]))


def chart_sleeping(d: ReportData) -> dict:
    older = volume_key("old stock (not including courtesy)", "Older than 60 days")

    def budget(p):
        share, stock = _budget(d, B_OLD, p["month"]), inventories_budget(d, p)
        return share * stock if share is not None and stock is not None else None
    return band_chart(d, "sleeping", "int", lambda p: sleeping_total(d, p),
                      lambda p: _n1(d, p, lambda e: stock_n1(d, e, older)), budget)


def chart_purchases(d: ReportData) -> dict:
    def actual(p):
        return _per_week(d, p, purchases_count(d, p))
    return band_chart(d, "purchases", "int", actual, lambda p: _n1(d, p, actual),
                      lambda p: _budget_per_week(d, B_PURCHASES, p))


def chart_total_recon(d: ReportData) -> dict:
    def actual(p):
        return recon_per_week(d, p, RECON_GOOLE, RECON_HULL, RECON_SITES)

    def n1(p):
        earlier = n1_period(p)
        return actual(earlier) if earlier else None
    return band_chart(d, "total_recon", "int", actual, n1,
                      lambda p: _sum_all(_budget_per_week(d, B_PREP, p), _budget_per_week(d, B_SITES, p)))


def chart_net_orders(d: ReportData) -> dict:
    def actual(p):
        return _per_week(d, p, net_orders(d, p))
    return band_chart(d, "net_orders", "int", actual, lambda p: _n1(d, p, actual),
                      lambda p: _budget_per_week(d, B_ORDERS, p))


def chart_rotation(d: ReportData, chart_id: str, site_key: str, block_label: str) -> dict:
    target = d.settings["charts"][chart_id]["target"]
    return band_chart(d, chart_id, "days", lambda p: refurb_days(d, p, site_key, block_label), _no_n1,
                      lambda p: target, "Target")


def chart_mtd_deliveries(d: ReportData) -> dict:
    """Cumulative deliveries by day in the review's month, against the budget's run
    rate and the same month a year earlier. New: the slide's own chart link is broken."""
    month = d.current["month"]
    days = _days_in(month)
    budget = _budget(d, B_DELIVERIES, month)

    def cumulative(in_month: str, last_day: int):
        rows = _delivered(d, _month_period(in_month))
        by_day = {}
        if not rows.empty:
            dates = rows["invoice_posted_date"].astype(str).str[8:10]
            by_day = rows.groupby(dates)["deliveries"].sum().to_dict()
        out, total = [], 0.0
        for day in range(1, days + 1):
            total += float(by_day.get(f"{day:02d}", 0))
            out.append(total if day <= last_day else None)
        return out

    earlier = add_months(month, -12)
    n1 = cumulative(earlier, _days_in(earlier)) if d.meta["n1_available"] and not d.margin.empty else [None] * days
    actual = cumulative(month, d.review_day) if not d.margin.empty else [None] * days
    return _lines_chart(d, "mtd_deliveries", "int", [str(day) for day in range(1, days + 1)], [
        {"name": "Actual", "kind": "actual", "data": actual},
        {"name": "Budget Run Rate", "kind": "series_three",
         "data": [_clean(budget * day / days) if budget is not None else None for day in range(1, days + 1)]},
        {"name": "N-1", "kind": "n1", "dashed": True, "data": n1},
    ])


def chart_waterfall(d: ReportData, chart_id: str, channel: str, daily_column: str) -> dict:
    """GPU-M of the same channel at three stages: bought, first advertised, ordered."""
    periods = d.chart_periods
    def data(figure):
        return [None if p["kind"] == "spacer" else _clean(figure(p)) for p in periods]
    return _lines_chart(d, chart_id, "gbp0", [p["label"] for p in periods], [
        {"name": "Purchases", "kind": "actual", "data": data(lambda p: _purchased_gpu(d, p, channel))},
        {"name": "Published", "kind": "series_three", "data": data(lambda p: first_day_gpu(d, p, daily_column))},
        {"name": "Orders", "kind": "n1", "data": data(lambda p: _mean(_channel(_ordered(d, p), channel), "gpu_m_pq"))},
    ])


def chart_coverage(d: ReportData) -> dict:
    """Days of stock per stage, for the months and the latest week, against target."""
    targets = d.settings["coverage_targets"]
    periods = d.months + d.weeks[-1:]
    stages = _coverage_stages(d)
    return {"id": "coverage", "type": "bars", "title": d.settings["charts"]["coverage"]["title"], "unit": "days",
            "labels": [name for _, name, _ in stages],
            "series": [{"name": p["label"], "data": [_clean(coverage_days(d, p, units(p))) for _, _, units in stages]}
                       for p in periods],
            "target": [targets[key] for key, _, _ in stages]}


def _coverage_stages(d: ReportData) -> list:
    def stock(key):
        return lambda p: stock_at(d, p, key)
    return [("upstream", "Upstream Stock", stock(_retail("Upstream stock Total", "upstream"))),
            ("in_prep", "In Prep", stock(_retail("In prep Total", "in_prep"))),
            ("for_sale", "For Sale", stock(_retail("On sale stock Total", "on_sale"))),
            ("sold_not_delivered", "Sold Not Delivered", stock(_retail("Ordered"))),
            ("total", "Total", lambda p: total_b2c(d, p))]


# ── Sections ─────────────────────────────────────────────────────────────────

def section_overview(d: ReportData) -> dict:
    return {"charts": [chart_net_deliveries(d), chart_gpu_msb(d), chart_inventories(d), chart_days_inventories(d)]}


def section_deliveries_objective(d: ReportData) -> dict:
    periods = d.table_periods
    budget = _budget(d, B_DELIVERIES, d.current["month"])
    mini = {"id": "deliveries_budget", "title": "", "columns": [{"key": "budget", "label": d.current["month"], "group": "extra"}],
            "rows": [{"label": "Net Deliveries Budget #", "indent": 0, "bold": False, "format": "int",
                      "values": {"budget": _clean(budget)}}]}
    names = {"B2B2C": "B2B2C", "C2B2C - PX": "C2B2C - PX", "C2B2C - Other": "Other C2B2C", "Consignment": "Consignment"}
    rows = [_line(f"Deliveries {names[channel]}", periods, lambda p, c=channel: deliveries(d, p, c), indent=1)
            for channel, _ in CHANNELS]
    rows.append(_line("Net Deliveries B2C", periods, lambda p: deliveries(d, p), bold=True))
    rows += [_line(f"% Mix {names[channel]}", periods,
                   lambda p, c=channel: _ratio(deliveries(d, p, c), deliveries(d, p)), "pct0", indent=1)
             for channel, _ in CHANNELS]
    rows.append(_line("% Mix Deliveries", periods,
                      lambda p: _ratio(_sum_all(*[deliveries(d, p, c) for c, _ in CHANNELS]), deliveries(d, p)), "pct0", bold=True))
    return {"tables": [mini, _table("deliveries_channel", "Deliveries B2C By Channel", periods, rows)],
            "charts": [chart_mtd_deliveries(d)]}


def section_volumes(d: ReportData) -> dict:
    return {"charts": [chart_purchases(d), chart_total_recon(d), chart_net_orders(d), chart_net_deliveries(d)]}


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
               _channel_rows(shown, lambda p, c: _mean(_channel(_ordered(d, p), c), "gpu_m_pq"), "gbp0",
                             lambda p: _mean(_ordered(d, p), "gpu_m_pq"))),
        _table("adverts_published", "Adverts Published - Month Figures Are Weekly Averages", shown,
               _channel_rows(shown, lambda p, c: None if d.purchases.empty
                             else _per_week(d, p, float(len(_channel(_advertised(d, p), c)))))),
    ]
    more_charts = [chart_waterfall(d, "waterfall_b2b2c", "B2B2C", "avg_gpu_m_b2b2c"),
                   chart_waterfall(d, "waterfall_px", "C2B2C - PX", "avg_gpu_m_c2b2c_px"),
                   chart_waterfall(d, "waterfall_other", "C2B2C - Other", "avg_gpu_m_c2b2c_other")]
    return {"tables": [main], "auto_notes": [_trend_note(d)], "more": {"tables": more_tables, "charts": more_charts}}


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
        "charts": [chart_inventories(d), chart_days_inventories(d), chart_sleeping(d), chart_coverage(d)],
        "more": {"tables": [_table("stocks_status", "Stocks By Status", periods, slide_rows)],
                 "charts": [chart_for_sale(d),
                            chart_rotation(d, "rotation_sutton", RECON_HULL, "Sutton Fields"),
                            chart_rotation(d, "rotation_goole", RECON_GOOLE, "Goole"),
                            chart_rotation(d, "rotation_sites", RECON_SITES, "Sites")]},
    }


BUILDERS = {
    "overview": section_overview,
    "deliveries_objective": section_deliveries_objective,
    "volumes": section_volumes,
    "volume_trends": section_volume_trends,
    "inventories": section_inventories,
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
