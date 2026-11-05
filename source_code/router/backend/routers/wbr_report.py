"""Weekly Business Review > Report view: the old review spreadsheet's report,
fed from Snowflake and SharePoint, shown next to the Grid and Slides views.

This file holds the review's dates, its settings and the endpoint; wbr_report_data.py
reads the data and wbr_report_sections.py builds the sections.

The number settings (wbr_report_on and so on) are saved with the KPI settings in
kpi.py. The rest are not numbers, so each has its own key in kpi_layout and is
checked here. The Report view stays admin only until wbr_report_on is 1.
"""
import copy
import datetime
import logging
import re

from fastapi import APIRouter, Depends, Request

from .core import _uk_now, is_admin, require_page_api
from .wbr_report_data import add_months, add_sources, source_statuses
from .wbr_report_sections import fill_sections

logger = logging.getLogger(__name__)
router = APIRouter()

# Months the dbt model weekly_business_review__p keeps back from today.
DBT_WINDOW_MONTHS = 19
DEFAULT_AUTOVIEW_START = "2025-11"

_UPDATE_DATE_KEY = "wbr_report_update_date"
_LAST_CLOSED_KEY = "wbr_report_last_closed_month"
_AUTOVIEW_START_KEY = "wbr_report_autoview_start"
_SECTIONS_KEY = "wbr_report_sections"
_CHART_SETTINGS_KEY = "wbr_report_chart_settings"

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_COLOUR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

# In slide order. Stored settings keep the order and on/off, never the titles.
SECTIONS = (
    ("overview", "1- Overview"),
    ("pnl", "2- P&L"),
    ("deliveries_objective", "3-a) Deliveries Vs Objective"),
    ("volumes", "3-b) Average Weekly Volumes"),
    ("volume_trends", "3-c) Average Trend Volumes Per Week"),
    ("inventories", "4- Inventories EoP"),
    ("gpu_m_channel", "5-a) GPU-M"),
    ("gpu_m_b2b2c", "5-b) GPU-M - Focus B2B2C"),
    ("gpu_s_nature", "5-b) GPU-S"),
    ("financing", "5-c) Focus Financing Commissioned"),
    ("breakdown_gpu_ms", "Appendix - Breakdown GPU-MS"),
    ("glossary", "Glossary & Sources Of Data"),
)
_SECTION_TITLES = dict(SECTIONS)

# Everything a section shows that is not data: all editable in Settings > Report.
# A chart's axis is the spreadsheet's Floor and Sky. A percent chart's axis is a
# fraction (0.8 = 80%).
DEFAULT_CHART_SETTINGS = {
    "charts": {
        "net_deliveries": {"title": "Net Deliveries B2C (Avg/Week)", "min": 0, "max": 800},
        "gpu_msb": {"title": "GPU-MSB (£/Unit)", "min": -4000, "max": 5000},
        "inventories": {"title": "Inventories B2C", "min": 0, "max": 5000},
        "days_inventories": {"title": "Days Of Inventories", "min": 20, "max": 150},
        "sleeping": {"title": "Sleeping Stock B2C (Excl. Courtesy)", "min": 0, "max": 1000},
        "for_sale_pct": {"title": "% Inventories For Sale", "min": 0, "max": 0.8},
        "purchases": {"title": "Purchases (Avg/Week)", "min": 0, "max": 1000},
        "total_recon": {"title": "Total Recon Volume (Avg/Week)", "min": 0, "max": 1000},
        "net_orders": {"title": "Net Orders B2C (Avg/Week)", "min": 0, "max": 700},
        "rotation_sutton": {"title": "Rotation Sutton Fields (Days)", "min": 0, "max": 20, "target": 4},
        "rotation_goole": {"title": "Rotation Goole (Days)", "min": 0, "max": 20, "target": 4},
        "rotation_sites": {"title": "Rotation Sites (Days)", "min": 0, "max": 20, "target": 6},
        "mtd_deliveries": {"title": "Net Deliveries B2C Month To Date (New)"},
        "coverage": {"title": "Stock Coverage B2C (Days)"},
        "waterfall_b2b2c": {"title": "GPU-M Waterfall - B2B2C"},
        "waterfall_px": {"title": "GPU-M Waterfall - PX"},
        "waterfall_other": {"title": "GPU-M Waterfall - C2B Other"},
    },
    "coverage_targets": {"upstream": 5, "in_prep": 4, "for_sale": 20, "sold_not_delivered": 5, "total": 34},
    "sleeping_targets": {"upstream": "<10 Days", "in_prep": "<10 Days", "on_sale": "<60 Days",
                         "sold_not_delivered": "<10 Days", "trade_hold": "<10 Days"},
    "constants": {"deliveries_days_per_month": 30},
    "trend_basis": "last_week",
    "appearance": {"actual": "#1f6fb2", "n1": "#64748b", "series_three": "#b45309", "budget_line": "#6b8fb5",
                   "below_budget": "#dbe7f3", "above_budget": "#f6f8fb", "chart_height": 240, "table_font_px": 12},
    "notes": {
        "overview": "The margin calculated for the current month excludes the impact of write-downs and RFL/V5s (costs and revenues).",
        "volumes": "Month figures are weekly averages.",
        "inventories": "The date used for the end of the month is a proxy: the closest week ending on a Sunday.",
    },
    "titles": {},
}
_TREND_BASES = ("last_week", "months")
_APPEARANCE_LIMITS = {"chart_height": (120, 600), "table_font_px": (9, 18)}
_TEXT_MAX = {"title": 100, "note": 500, "target": 20}


