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
from .wbr_report_sections import default_glossary, fill_sections

logger = logging.getLogger(__name__)
router = APIRouter()

DEFAULT_AUTOVIEW_START = "2025-12"

_UPDATE_DATE_KEY = "wbr_report_update_date"
_LAST_CLOSED_KEY = "wbr_report_last_closed_month"
_AUTOVIEW_START_KEY = "wbr_report_autoview_start"
_SECTIONS_KEY = "wbr_report_sections"
_CHART_SETTINGS_KEY = "wbr_report_chart_settings"

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

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
_CHARTS_PER_SECTION_MAX = 40

# Everything a section shows that is not data: all editable in Settings > Report.
# Charts are KPI charts, so their titles, axes and targets are edited on the KPI page.
DEFAULT_CHART_SETTINGS = {
    "coverage_targets": {"upstream": 5, "in_prep": 4, "for_sale": 20, "sold_not_delivered": 5, "total": 34},
    "sleeping_targets": {"upstream": "<10 Days", "in_prep": "<10 Days", "on_sale": "<60 Days",
                         "sold_not_delivered": "<10 Days", "trade_hold": "<10 Days"},
    "constants": {"deliveries_days_per_month": 30, "fiscal_year_start_month": 10, "top_suppliers": 10, "sites_shown": 15,
                  "finance_companies_shown": 5, "sensitivity_gpu_step": 10, "sensitivity_deliveries_step": 50},
    "trend_basis": "last_week",
    "appearance": {"chart_width": 420, "chart_height": 335, "table_font_px": 12},
    "notes": {
        "overview": "The margin calculated for the current month excludes the impact of write-downs and RFL/V5s (costs and revenues).",
        "pnl": "The GPU calculated for the current month excludes the impact of write-downs and RFL/V5s. Cost of sales are standard based except for refurb costs.",
        "volumes": "Month figures are weekly averages.",
        "inventories": "The date used for the end of the month is a proxy: the closest week ending on a Sunday.",
        "gpu_m_channel": "The GPU calculated for the current month excludes the impact of write-downs and RFL/V5s. Cost of sales are standard based except for refurb costs.",
        "gpu_m_b2b2c": "The GPU calculated for the current month excludes the impact of write-downs and RFL/V5s. Cost of sales are standard based except for refurb costs.",
        "gpu_s_nature": "The GPU calculated for the current month excludes the impact of write-downs and RFL/V5s.",
    },
    "titles": {},
    "glossary": "",
}
_TREND_BASES = ("last_week", "months")
_APPEARANCE_LIMITS = {"chart_width": (240, 1200), "chart_height": (120, 600), "table_font_px": (9, 18)}
_TEXT_MAX = {"title": 100, "note": 500, "target": 20, "glossary": 20000}
# (lowest, highest, name on the page).
_CONSTANT_LIMITS = {
    "deliveries_days_per_month": (28, 31, "Days Per Month For Deliveries"),
    "fiscal_year_start_month": (1, 12, "Financial Year Starts In Month"),
    "top_suppliers": (1, 30, "Top Suppliers Shown"),
    "sites_shown": (1, 30, "Sites Shown"),
    "finance_companies_shown": (1, 15, "Finance Companies Shown"),
    "sensitivity_gpu_step": (1, 500, "Sensitivity GPU-MSB Step"),
    "sensitivity_deliveries_step": (1, 500, "Sensitivity Deliveries Step"),
}


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
    """Targets, constants, note texts and titles, over the defaults, so a setting added
    later is not lost from an old saved list. Raises ValueError with a message."""
    raw = raw if isinstance(raw, dict) else {}
    out = copy.deepcopy(DEFAULT_CHART_SETTINGS)

    given_targets = _given(raw, "coverage_targets")
    for key, default in out["coverage_targets"].items():
        out["coverage_targets"][key] = _clean_number(given_targets.get(key), default, "Coverage Target")
    given_sleeping = _given(raw, "sleeping_targets")
    for key, default in out["sleeping_targets"].items():
        out["sleeping_targets"][key] = _clean_text(given_sleeping.get(key), default, _TEXT_MAX["target"])

    given_constants = _given(raw, "constants")
    for key, (low, high, name) in _CONSTANT_LIMITS.items():
        number = _clean_number(given_constants.get(key), out["constants"][key], name)
        if not low <= number <= high:
            raise ValueError(f"{name} must be from {low} to {high}")
        out["constants"][key] = number

    basis = raw.get("trend_basis", out["trend_basis"])
    if basis not in _TREND_BASES:
        raise ValueError("Trend must compare with the week before or the months shown")
    out["trend_basis"] = basis

    given_look = _given(raw, "appearance")
    for key, default in out["appearance"].items():
        low, high = _APPEARANCE_LIMITS[key]
        number = _clean_number(given_look.get(key), default, key.replace("_", " ").title())
        if not low <= number <= high:
            raise ValueError(f"{key.replace('_', ' ').title()} must be from {low} to {high}")
        out["appearance"][key] = number

    given_notes, given_titles = _given(raw, "notes"), _given(raw, "titles")
    for section, _ in SECTIONS:
        if section in given_notes:
            out["notes"][section] = _clean_text(given_notes[section], "", _TEXT_MAX["note"])
        title = _clean_text(given_titles.get(section), "", _TEXT_MAX["title"])
        if title and title != _SECTION_TITLES[section]:
            out["titles"][section] = title
    # Blank means the text in data/wbr_report_glossary.md, so a later change to that file still shows.
    glossary = _clean_text(raw.get("glossary"), "", _TEXT_MAX["glossary"])
    out["glossary"] = "" if glossary == default_glossary() else glossary
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
            "weeks_per_month": numbers["wbr_report_weeks_per_month"],
            "appearance": look,
            "warnings": warnings,
        },
        "sections": [{"id": s["id"], "title": titles[s["id"]], "tables": [], "notes": [], "more": {"tables": []}}
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


# ── Export To Google Slides ──────────────────────────────────────────────────
# The page sends one slide at a time, so the whole deck is not one request that
# runs past the API Gateway time limit, and a slide's images are small enough to send.
_DECK_ID_RE = re.compile(r"[A-Za-z0-9_\-]{10,100}")
_SLIDE_TEXT_MAX = {"title": 200, "subtitle": 300, "note": 1000}
_SLIDE_CELL_MAX = 1000
_SLIDE_ROWS_MAX = 40
_SLIDE_COLUMNS_MAX = 30
_SLIDE_LINES_MAX = 3
_SLIDE_LINE_CHARTS_MAX = 2


def _slide_text(raw, key: str) -> str:
    return str(raw.get(key) or "").strip()[:_SLIDE_TEXT_MAX[key]]


def _clean_slide_table(raw) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("head"), list) or not isinstance(raw.get("rows"), list):
        raise ValueError("Bad table on the slide")
    head = [str(c or "")[:_SLIDE_CELL_MAX] for c in raw["head"]]
    if not 1 <= len(head) <= _SLIDE_COLUMNS_MAX or not 1 <= len(raw["rows"]) <= _SLIDE_ROWS_MAX:
        raise ValueError("A table on the slide has too many rows or columns")
    rows = []
    for row in raw["rows"]:
        cells = row.get("cells") if isinstance(row, dict) else None
        if not isinstance(cells, list) or len(cells) != len(head):
            raise ValueError("Bad table row on the slide")
        rows.append({"cells": [str(c or "")[:_SLIDE_CELL_MAX] for c in cells], "bold": row.get("bold") is True})
    return {"head": head, "rows": rows, "text": raw.get("text") is True}


