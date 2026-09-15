"""0% finance subsidy rates — benchmark terms in, every month in between out.

Lenders quote the subsidy we pay as a % of balance at a few benchmark terms
only. Agreements run to any whole month, so every term between two benchmarks
is filled in on a straight line and the whole range is offered for pasting into
Apex.

The interpolation lives here rather than in the page's JavaScript so there is a
single definition of what a term is worth.
"""
import datetime
import logging
from decimal import Decimal, ROUND_HALF_UP

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from utils.db_utils import (get_database_connection, get_subsidy_schedules,
                            save_subsidy_schedule, delete_subsidy_schedule)

from .core import (BASE_URL, templates, _track, get_current_user,
                   require_page, require_page_api)

logger = logging.getLogger(__name__)

router = APIRouter()

FINANCE_TYPES = ("HP", "PCP")
_THREE_DP = Decimal("0.001")
_MIN_TERM = 1
_MAX_TERM = 120


def _q3(value: Decimal) -> Decimal:
    return Decimal(value).quantize(_THREE_DP, rounding=ROUND_HALF_UP)


def expand_benchmarks(benchmarks: list) -> list:
    """Fill every whole month between the benchmark terms on a straight line.

    benchmarks is [(term_months, rate_pct), ...]; returns
    [{"term": int, "rate": "10.499", "benchmark": bool}, ...] covering the
    lowest benchmark term through the highest inclusive.

    Terms outside the quoted range are deliberately not extrapolated — a rate
    the lender has not quoted is not ours to invent.
    """
    pairs = sorted(((int(t), Decimal(r)) for t, r in benchmarks), key=lambda p: p[0])
    if not pairs:
        return []
    if len(pairs) == 1:
        return [{"term": pairs[0][0], "rate": str(_q3(pairs[0][1])), "benchmark": True}]

    rows = []
    for (t1, r1), (t2, r2) in zip(pairs, pairs[1:]):
        span = Decimal(t2 - t1)
        step = (r2 - r1) / span
        for term in range(t1, t2):
            rate = r1 + step * Decimal(term - t1)
            rows.append({"term": term, "rate": str(_q3(rate)), "benchmark": term == t1})

    last_term, last_rate = pairs[-1]
    rows.append({"term": last_term, "rate": str(_q3(last_rate)), "benchmark": True})
    return rows


def _with_effective_ranges(schedules: list) -> list:
    """Attach the date range each schedule is in force for.

    A schedule runs until the day before the next one for the same company and
    finance type, so each combination is versioned independently.
    """
    by_combo: dict = {}
    for s in schedules:
        by_combo.setdefault((s["finance_company"], s["finance_type"]), []).append(s)

    for combo_schedules in by_combo.values():
        combo_schedules.sort(key=lambda s: s["effective_from"])
        for i, s in enumerate(combo_schedules):
            nxt = combo_schedules[i + 1] if i + 1 < len(combo_schedules) else None
            s["effective_to"] = (nxt["effective_from"] - datetime.timedelta(days=1)) if nxt else None
    return schedules


def _serialise(schedules: list) -> list:
    out = []
    for s in _with_effective_ranges(schedules):
        rows = expand_benchmarks(s["benchmarks"])
        out.append({
            "id": s["id"],
            "company": s["finance_company"],
            "type": s["finance_type"],
            "effective_from": s["effective_from"].isoformat(),
            "effective_to": s["effective_to"].isoformat() if s["effective_to"] else None,
            "benchmarks": [{"term": int(t), "rate": str(_q3(Decimal(r)))} for t, r in sorted(s["benchmarks"])],
            "rows": rows,
        })
    out.sort(key=lambda s: (s["company"], s["type"], s["effective_from"]))
    return out


def _load_all() -> list:
    with get_database_connection() as conn:
        return _serialise(get_subsidy_schedules(conn))