# ── Dates ────────────────────────────────────────────────────────────────────

def last_sunday(today: datetime.date) -> datetime.date:
    """The most recent Sunday on or before today."""
    return today - datetime.timedelta(days=today.isoweekday() % 7)


def _week(end: datetime.date) -> dict:
    """The Monday to Sunday week ending on end, and the same ISO week a year
    earlier. Rows are put into weeks by these dates, not by dbt's week keys,
    which have the wrong year for a week starting in December. A week 53 has no
    match in a year with 52 weeks, so its N-1 dates are None."""
    start = end - datetime.timedelta(days=6)
    year, number, _ = end.isocalendar()
    try:
        n1_start = datetime.date.fromisocalendar(year - 1, number, 1)
        n1_end = n1_start + datetime.timedelta(days=6)
    except ValueError:
        n1_start = n1_end = None
    return {"label": f"W{number:02d}", "key": f"{year}-W{number:02d}",
            "start": start.isoformat(), "end": end.isoformat(),
            "n1_start": n1_start.isoformat() if n1_start else None,
            "n1_end": n1_end.isoformat() if n1_end else None}


def report_periods(update_date: datetime.date, months: int, weeks: int) -> dict:
    """The complete months before the review's month, that month, and the weeks
    ending on the review's Sunday, oldest first."""
    current = update_date.strftime("%Y-%m")
    shown = [add_months(current, -n) for n in range(months, 0, -1)]
    ends = [update_date - datetime.timedelta(days=7 * n) for n in range(weeks - 1, -1, -1)]
    return {"months": shown, "current_month": current, "weeks": [_week(end) for end in ends],
            "n1_months": {m: add_months(m, -12) for m in shown + [current]}}


def month_kinds(months: list, last_closed: str) -> dict:
    """{month: 'actual' or 'fcst'}: actual up to and including the last closed month."""
    return {m: "actual" if m <= last_closed else "fcst" for m in months}


def n1_in_dbt_window(update_date: datetime.date, months: int, today: datetime.date) -> bool:
    """Whether N-1 of the earliest month shown is inside the dbt model's window."""
    earliest = add_months(update_date.strftime("%Y-%m"), -(months + 12))
    return earliest >= add_months(today.strftime("%Y-%m"), -DBT_WINDOW_MONTHS)


# ── Settings that are not numbers ────────────────────────────────────────────

def clean_month(value, label: str) -> str:
    """'YYYY-MM' or blank. Raises ValueError with a message for the page."""
    value = str(value or "").strip()
    if value and not _MONTH_RE.fullmatch(value):
        raise ValueError(f"{label} must be a month like 2026-09")
    return value


def clean_update_date(value) -> str:
    """An ISO date on a Sunday, or blank."""
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        parsed = datetime.date.fromisoformat(value)
    except ValueError:
        raise ValueError("Review Date must be a date like 2026-10-25")
    if parsed.isoweekday() != 7:
        raise ValueError("Review Date must be a Sunday")
    return parsed.isoformat()


def clean_sections(value) -> list:
    """[{id, on}] in the order given. A section missing from the list goes on
    the end, switched on, so a new section is not lost from an old saved list."""
    if value is None:
        value = []
    if not isinstance(value, list):
        raise ValueError("Sections are not in the right form")
    out, seen = [], set()
    for entry in value:
        section = entry.get("id") if isinstance(entry, dict) else None
        if section not in _SECTION_TITLES:
            raise ValueError("Unknown report section")
        if section in seen:
            raise ValueError("A report section is listed twice")
        seen.add(section)
        out.append({"id": section, "on": bool(entry.get("on", True))})
    out.extend({"id": section, "on": True} for section, _ in SECTIONS if section not in seen)
    return out


def _clean_number(value, default, label: str) -> float:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number")
    if number != number or number in (float("inf"), float("-inf")):
        raise ValueError(f"{label} must be a number")
    return number