def clean_export_slide(raw) -> dict:
    """One slide from the page: {title, subtitle, note, and a table {head, rows: [{cells,
    bold}], text} or lines: [[{title, subtitle, image}]]}. Raises ValueError with a message."""
    if not isinstance(raw, dict):
        raise ValueError("Bad slide")
    out = {key: _slide_text(raw, key) for key in _SLIDE_TEXT_MAX}
    if raw.get("table") is not None:
        out["table"] = _clean_slide_table(raw["table"])
        return out
    lines = raw.get("lines")
    if not isinstance(lines, list) or not 1 <= len(lines) <= _SLIDE_LINES_MAX:
        raise ValueError("A slide needs a table or charts")
    out["lines"] = []
    for line in lines:
        if not isinstance(line, list) or not 1 <= len(line) <= _SLIDE_LINE_CHARTS_MAX or not all(isinstance(c, dict) for c in line):
            raise ValueError("Bad line of charts on the slide")
        out["lines"].append([{"title": _slide_text(c, "title"), "subtitle": _slide_text(c, "subtitle"), "image": c.get("image")}
                             for c in line])
    return out


def _report_refused(user: str) -> str | None:
    """Why this user may not use the Report, or None: admin only until wbr_report_on is 1."""
    if is_admin(user):
        return None
    numbers, _ = _read_settings()
    return None if int(numbers["wbr_report_on"]) == 1 else "The Report view is not available yet"