@router.get("/subsidy-rates", response_class=HTMLResponse)
def subsidy_rates_page(request: Request, user: str = Depends(require_page('subsidy_rates'))):
    try:
        schedules = _load_all()
        load_error = None
    except Exception as e:
        logger.error(f"Subsidy rates load failed: {e}")
        schedules = []
        load_error = str(e)

    _track(request, 'page_view', '/subsidy-rates')
    return templates.TemplateResponse("subsidy_rates.html", {
        "request": request,
        "user": user,
        "BASE_URL": BASE_URL,
        "schedules": schedules,
        "load_error": load_error,
        "today": datetime.date.today().isoformat(),
    })


def _parse_payload(payload: dict) -> tuple:
    """Validate an incoming schedule. Returns (values, error_message)."""
    company = str(payload.get("company", "")).strip()
    if not company:
        return None, "Give the finance company a name."
    if len(company) > 100:
        return None, "That company name is too long (100 characters maximum)."

    ftype = str(payload.get("type", "")).strip().upper()
    if ftype not in FINANCE_TYPES:
        return None, "Finance type must be HP or PCP."

    try:
        effective_from = datetime.date.fromisoformat(str(payload.get("effective_from", "")))
    except ValueError:
        return None, "Set a valid date for when these rates start."

    raw = payload.get("benchmarks") or []
    if not isinstance(raw, list):
        return None, "Benchmark terms are missing."

    seen = set()
    benchmarks = []
    for entry in raw:
        try:
            term = int(entry["term"])
            rate = Decimal(str(entry["rate"]))
        except (KeyError, TypeError, ValueError, ArithmeticError):
            return None, "Every row needs a whole-number term and a rate."
        if not _MIN_TERM <= term <= _MAX_TERM:
            return None, f"Term {term} must be between {_MIN_TERM} and {_MAX_TERM} months."
        if rate < 0:
            return None, f"The rate for {term} months must be zero or more."
        if rate >= Decimal("10000"):
            return None, f"The rate for {term} months is implausibly large."
        if term in seen:
            return None, f"Term {term} is entered twice."
        seen.add(term)
        benchmarks.append((term, _q3(rate)))

    if len(benchmarks) < 2:
        return None, "Enter at least two benchmark terms so the months between can be worked out."

    benchmarks.sort()
    return (company, ftype, effective_from, benchmarks), None


@router.post("/api/subsidy-rates")
async def save_rates(request: Request, user: str = Depends(require_page_api('subsidy_rates'))):
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"status": "error", "message": "body must be valid JSON"}, status_code=400)

    values, error = _parse_payload(payload)
    if error:
        return JSONResponse({"status": "error", "message": error}, status_code=400)

    company, ftype, effective_from, benchmarks = values
    schedule_id = payload.get("id")

    try:
        with get_database_connection() as conn:
            save_subsidy_schedule(conn, schedule_id, company, ftype, effective_from, benchmarks, user)
            schedules = _serialise(get_subsidy_schedules(conn))
        return {"status": "ok", "schedules": schedules}
    except Exception as e:
        message = str(e)
        if "subsidy_rate_schedules_uniq" in message:
            return JSONResponse({
                "status": "error",
                "message": f"{company} {ftype} already has rates starting {effective_from:%d %b %Y}. Edit that schedule instead.",
            }, status_code=409)
        logger.error(f"Subsidy rates save failed: {e}")
        return JSONResponse({"status": "error", "message": message}, status_code=500)


@router.delete("/api/subsidy-rates/{schedule_id}")
def remove_rates(schedule_id: int, user: str = Depends(require_page_api('subsidy_rates'))):
    try:
        with get_database_connection() as conn:
            delete_subsidy_schedule(conn, schedule_id)
            schedules = _serialise(get_subsidy_schedules(conn))
        return {"status": "ok", "schedules": schedules}
    except Exception as e:
        logger.error(f"Subsidy rates delete failed: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