def _clean_text(value, default: str, limit: int) -> str:
    text = str(value if value is not None else default).strip()
    return text[:limit]


def _given(raw: dict, key: str) -> dict:
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def clean_chart_settings(raw) -> dict:
    """Targets, axis limits, constants, note texts and titles, over the defaults, so a
    chart added later is not lost from an old saved list. Raises ValueError with a message."""
    raw = raw if isinstance(raw, dict) else {}
    out = copy.deepcopy(DEFAULT_CHART_SETTINGS)

    given_charts = _given(raw, "charts")
    for chart_id, chart in out["charts"].items():
        given = given_charts.get(chart_id)
        given = given if isinstance(given, dict) else {}
        chart["title"] = _clean_text(given.get("title"), chart["title"], _TEXT_MAX["title"]) or chart["title"]
        for key in ("min", "max", "target"):
            if key in chart:
                chart[key] = _clean_number(given.get(key), chart[key], f"{chart['title']} {key.title()}")
        if "min" in chart and chart["min"] >= chart["max"]:
            raise ValueError(f"{chart['title']}: Min must be below Max")

    given_targets = _given(raw, "coverage_targets")
    for key, default in out["coverage_targets"].items():
        out["coverage_targets"][key] = _clean_number(given_targets.get(key), default, "Coverage Target")
    given_sleeping = _given(raw, "sleeping_targets")
    for key, default in out["sleeping_targets"].items():
        out["sleeping_targets"][key] = _clean_text(given_sleeping.get(key), default, _TEXT_MAX["target"])

    days = _clean_number(_given(raw, "constants").get("deliveries_days_per_month"),
                         out["constants"]["deliveries_days_per_month"], "Days Per Month For Deliveries")
    if not 28 <= days <= 31:
        raise ValueError("Days Per Month For Deliveries must be from 28 to 31")
    out["constants"]["deliveries_days_per_month"] = days

    basis = raw.get("trend_basis", out["trend_basis"])
    if basis not in _TREND_BASES:
        raise ValueError("Trend must compare with the week before or the months shown")
    out["trend_basis"] = basis

    given_look = _given(raw, "appearance")
    for key, default in out["appearance"].items():
        if key in _APPEARANCE_LIMITS:
            low, high = _APPEARANCE_LIMITS[key]
            number = _clean_number(given_look.get(key), default, key.replace("_", " ").title())
            if not low <= number <= high:
                raise ValueError(f"{key.replace('_', ' ').title()} must be from {low} to {high}")
            out["appearance"][key] = number
        else:
            colour = given_look.get(key) or default
            if not isinstance(colour, str) or not _COLOUR_RE.fullmatch(colour):
                raise ValueError("Colours must be like #1f6fb2")
            out["appearance"][key] = colour

    given_notes, given_titles = _given(raw, "notes"), _given(raw, "titles")
    for section, _ in SECTIONS:
        if section in given_notes:
            out["notes"][section] = _clean_text(given_notes[section], "", _TEXT_MAX["note"])
        title = _clean_text(given_titles.get(section), "", _TEXT_MAX["title"])
        if title and title != _SECTION_TITLES[section]:
            out["titles"][section] = title
    return out


def clean_report_settings(raw: dict) -> dict:
    """The text and list settings, checked. Raises ValueError with a message."""
    return {
        _UPDATE_DATE_KEY: clean_update_date(raw.get("update_date")),
        _LAST_CLOSED_KEY: clean_month(raw.get("last_closed_month"), "Last Closed Month"),
        _AUTOVIEW_START_KEY: clean_month(raw.get("autoview_start"), "AutoView Costs Start") or DEFAULT_AUTOVIEW_START,
        _SECTIONS_KEY: clean_sections(raw.get("sections")),
        _CHART_SETTINGS_KEY: clean_chart_settings(raw.get("chart_settings")),
    }


def _stored_chart_settings(stored) -> dict:
    """What is saved, or the defaults if it cannot be used."""
    try:
        return clean_chart_settings(stored)
    except ValueError:
        return clean_chart_settings(None)


def _stored_sections(stored) -> list:
    """The saved list, or the default if what is saved cannot be used."""
    try:
        return clean_sections(stored)
    except ValueError:
        return clean_sections(None)


def _stored_date(value) -> str:
    try:
        return clean_update_date(value)
    except ValueError:
        return ""


def _stored_month(value, default: str = "") -> str:
    try:
        return clean_month(value, "Month") or default
    except ValueError:
        return default