@router.post("/weekly-business-review/report/export/start")
def weekly_business_review_report_export_start(request: Request, payload: dict,
                                               user: str = Depends(require_page_api("weekly_business_review"))):
    """A new deck with its title slide, for the page to add the slides to. {update_date}."""
    from clients.google_slides_client import GoogleSlidesClient, GoogleSlidesError
    try:
        refused = _report_refused(user)
        if refused:
            return {"status": "error", "message": refused}
        review = datetime.date.fromisoformat(clean_update_date((payload or {}).get("update_date")) or last_sunday(_uk_now().date()).isoformat())
        deck_id, url = GoogleSlidesClient().create_report_deck(f"Weekly Business Review {review:%d/%m/%Y}",
                                                               f"Review Date {review:%d/%m/%Y}")
    except (ValueError, GoogleSlidesError) as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        logger.error(f"Weekly Business Review report export failed to start: {e}")
        return {"status": "error", "message": str(e)}
    _track(request, 'wbr_report_export_slides', '/weekly-business-review')
    return {"status": "ok", "deck_id": deck_id, "url": url}


@router.post("/weekly-business-review/report/export/slide")
def weekly_business_review_report_export_slide(request: Request, payload: dict,
                                               user: str = Depends(require_page_api("weekly_business_review"))):
    """Adds one slide to the end of a deck from export/start. {deck_id, index, slide}."""
    import boto3
    from clients.google_slides_client import GoogleSlidesClient, GoogleSlidesError
    from .kpi import s3_bucket_region, wbr_slide_image_url
    payload = payload or {}
    deck_id, index = str(payload.get("deck_id") or ""), payload.get("index")
    if not _DECK_ID_RE.fullmatch(deck_id) or isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 9999:
        return {"status": "error", "message": "Bad deck"}
    try:
        refused = _report_refused(user)
        if refused:
            return {"status": "error", "message": refused}
        slide = clean_export_slide(payload.get("slide"))
        if slide.get("lines"):
            s3 = boto3.client("s3", region_name=s3_bucket_region)
            for l, line in enumerate(slide["lines"]):
                for c, chart in enumerate(line):
                    chart["image_url"] = wbr_slide_image_url(s3, f"report_{deck_id}_{index:04d}_{l}_{c}", chart.pop("image"), chart["title"])
        GoogleSlidesClient().add_report_slide(deck_id, slide)
    except (ValueError, GoogleSlidesError) as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        logger.error(f"Weekly Business Review report slide failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok"}


def clean_report_charts(raw) -> dict:
    """The KPI charts chosen for each section: {section id: [{id, ref, period, title?,
    subtitle?}]}. Raises ValueError with a message."""
    from .kpi import _wbr_titles
    if not isinstance(raw, dict):
        raise ValueError("Report charts are not in the right form")
    out, ids = {}, set()
    for section, items in raw.items():
        if section not in _SECTION_TITLES:
            raise ValueError("Unknown report section")
        if not isinstance(items, list) or len(items) > _CHARTS_PER_SECTION_MAX:
            raise ValueError(f"A section can hold at most {_CHARTS_PER_SECTION_MAX} charts")
        clean, seen = [], set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Bad report chart")
            item_id, ref, period = item.get("id"), item.get("ref"), item.get("period")
            if not all(isinstance(v, str) and 0 < len(v) <= 40 for v in (item_id, ref)) or period not in ("m", "w"):
                raise ValueError("Bad report chart")
            if item_id in ids or (ref, period) in seen:
                raise ValueError("That chart is already in this section")
            ids.add(item_id)
            seen.add((ref, period))
            clean.append({"id": item_id, "ref": ref, "period": period, **_wbr_titles(item)})
        if clean:
            out[section] = clean
    return out


@router.post("/weekly-business-review/report/charts")
def weekly_business_review_report_charts_save(request: Request, payload: dict,
                                              user: str = Depends(require_page_api("weekly_business_review"))):
    """Replace the KPI charts chosen for the Report's sections. Admin only."""
    from .kpi import _WBR_REPORT_CHARTS_KEY, _write_kpi_layout_value
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the report charts"}
    try:
        charts = clean_report_charts((payload or {}).get("charts"))
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    try:
        _write_kpi_layout_value(_WBR_REPORT_CHARTS_KEY, charts)
    except Exception as e:
        logger.error(f"Weekly Business Review report charts save failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok"}


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
    return {"status": "ok", "settings": text, "titles": _SECTION_TITLES, "sources": sources,
            "glossary_default": default_glossary()}


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