def _read_settings() -> tuple:
    """(number settings, text and list settings) from kpi_layout."""
    from .kpi import _kpi_settings, _read_kpi_layout_values
    stored = _read_kpi_layout_values(["settings", _UPDATE_DATE_KEY, _LAST_CLOSED_KEY,
                                      _AUTOVIEW_START_KEY, _SECTIONS_KEY, _CHART_SETTINGS_KEY])
    return _kpi_settings(stored.get("settings")), {
        "update_date": _stored_date(stored.get(_UPDATE_DATE_KEY)),
        "last_closed_month": _stored_month(stored.get(_LAST_CLOSED_KEY)),
        "autoview_start": _stored_month(stored.get(_AUTOVIEW_START_KEY), DEFAULT_AUTOVIEW_START),
        "sections": _stored_sections(stored.get(_SECTIONS_KEY)),
        "chart_settings": _stored_chart_settings(stored.get(_CHART_SETTINGS_KEY)),
    }


# ── The report ───────────────────────────────────────────────────────────────

def build_report(numbers: dict, text: dict, today: datetime.date, update_date: str = "") -> dict:
    """The page's payload. update_date, when given, replaces the saved date."""
    warnings = []
    chosen = update_date or text["update_date"]
    review = datetime.date.fromisoformat(chosen) if chosen else last_sunday(today)
    months, weeks = int(numbers["wbr_report_months"]), int(numbers["wbr_report_weeks"])
    periods = report_periods(review, months, weeks)

    last_closed = text["last_closed_month"]
    if not last_closed:
        last_closed = add_months(periods["current_month"], -1)
        warnings.append(f"Last Closed Month is not set, so {last_closed} has been used.")
    n1_available = n1_in_dbt_window(review, months, today)
    if not n1_available:
        warnings.append("N-1 from the sales and purchases data is blank: this date and number of "
                        "months go back further than the data kept.")

    shown = periods["months"] + [periods["current_month"]]
    titles = {**_SECTION_TITLES, **text.get("chart_settings", {}).get("titles", {})}
    look = text.get("chart_settings", DEFAULT_CHART_SETTINGS)["appearance"]
    return {
        "status": "ok",
        "meta": {
            "update_date": review.isoformat(),
            **periods,
            "month_kinds": month_kinds(shown, last_closed),
            "last_closed_month": last_closed,
            "autoview_start": text["autoview_start"],
            "n1_available": n1_available,
            "weeks_per_month": numbers["wbr_report_weeks_per_month"],
            "recon_default": numbers["wbr_report_recon_default"],
            "recon_floor": numbers["wbr_report_recon_floor"],
            "appearance": look,
            "warnings": warnings,
        },
        "sections": [{"id": s["id"], "title": titles[s["id"]], "tables": [], "charts": [], "notes": [],
                      "more": {"tables": [], "charts": []}}
                     for s in text["sections"] if s["on"]],
    }


@router.get("/weekly-business-review/report")
def weekly_business_review_report(request: Request, update_date: str = "", refresh: int = 0,
                                  user: str = Depends(require_page_api("weekly_business_review"))):
    """The Report view's data. Admin only until wbr_report_on is 1. Only the admin
    can ask for a date other than the saved one, or for the data to be fetched again."""
    admin = is_admin(user)
    try:
        numbers, text = _read_settings()
    except Exception as e:
        logger.error(f"Weekly Business Review report settings read failed: {e}")
        return {"status": "error", "message": str(e)}
    if not admin and int(numbers["wbr_report_on"]) != 1:
        return {"status": "error", "message": "The Report view is not available yet"}
    try:
        chosen = clean_update_date(update_date) if admin else ""
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    report = build_report(numbers, text, _uk_now().date(), chosen)
    fill_sections(report, add_sources(report, admin and refresh == 1), text["chart_settings"])
    return report


@router.get("/weekly-business-review/report/settings")
def weekly_business_review_report_settings(request: Request, user: str = Depends(require_page_api("weekly_business_review"))):
    """The Report tab of the review's Settings popup. Admin only."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can see the report settings"}
    try:
        _, text = _read_settings()
    except Exception as e:
        logger.error(f"Weekly Business Review report settings read failed: {e}")
        return {"status": "error", "message": str(e)}
    try:
        sources = source_statuses()
    except Exception as e:
        logger.warning(f"Weekly Business Review report file status read failed: {e}")
        sources = []
    return {"status": "ok", "settings": text, "titles": _SECTION_TITLES, "sources": sources}


@router.post("/weekly-business-review/report/settings")
def weekly_business_review_report_settings_save(request: Request, payload: dict,
                                                user: str = Depends(require_page_api("weekly_business_review"))):
    """Save the Report tab's dates and section list. Admin only."""
    from .kpi import _write_kpi_layout_values
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the report settings"}
    try:
        values = clean_report_settings((payload or {}).get("settings") or {})
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    try:
        _write_kpi_layout_values(values)
    except Exception as e:
        logger.error(f"Weekly Business Review report settings save failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok"}
