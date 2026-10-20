"""KPI dashboard: Snowflake daily summary, SharePoint sessions, Cortex AI summary."""

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
import calendar
import json
import re
import time
import logging
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor


from utils.db_utils import (get_database_connection, get_prod_pricer_connection,
                            get_chart_targets, get_chart_target_ranges, save_chart_targets,
                            delete_chart_target, replace_chart_target_dates,
                            get_chart_target_overview,
                            get_chart_target_summary, get_ga4_campaign_ids,
                            add_ga4_campaign, remove_ga4_campaign)
from utils.snowflake_client import SnowflakeClient
from utils import sharepoint_sources
from utils import at_invoices
from utils import kpi_data_sources as kds

from .core import (BASE_URL, templates, _track, get_current_user, is_admin,
                   require_page, require_page_api, _uk_now,
                   s3_bucketname, s3_bucket_region, _S3_ENV)

logger = logging.getLogger(__name__)

router = APIRouter()


GA4_CAMPAIGN_EVENTS = [
    "Apply-Finance_Lead",
    "CSF-Approved_Lead",
    "Pre-Approved_Lead",
    "Vehicle_Enquiry_Lead",
    "Vehicle_Reservation_Lead",
]

# Selecting this instead of a single event sums every event in
# GA4_CAMPAIGN_EVENTS. Sessions are NOT summed with them — see the query below.
GA4_ALL_EVENTS = "All events"

# How many days the page shows in a day chart at once. All days are sent; the
# page scrolls this window through them.
GA4_DAILY_WINDOW_DAYS = 25

GA4_CAMPAIGN_METRICS_TABLE = "PROD_CARSUPERMARKET_DB.COMPUTE_UX_TRACKING.GA4_CAMPAIGN_METRICS"

# The campaigns shown are chosen on the page (Settings) and stored by GA4
# campaign ID in Aurora — see db/14_ga4_campaigns.sql. IDs rather than names,
# because campaigns get renamed; the page shows each one's current name.


# ── Chart targets (Google Ads Campaigns page) ────────────────────────────────
# Two metrics are stored per campaign: sessions and events. The conversion rate,
# the aggregate across campaigns and the All-events total are all DERIVED, never
# stored — see db/12_chart_targets.sql for why.
#
# Targets are written from today to TARGET_HORIZON_YEARS ahead, so one figure
# keeps applying to days that have not arrived yet.

TARGET_HORIZON_YEARS = 2
# How far back a saved target reaches. The charts show up to 13 months of
# history, and GA4 data typically ends a day or two before today — so a target
# written from today forward would land entirely on dates no chart draws, and
# nothing would appear until the data caught up. Covering history as well means
# the figure you enter is simply "the target", against past days and future
# ones alike.
TARGET_HISTORY_YEARS = 3


def _target_key(metric: str, campaign: str, event_name: str = None) -> str:
    """campaign is the GA4 campaign ID. sessions is shared across every event,
    so its key carries no event."""
    if metric == 'sessions':
        return f"ga4:sessions:{campaign}"
    return f"ga4:events:{event_name}:{campaign}"


def _shift_years(d: datetime.date, years: int) -> datetime.date:
    """29 Feb has no anniversary in a common year, so it lands on 28 Feb."""
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(month=2, day=28, year=d.year + years)


def _target_write_range() -> tuple[datetime.date, datetime.date]:
    """The full span a saved target covers: history back, horizon forward.

    Deliberately wider than the charts on both sides, so a target is drawn
    everywhere the page can look without needing to know the data's own dates.
    """
    today = _uk_now().date()
    return _shift_years(today, -TARGET_HISTORY_YEARS), _shift_years(today, TARGET_HORIZON_YEARS)


def _full_week_length(_monday: datetime.date) -> int:
    return 7


def _full_month_length(first_of_month: datetime.date) -> int:
    return calendar.monthrange(first_of_month.year, first_of_month.month)[1]


def _roll_up_targets(by_date: dict, daily_dates: list, weekly_groups: list, monthly_groups: list) -> dict:
    """Roll one key's per-date targets up to the chart's own periods.

    A period's target is the average daily target across the dates that have
    one, times the period's FULL length — not the sum of the dates present.

    That matters for the current week and month, which are only part elapsed,
    and for the last period of the data where GA4 has not caught up. Summing
    only the dates present would show three days' worth of target on a week
    three days in, so the target line would dive at the right-hand end and read
    as the target collapsing rather than the period being unfinished.

    Scaling by the average also leaves the conversion view correct: the rate is
    summed events over summed sessions, and multiplying both sides by the same
    period length cancels out.

    `covered` carries how many dates in each period actually had a target, so
    the page can still tell a fully-targeted period from a barely-targeted one
    and refuse to build a rate from mismatched halves.

    A period with no targeted date at all is None, never 0: a zero would draw a
    target along the bottom and paint the chart red.
    """
    def _period(dates, full_length):
        values = [by_date[d] for d in dates if d in by_date]
        if not values:
            return None, 0
        # Round to the same 4dp the column stores, so a scaled-up average does
        # not carry floating-point noise into the chart.
        return round(sum(values) / len(values) * full_length, 4), len(values)

    daily, daily_covered = zip(*[_period([d], 1) for d in daily_dates]) if daily_dates else ((), ())
    weekly, weekly_covered = (
        zip(*[_period(g, _full_week_length(g[0])) for g in weekly_groups if g])
        if any(weekly_groups) else ((), ())
    )
    monthly, monthly_covered = (
        zip(*[_period(g, _full_month_length(g[0])) for g in monthly_groups if g])
        if any(monthly_groups) else ((), ())
    )
    return {
        "daily": list(daily),
        "weekly": list(weekly),
        "monthly": list(monthly),
        "daily_covered": list(daily_covered),
        "weekly_covered": list(weekly_covered),
        "monthly_covered": list(monthly_covered),
    }


def _ga4_date_groups(data_start: datetime.date, data_end: datetime.date) -> tuple[list, list, list]:
    """The day, week and month groups the GA4 charts are drawn against.

    Shared by the chart endpoint and the targets-only endpoint so both roll up
    against exactly the same periods — a target computed against different
    groups would not line up with the columns it is drawn over.
    """
    daily_dates = []
    date_iter = data_start
    while date_iter <= data_end:
        daily_dates.append(date_iter)
        date_iter += datetime.timedelta(days=1)

    current_week_monday = _week_monday_of(data_end)
    week_mondays = [current_week_monday - datetime.timedelta(weeks=i) for i in range(13, -1, -1)]
    week_mondays = [monday for monday in week_mondays
                    if monday + datetime.timedelta(days=6) >= data_start]
    weekly_date_groups = [
        [monday + datetime.timedelta(days=i) for i in range(7)
         if data_start <= monday + datetime.timedelta(days=i) <= data_end]
        for monday in week_mondays
    ]

    monthly_start = max(data_start.replace(day=1), _shift_month_start(data_end.replace(day=1), -13))
    month_firsts = []
    month_iter = monthly_start
    while month_iter <= data_end.replace(day=1):
        month_firsts.append(month_iter)
        month_iter = _shift_month_start(month_iter, 1)

    monthly_date_groups = []
    for month_first in month_firsts:
        next_month = _shift_month_start(month_first, 1)
        month_dates = []
        date_iter = max(month_first, data_start)
        while date_iter < next_month and date_iter <= data_end:
            month_dates.append(date_iter)
            date_iter += datetime.timedelta(days=1)
        monthly_date_groups.append(month_dates)

    return daily_dates, weekly_date_groups, monthly_date_groups


def _shift_month_start(d: datetime.date, months: int) -> datetime.date:
    month_index = d.month - 1 + months
    return datetime.date(d.year + month_index // 12, month_index % 12 + 1, 1)


def _campaign_targets(conn, event_name: str, campaign_ids: list, daily_dates: list,
                      weekly_groups: list, monthly_groups: list) -> dict:
    """Per-campaign session and event targets, rolled up to the chart periods,
    keyed by campaign ID.

    Only the two STORED metrics are returned. The aggregate, the conversion rate
    and the All-events total are derived in the page from whichever campaigns
    are on screen.
    """
    target_keys = {}
    for campaign in campaign_ids:
        target_keys[(campaign, 'sessions')] = _target_key('sessions', campaign)
        if event_name != GA4_ALL_EVENTS:
            target_keys[(campaign, 'events')] = _target_key('events', campaign, event_name)
        else:
            for component in GA4_CAMPAIGN_EVENTS:
                target_keys[(campaign, f'events:{component}')] = _target_key('events', campaign, component)

    data_start, data_end = daily_dates[0], daily_dates[-1]
    stored = get_chart_targets(conn, list(target_keys.values()), data_start, data_end)

    out = {}
    for campaign in campaign_ids:
        sessions_by_date = stored.get(target_keys[(campaign, 'sessions')], {})
        if event_name != GA4_ALL_EVENTS:
            events_by_date = stored.get(target_keys[(campaign, 'events')], {})
        else:
            # Sum the component events per date, but ONLY for dates where every
            # component has a target. Adding up whichever events happen to be set
            # would present two events out of five as a complete All-events
            # target, which reads as a real figure to compare against while
            # actually being a fraction of one.
            #
            # Sessions are NOT summed — one value per campaign per date, matching
            # the actual's MAX(SESSIONS).
            component_maps = [
                stored.get(target_keys[(campaign, f'events:{component}')], {})
                for component in GA4_CAMPAIGN_EVENTS
            ]
            events_by_date = {}
            if all(component_maps):
                complete_dates = set(component_maps[0])
                for component_map in component_maps[1:]:
                    complete_dates &= set(component_map)
                for d in complete_dates:
                    events_by_date[d] = sum(component_map[d] for component_map in component_maps)
        out[campaign] = {
            "sessions": _roll_up_targets(sessions_by_date, daily_dates, weekly_groups, monthly_groups),
            "events": _roll_up_targets(events_by_date, daily_dates, weekly_groups, monthly_groups),
        }
    return out


@router.get("/kpi/ga4-targets")
def kpi_ga4_targets(
    event_name: str,
    data_start: str,
    data_end: str,
    user: str = Depends(require_page_api('google_ads_campaigns')),
):
    """Targets alone, rolled up against the loaded charts' own date range.

    Saving a target changes nothing in GA4, so the page uses this instead of
    reloading the chart data — that was re-querying Snowflake on every save
    just to redraw a red line.
    """
    if event_name != GA4_ALL_EVENTS and event_name not in GA4_CAMPAIGN_EVENTS:
        return {"status": "error", "message": f"Unknown event: {event_name}"}
    try:
        start = datetime.date.fromisoformat(data_start)
        end = datetime.date.fromisoformat(data_end)
    except ValueError:
        return {"status": "error", "message": "Dates must be YYYY-MM-DD"}
    if start > end:
        return {"status": "error", "message": "data_start is after data_end"}
    # The page only ever sends back the range the chart endpoint gave it; this
    # caps a hand-crafted request rather than letting it build years of groups.
    if (end - start).days > 800:
        return {"status": "error", "message": "Date range is too wide"}

    daily_dates, weekly_groups, monthly_groups = _ga4_date_groups(start, end)
    try:
        with get_database_connection() as conn:
            campaign_ids = get_ga4_campaign_ids(conn)
            targets = _campaign_targets(conn, event_name, campaign_ids,
                                        daily_dates, weekly_groups, monthly_groups)
    except Exception as e:
        logger.error("Reading GA4 targets failed: %s", e)
        return {"status": "error", "message": str(e)}

    return {"status": "ok", "event_name": event_name,
            "campaigns": [{"id": c, "targets": t} for c, t in targets.items()]}


def _validate_target_request(campaign: str, metric: str, event_name: str,
                             campaign_ids: list) -> str | None:
    """Returns an error message, or None when the request is valid.

    campaign is a GA4 campaign ID and must be one of the campaigns added in
    Settings."""
    if campaign not in campaign_ids:
        return f"Unknown campaign: {campaign}"
    if metric not in ('sessions', 'events'):
        return f"Unknown metric: {metric}"
    if metric == 'events':
        # GA4_ALL_EVENTS is a valid selection on the page but is NOT a stored
        # key — its target is the component events summed. The popup edits those
        # components, so a write naming it is a bug in the caller.
        if event_name == GA4_ALL_EVENTS:
            return "All events has no target of its own — set a target on each event"
        if event_name not in GA4_CAMPAIGN_EVENTS:
            return f"Unknown event: {event_name}"
    return None


def _target_value_or_error(raw) -> tuple[float | None, str | None]:
    """Zero is a valid target (0% is a real aim) and stays distinct from missing."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, "Target must be a number"
    if value != value or value in (float('inf'), float('-inf')):
        return None, "Target must be a number"
    if value < 0:
        return None, "Target cannot be negative"
    # NUMERIC(14,4): ten digits before the point.
    if value >= 10 ** 10:
        return None, "Target is too large"
    return value, None


@router.post("/kpi/chart-target")
async def kpi_save_chart_target(
    request: Request,
    user: str = Depends(require_page_api('google_ads_campaigns')),
):
    """Save one or more targets for a campaign in a single transaction.

    The conversion popup edits sessions AND events together: saving them
    separately could commit one and fail the other, leaving a derived conversion
    rate nobody entered.

    Body: {"campaign": str, "event_name": str|null,
           "targets": [{"metric": "sessions"|"events", "value": number}, ...]}
    """
    body = await request.json()
    campaign = body.get('campaign')
    event_name = body.get('event_name')
    entries = body.get('targets') or []
    if not entries:
        return {"status": "error", "message": "No targets given"}

    start, end = _target_write_range()
    try:
        with get_database_connection() as conn:
            campaign_ids = get_ga4_campaign_ids(conn)
            writes = []
            for entry in entries:
                metric = entry.get('metric')
                error = _validate_target_request(campaign, metric, event_name, campaign_ids)
                if error:
                    return {"status": "error", "message": error}
                value, error = _target_value_or_error(entry.get('value'))
                if error:
                    return {"status": "error", "message": error}
                writes.append((_target_key(metric, campaign, event_name), value))
            save_chart_targets(conn, writes, start, end, user)
    except Exception as e:
        logger.error("Saving chart target failed for %s: %s", campaign, e)
        return {"status": "error", "message": f"Save failed: {e}"}

    return {
        "status": "ok",
        "effective_start": start.isoformat(),
        "effective_end": end.isoformat(),
        "saved": [{"metric": entry.get('metric'), "value": value}
                  for entry, (_, value) in zip(entries, writes)],
    }


@router.delete("/kpi/chart-target")
async def kpi_delete_chart_target(
    request: Request,
    user: str = Depends(require_page_api('google_ads_campaigns')),
):
    """Remove a target across the same span a save writes.

    Mirrors the save range: a save covers history as well as the future, so a
    remove that only cleared today onward would leave the old dates behind and
    the line would still be drawn over the past.

    Only the named metric is removed — clearing the shared sessions target from
    one event's popup would change every other event's conversion target.
    """
    body = await request.json()
    campaign = body.get('campaign')
    metric = body.get('metric')
    event_name = body.get('event_name')

    start, _ = _target_write_range()
    try:
        with get_database_connection() as conn:
            error = _validate_target_request(campaign, metric, event_name, get_ga4_campaign_ids(conn))
            if error:
                return {"status": "error", "message": error}
            # from_date is the start of the same span a save writes, so the
            # whole target goes rather than just its future half.
            removed = delete_chart_target(conn, _target_key(metric, campaign, event_name), start)
    except Exception as e:
        # Never report a database failure as a successful removal.
        logger.error("Removing chart target failed for %s: %s", campaign, e)
        return {"status": "error", "message": f"Remove failed: {e}"}

    return {"status": "ok", "removed": removed, "effective_start": start.isoformat()}


@router.get("/kpi/chart-target")
def kpi_get_chart_target(
    campaign: str,
    event_name: str = None,
    user: str = Depends(require_page_api('google_ads_campaigns')),
):
    """Current stored targets for one campaign, for the popup to prefill.

    Read separately from the chart data on purpose: a target saved today-forward
    is invisible to a read bounded by the charts' last data date, which is often
    yesterday. Reading it here means Add/Edit state and the prefilled value are
    right even when nothing is drawn yet.
    """
    keys = {'sessions': _target_key('sessions', campaign)}
    if event_name and event_name != GA4_ALL_EVENTS and event_name in GA4_CAMPAIGN_EVENTS:
        keys['events'] = _target_key('events', campaign, event_name)

    try:
        with get_database_connection() as conn:
            if campaign not in get_ga4_campaign_ids(conn):
                return {"status": "error", "message": f"Unknown campaign: {campaign}"}
            stored = get_chart_target_ranges(conn, list(keys.values()))
    except Exception as e:
        logger.error("Reading chart target failed for %s: %s", campaign, e)
        return {"status": "error", "message": str(e)}

    out = {}
    for metric, key in keys.items():
        row = stored.get(key)
        out[metric] = None if not row else {
            "value": row['value'],
            "start": row['start'].isoformat(),
            "end": row['end'].isoformat(),
        }
    return {"status": "ok", "campaign": campaign, "event_name": event_name, "targets": out}


def _week_monday_of(d: datetime.date) -> datetime.date:
    return d - datetime.timedelta(days=d.weekday())


def _prev_iso_week_monday(monday: datetime.date) -> datetime.date:
    """Monday of the same ISO week NUMBER in the previous ISO year.
    This is the N-1 week definition for all weekly charts — equivalent week
    number, not the same 7 dates shifted by 52 weeks."""
    iso_year, iso_week, _ = monday.isocalendar()
    try:
        return datetime.date.fromisocalendar(iso_year - 1, iso_week, 1)
    except ValueError:
        # Week 53 with no equivalent in the previous ISO year — use its last week
        return datetime.date.fromisocalendar(iso_year - 1, 52, 1)


@router.get("/kpi", response_class=HTMLResponse)
def kpi_page(request: Request, user: str = Depends(require_page('kpi'))):
    _track(request, 'page_view', '/kpi')
    return templates.TemplateResponse("kpi.html", {"request": request, "user": user, "BASE_URL": BASE_URL,
                                                   "kpi_is_admin": is_admin(user)})


@router.get("/google-ads-campaigns", response_class=HTMLResponse)
def google_ads_campaigns_page(request: Request, user: str = Depends(require_page('google_ads_campaigns'))):
    _track(request, 'page_view', '/google-ads-campaigns')
    return templates.TemplateResponse("google_ads_campaigns.html", {"request": request, "user": user, "BASE_URL": BASE_URL})


# ── Google Ads Campaigns: Settings (which campaigns the page shows) ──────────

@router.get("/kpi/ga4-campaigns")
def kpi_ga4_campaigns_list(user: str = Depends(require_page_api('google_ads_campaigns'))):
    """Every campaign in the GA4 data, for the Settings popup to pick from.

    One row per campaign ID with its current name (the name on its latest
    date), the last date it had sessions, and whether it is active. Active
    means it has sessions on the latest date loaded in the table — GA4 runs a
    day or two behind, so comparing with yesterday would show everything as
    inactive until the data caught up.

    Rows are only written for days a campaign had sessions, and each such day
    repeats once per event, so one event is enough to read every campaign-day.
    """
    sql = f"""
        WITH latest AS (
            SELECT MAX(DT) AS max_dt
            FROM {GA4_CAMPAIGN_METRICS_TABLE}
        )
        SELECT
            CAMPAIGN_ID
            , MAX_BY(CAMPAIGN_NAME, DT) AS CAMPAIGN_NAME
            , MAX(DT) AS LAST_DT
            , MAX(DT) = (SELECT max_dt FROM latest) AS IS_ACTIVE
        FROM {GA4_CAMPAIGN_METRICS_TABLE}
        WHERE EVENT_NAME = %s
        GROUP BY CAMPAIGN_ID
        ORDER BY IS_ACTIVE DESC, CAMPAIGN_NAME
    """
    try:
        with get_database_connection() as conn:
            chosen = get_ga4_campaign_ids(conn)
        with SnowflakeClient() as sf:
            rows, cols = sf.execute(sql, [GA4_CAMPAIGN_EVENTS[0]])
    except Exception as e:
        logger.error("Listing GA4 campaigns failed: %s", e)
        return {"status": "error", "message": str(e)}

    col_map = {c.upper(): i for i, c in enumerate(cols)}
    chosen_set = set(chosen)
    campaigns = []
    for row in rows:
        last_dt = row[col_map["LAST_DT"]]
        campaigns.append({
            "id": str(row[col_map["CAMPAIGN_ID"]]),
            "name": str(row[col_map["CAMPAIGN_NAME"]] or ''),
            "last_date": last_dt.isoformat() if hasattr(last_dt, 'isoformat') else str(last_dt),
            "active": bool(row[col_map["IS_ACTIVE"]]),
            "added": str(row[col_map["CAMPAIGN_ID"]]) in chosen_set,
        })
    # The chosen list keeps the order the campaigns were added in, which is
    # the order the charts page shows them.
    return {"status": "ok", "chosen": chosen, "campaigns": campaigns}


def _campaign_id_or_error(raw) -> tuple[str | None, str | None]:
    campaign_id = str(raw or '').strip()
    if not campaign_id:
        return None, "No campaign given"
    if campaign_id == '(not set)':
        return None, "Campaigns with no ID cannot be added"
    if len(campaign_id) > 50:
        return None, "Campaign ID is too long"
    return campaign_id, None


@router.post("/kpi/ga4-campaigns")
async def kpi_ga4_campaigns_add(
    request: Request,
    user: str = Depends(require_page_api('google_ads_campaigns')),
):
    body = await request.json()
    campaign_id, error = _campaign_id_or_error(body.get('campaign_id'))
    if error:
        return {"status": "error", "message": error}
    try:
        with get_database_connection() as conn:
            add_ga4_campaign(conn, campaign_id, user)
    except Exception as e:
        logger.error("Adding GA4 campaign %s failed: %s", campaign_id, e)
        return {"status": "error", "message": f"Add failed: {e}"}
    return {"status": "ok", "campaign_id": campaign_id}


@router.delete("/kpi/ga4-campaigns")
async def kpi_ga4_campaigns_remove(
    request: Request,
    user: str = Depends(require_page_api('google_ads_campaigns')),
):
    """Takes the campaign off the page. Its targets are left in chart_targets,
    so they come back if it is added again."""
    body = await request.json()
    campaign_id, error = _campaign_id_or_error(body.get('campaign_id'))
    if error:
        return {"status": "error", "message": error}
    try:
        with get_database_connection() as conn:
            removed = remove_ga4_campaign(conn, campaign_id)
    except Exception as e:
        logger.error("Removing GA4 campaign %s failed: %s", campaign_id, e)
        return {"status": "error", "message": f"Remove failed: {e}"}
    return {"status": "ok", "removed": removed}


@router.get("/kpi/ga4-campaign-conversion")
def kpi_ga4_campaign_conversion(
    request: Request,
    event_name: str = GA4_CAMPAIGN_EVENTS[0],
    # Called by the Google Ads Campaigns page, not the KPI dashboard — mapped
    # to its actual caller so granting only that page leaves a working page.
    user: str = Depends(require_page_api('google_ads_campaigns')),
):
    """Conversion rate and raw event volume for one GA4 event, by campaign.

    All campaigns are returned for the selected event, plus an "Aggregated Total"
    series summing across every campaign before the rate is taken. Day, week
    and month rates are ratios of summed event counts to each campaign's own
    summed sessions, not to site-wide sessions; weekly and monthly values are
    never averages of the daily percentages. Volumes are the same summed event
    counts before dividing by sessions.

    Reads COMPUTE_UX_TRACKING.GA4_CAMPAIGN_METRICS, which already holds one row
    per date, campaign and event with that campaign's sessions on the same row,
    zero-filled for days with sessions but no events. Sessions repeat across the
    event rows for a date and campaign, so the EVENT_NAME filter below is what
    stops the session totals multiplying by the number of events.

    With GA4_ALL_EVENTS the event counts are summed over every event while the
    sessions for that date and campaign are counted ONCE (MAX, not SUM, since
    the value repeats on each event's row). The resulting rate is lead events
    per session, which can exceed 100% because one session can fire several
    events — it is not a share of converting sessions.
    """
    if event_name != GA4_ALL_EVENTS and event_name not in GA4_CAMPAIGN_EVENTS:
        return {"status": "error", "message": f"Unknown event: {event_name}"}

    # The chosen campaigns come first: without them there is nothing to query.
    # Unlike the targets below, this does not fail soft — a page drawn with no
    # campaigns because Aurora was unreachable would look like none were added.
    try:
        with get_database_connection() as conn:
            campaign_ids = get_ga4_campaign_ids(conn)
    except Exception as e:
        logger.error("Reading chosen GA4 campaigns failed: %s", e)
        return {"status": "error", "message": f"Could not read the campaign list: {e}"}
    if not campaign_ids:
        return {"status": "empty", "message": "No campaigns added yet. Use Settings to add some."}

    try:
        today = datetime.date.today()
        first_of_this_month = today.replace(day=1)

        def _shift_month(d: datetime.date, months: int) -> datetime.date:
            month_index = d.month - 1 + months
            return datetime.date(d.year + month_index // 12, month_index % 12 + 1, 1)

        fetch_start = _shift_month(first_of_this_month, -13)
        campaign_placeholders = ", ".join(["%s"] * len(campaign_ids))
        event_placeholders = ", ".join(["%s"] * len(GA4_CAMPAIGN_EVENTS))

        # Every event in ONE query, with EVENT_NAME kept as a column. Switching
        # events on the page used to re-run this per event, which is a fresh
        # Snowflake connection each time for data that is the same underneath —
        # the rows for all five events come back together for barely more cost
        # than one, and sessions are identical across them anyway.
        #
        # Filtered on CAMPAIGN_ID so a renamed campaign stays one series.
        # CAMPAIGN_NAME is the name on each date; the one on the latest date is
        # what the page shows.
        sql = f"""
            SELECT DT, CAMPAIGN_ID, CAMPAIGN_NAME, EVENT_NAME, SESSIONS, EVENT_COUNT
            FROM {GA4_CAMPAIGN_METRICS_TABLE}
            WHERE EVENT_NAME IN ({event_placeholders})
              AND CAMPAIGN_ID IN ({campaign_placeholders})
              AND DT >= %s AND DT <= %s
            ORDER BY DT, CAMPAIGN_ID
        """
        params = [*GA4_CAMPAIGN_EVENTS, *campaign_ids, str(fetch_start), str(today)]

        with SnowflakeClient() as sf:
            metric_rows, metric_cols = sf.execute(sql, params)

        col_map = {c.upper(): i for i, c in enumerate(metric_cols)}

        def _date_value(value) -> datetime.date:
            if isinstance(value, datetime.datetime):
                return value.date()
            if isinstance(value, datetime.date):
                return value
            return datetime.date.fromisoformat(str(value)[:10])

        def _number(value) -> float:
            try:
                return float(value) if value is not None else 0.0
            except (TypeError, ValueError):
                return 0.0

        # Sessions belong to the campaign and date, not to an event — the same
        # value repeats on every event's row, so it is stored once rather than
        # per event. Events are kept per event name.
        sessions_by_campaign = {campaign: {} for campaign in campaign_ids}
        events_by_event = {
            event: {campaign: {} for campaign in campaign_ids}
            for event in GA4_CAMPAIGN_EVENTS
        }
        # Current name per ID: the name on the latest date seen. A campaign with
        # no rows in the window falls back to showing its ID.
        current_names: dict[str, tuple[datetime.date, str]] = {}
        all_dates: set[datetime.date] = set()
        for row in metric_rows:
            campaign = str(row[col_map["CAMPAIGN_ID"]])
            if campaign not in sessions_by_campaign:
                continue
            row_event = str(row[col_map["EVENT_NAME"]])
            if row_event not in events_by_event:
                continue
            date_value = _date_value(row[col_map["DT"]])
            name = row[col_map["CAMPAIGN_NAME"]]
            if name and (campaign not in current_names or date_value >= current_names[campaign][0]):
                current_names[campaign] = (date_value, str(name))
            sessions_by_campaign[campaign][date_value] = _number(row[col_map["SESSIONS"]])
            events_by_event[row_event][campaign][date_value] = _number(row[col_map["EVENT_COUNT"]])
            all_dates.add(date_value)

        # All events: the counts summed across every event, per campaign and
        # date. Sessions are NOT summed with them — they are counted once, which
        # is why the rate here is events per session and can exceed 100%.
        all_events_by_campaign = {campaign: {} for campaign in campaign_ids}
        for per_campaign in events_by_event.values():
            for campaign, by_date in per_campaign.items():
                target = all_events_by_campaign[campaign]
                for date_value, value in by_date.items():
                    target[date_value] = target.get(date_value, 0.0) + value

        if not all_dates:
            return {"status": "error", "message": "No GA4 session data returned"}

        data_start = min(all_dates)
        data_end = max(all_dates)

        # Every day is sent, not just the most recent window, so the page can
        # scroll the day charts back through the history without another fetch.
        # The page shows DAILY_WINDOW_DAYS of them at a time.
        daily_dates = []
        date_iter = data_start
        while date_iter <= data_end:
            daily_dates.append(date_iter)
            date_iter += datetime.timedelta(days=1)

        current_week_monday = _week_monday_of(data_end)
        # 14 weeks to match the 14 months below: both are the base the page
        # averages to set its y-scale, and a longer base is steadier. Weeks with
        # no data yet are dropped by the filter on the next line.
        week_mondays = [current_week_monday - datetime.timedelta(weeks=i) for i in range(13, -1, -1)]
        week_mondays = [monday for monday in week_mondays if monday + datetime.timedelta(days=6) >= data_start]
        weekly_date_groups = [
            [monday + datetime.timedelta(days=i) for i in range(7) if monday + datetime.timedelta(days=i) <= data_end]
            for monday in week_mondays
        ]

        monthly_start = max(data_start.replace(day=1), _shift_month(data_end.replace(day=1), -13))
        month_firsts = []
        month_iter = monthly_start
        while month_iter <= data_end.replace(day=1):
            month_firsts.append(month_iter)
            month_iter = _shift_month(month_iter, 1)

        monthly_date_groups = []
        for month_first in month_firsts:
            next_month = _shift_month(month_first, 1)
            month_dates = []
            date_iter = month_first
            while date_iter < next_month and date_iter <= data_end:
                month_dates.append(date_iter)
                date_iter += datetime.timedelta(days=1)
            monthly_date_groups.append(month_dates)

        month_abbrs = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

        # Aggregated sums events and sessions across every campaign before
        # dividing, so it is a true pooled rate, not an average of the rows.
        def _sessions(campaigns: list[str], date_value: datetime.date) -> float:
            return sum(sessions_by_campaign[c].get(date_value, 0.0) for c in campaigns)

        def _has_sessions(campaigns: list[str], date_value: datetime.date) -> bool:
            return any(date_value in sessions_by_campaign[c] for c in campaigns)

        def _event_count(events_map: dict, campaigns: list[str], date_value: datetime.date) -> float:
            return sum(events_map[c].get(date_value, 0.0) for c in campaigns)

        def _rate(events_map: dict, campaigns: list[str], dates: list[datetime.date]) -> float | None:
            valid_dates = [d for d in dates if _has_sessions(campaigns, d)]
            if not valid_dates:
                return None
            sessions = sum(_sessions(campaigns, d) for d in valid_dates)
            if sessions <= 0:
                return None
            total_events = sum(_event_count(events_map, campaigns, d) for d in valid_dates)
            return round(total_events / sessions * 100, 3)

        def _volume(events_map: dict, campaigns: list[str], dates: list[datetime.date]) -> float | None:
            valid_dates = [d for d in dates if _has_sessions(campaigns, d)]
            if not valid_dates:
                return None
            return sum(_event_count(events_map, campaigns, d) for d in valid_dates)

        def _session_total(campaigns: list[str], dates: list[datetime.date]) -> float | None:
            valid_dates = [d for d in dates if _has_sessions(campaigns, d)]
            if not valid_dates:
                return None
            return sum(_sessions(campaigns, d) for d in valid_dates)

        def _series(events_map: dict, campaigns: list[str]) -> dict:
            return {
                "daily_rates": [_rate(events_map, campaigns, [d]) for d in daily_dates],
                "weekly_rates": [_rate(events_map, campaigns, dates) for dates in weekly_date_groups],
                "monthly_rates": [_rate(events_map, campaigns, dates) for dates in monthly_date_groups],
                "daily_volumes": [_volume(events_map, campaigns, [d]) for d in daily_dates],
                "weekly_volumes": [_volume(events_map, campaigns, dates) for dates in weekly_date_groups],
                "monthly_volumes": [_volume(events_map, campaigns, dates) for dates in monthly_date_groups],
                # Sessions are sent per campaign so the page can re-pool the
                # aggregate when campaigns are filtered out — a pooled rate
                # cannot be rebuilt from the row percentages alone.
                "daily_sessions": [_session_total(campaigns, [d]) for d in daily_dates],
                "weekly_sessions": [_session_total(campaigns, dates) for dates in weekly_date_groups],
                "monthly_sessions": [_session_total(campaigns, dates) for dates in monthly_date_groups],
            }

        # ── Targets ───────────────────────────────────────────────────────────
        # Only the two STORED metrics are sent, per campaign. The aggregate, the
        # conversion rate and the All-events total are derived in the page, from
        # whichever campaigns are on screen — sending a pre-summed aggregate here
        # would go stale the moment a campaign is unticked.
        #
        # Reading fails soft: a target store that is unreachable (or a table that
        # does not exist yet) must not take the whole chart page down with it.
        # Shared with /kpi/ga4-targets so both roll up identically. Reading fails
        # soft: an unreachable target store (or a table that does not exist yet)
        # must not take the whole chart page down with it.
        # One read covers every event, since the same session keys are needed for
        # all of them and All events reads each component anyway.
        targets_by_event = {}
        try:
            with get_database_connection() as conn:
                for event in (GA4_ALL_EVENTS, *GA4_CAMPAIGN_EVENTS):
                    targets_by_event[event] = _campaign_targets(
                        conn, event, campaign_ids, daily_dates, weekly_date_groups, monthly_date_groups)
        except Exception as e:
            logger.warning("Chart targets unavailable, charts will show none: %s", e)
            targets_by_event = {}

        def _event_payload(events_map: dict, event: str) -> dict:
            stored = targets_by_event.get(event, {})
            return {
                "aggregated": {"name": "Aggregated Total", **_series(events_map, campaign_ids)},
                "campaigns": [{"id": campaign,
                               "name": current_names[campaign][1] if campaign in current_names else campaign,
                               **_series(events_map, [campaign]),
                               "targets": stored.get(campaign)}
                              for campaign in campaign_ids],
            }

        # Every event is built and sent together, so switching events on the page
        # is a local lookup rather than another Snowflake round trip.
        by_event = {GA4_ALL_EVENTS: _event_payload(all_events_by_campaign, GA4_ALL_EVENTS)}
        for event in GA4_CAMPAIGN_EVENTS:
            by_event[event] = _event_payload(events_by_event[event], event)

        selected = by_event[event_name]
        aggregated = selected["aggregated"]
        campaigns = selected["campaigns"]

        return {
            "status": "ok",
            "event_name": event_name,
            "events": [GA4_ALL_EVENTS, *GA4_CAMPAIGN_EVENTS],
            "all_events_label": GA4_ALL_EVENTS,
            "data_start": data_start.isoformat(),
            "data_end": data_end.isoformat(),
            "dates": [date_value.isoformat() for date_value in daily_dates],
            "daily_window_days": GA4_DAILY_WINDOW_DAYS,
            "weekly_labels": [f"W{monday.isocalendar()[1]} ({monday.day}-{month_abbrs[monday.month - 1]})" for monday in week_mondays],
            "monthly_labels": [month_abbrs[month_first.month - 1] + " " + str(month_first.year)[2:] for month_first in month_firsts],
            "aggregated": aggregated,
            "campaigns": campaigns,
            # Every event's series, so switching the Event filter is a local
            # lookup rather than another Snowflake query. The selected event is
            # also returned above under aggregated/campaigns, unchanged, so an
            # older page that does not know about this keeps working.
            "by_event": by_event,
        }
    except Exception as e:
        logger.error("GA4 campaign conversion data failed for %s: %s", event_name, e)
        return {"status": "error", "message": str(e)}


# ── KPI charts: metric catalogue ─────────────────────────────────────────────
# Every chart on the KPI page is created on the page itself. A chart reads one
# metric from one of two sources:
#
#   snowflake  - a number column of product_slt_kpis.daily_summary
#   sharepoint - a number column of any KPI Daily Metrics file in
#                Settings > SharePoint (see _get_sharepoint_data)
#
# The column lists are read live, so new columns appear without code changes.
# No daily_summary column is named in this file except the exclusions below;
# the weekly/monthly rollup default is guessed from the column name.

#   autotrader - allowance and cost figures worked out from Settings > Autotrader
#                (see _AT_METRICS)
#   ds_...     - a Snowflake table added in Settings > Data Sources, by its id
#                (see _data_sources and utils/kpi_data_sources.py)
KPI_SOURCES = ("snowflake", "sharepoint", "autotrader")

# daily_summary columns that are not metrics: the date itself, its text
# labels, and the N-1 / target companions of each metric.
_DS_NON_METRIC_COLUMNS = {"DATE", "MONTH", "WEEK"}
_SNOWFLAKE_NUMBER_TYPES = {"NUMBER", "FLOAT", "DECIMAL", "NUMERIC", "INT", "INTEGER", "BIGINT", "DOUBLE", "REAL"}

# Live column discovery: target columns are detected by naming convention
# (<METRIC>_TARGET) against the actual table,
# so columns added to daily_summary later are picked up without code changes.
_ds_columns_cache: list | None = None      # [(column, data_type)] in table order
_ds_columns_cache_at: float = 0.0
_DS_COLUMNS_TTL = 600  # seconds
# Load Data sends a new token on each click. The first request carrying it
# re-reads the column list, so a daily_summary rebuilt with different columns
# is picked up without a restart. Later requests with the same token reuse it.
_ds_columns_refresh_token: str = ""

# The page sends /kpi/metrics, /kpi/custom-data-batch and others at the same
# moment. Without this lock each one finds a cache empty and runs the same
# Snowflake lookup; with it the first one reads and the rest reuse the result.
_meta_lock = threading.RLock()


def _refresh_daily_summary_columns(token: str) -> None:
    global _ds_columns_cache, _ds_columns_refresh_token
    if token and token != _ds_columns_refresh_token:
        _ds_columns_refresh_token = token
        _ds_columns_cache = None
        # Settings > Data Sources tables too: their columns and names.
        _source_columns_cache.clear()
        _source_names_cache.clear()
        # And the copy shared with other containers.
        _clear_shared_columns()


def _get_daily_summary_column_types() -> list:
    global _ds_columns_cache, _ds_columns_cache_at
    if _ds_columns_cache is not None and time.time() - _ds_columns_cache_at < _DS_COLUMNS_TTL:
        return _ds_columns_cache
    with _meta_lock:
        # Another request may have read it while this one waited.
        if _ds_columns_cache is not None and time.time() - _ds_columns_cache_at < _DS_COLUMNS_TTL:
            return _ds_columns_cache
        try:
            # Same lookup as any other table, so it shares the Aurora copy.
            _ds_columns_cache = _vehicle_table_columns("PRODUCT_SLT_KPIS.DAILY_SUMMARY")
            _ds_columns_cache_at = time.time()
        except Exception as e:
            logger.warning("daily_summary column discovery failed: %s", e)
            if _ds_columns_cache is None:
                # No list yet: return none rather than a guessed one. The read
                # time stays 0, so the next request tries again.
                return []
        return _ds_columns_cache


def _get_daily_summary_columns() -> set:
    return {col for col, _ in _get_daily_summary_column_types()}


def _snowflake_metric_columns() -> list:
    """Number columns of daily_summary that are metrics in their own right, in table order."""
    return [
        col for col, data_type in _get_daily_summary_column_types()
        if data_type in _SNOWFLAKE_NUMBER_TYPES
        and col not in _DS_NON_METRIC_COLUMNS
        and not col.endswith("_N_MINUS_1")
        and not col.endswith("_TARGET")
    ]


def _target_column(metric: str) -> str | None:
    """The metric's <METRIC>_TARGET column, or None where it doesn't exist."""
    target = f"{metric}_TARGET"
    return target if target in _get_daily_summary_columns() else None


def _guess_agg(column: str) -> str:
    """Default weekly/monthly rollup: averages and stock levels average,
    everything else is a daily count that adds up."""
    upper = column.upper()
    if upper.startswith("AVG") or upper.startswith("STOCK") or "AVERAGE" in upper or "RATE" in upper or "%" in upper:
        return "avg"
    return "sum"


def _sharepoint_metric_columns() -> list:
    """Number columns of the SharePoint table, or [] when it cannot be read."""
    try:
        sp = _get_sharepoint_data()
    except Exception as e:
        logger.warning("SharePoint column list failed: %s", e)
        return []
    return sp.get("columns", []) if sp.get("status") == "ok" else []


def _validate_metric(source: str, metric: str) -> str | None:
    """Returns an error message, or None when the source/metric pair is valid."""
    entry = _data_source(source)
    if entry is None and source not in KPI_SOURCES:
        return f"Unknown source: {source}"
    if not isinstance(metric, str) or not metric.strip():
        return "Missing metric"
    if entry is not None:
        # A wide table's metric is a column name that goes into the SQL, so it
        # is always checked against the live list.
        try:
            names = _data_source_metrics(entry)
        except Exception as e:
            return f"{entry['name']}: {e}"
        if metric not in names:
            return f"Unknown metric in {entry['name']}: {metric}"
        return None
    if source == "snowflake":
        if metric not in _snowflake_metric_columns():
            return f"Unknown metric: {metric}"
    elif source == "autotrader":
        if metric not in _AT_METRICS:
            return f"Unknown Autotrader setting: {metric}"
    else:
        # Only checked against the list when the SharePoint data is already in
        # memory — reading it can mean a slow SharePoint fetch, which a layout
        # save should not wait on.
        cached_columns = _kpi_sp_last.get("columns") if _kpi_sp_last.get("status") == "ok" else None
        if cached_columns is not None and metric not in cached_columns:
            return f"Unknown SharePoint column: {metric}"
        if len(metric) > 200:
            return "Metric name is too long"
    return None


# ── KPI data sources (Settings > Data Sources) ───────────────────────────────
# Snowflake tables added on the page, admin only. The list is stored in
# kpi_layout under 'data_sources'; a chart names one by its id as its source.
# See utils/kpi_data_sources.py for the two table shapes.
#
# Targets: fixed and per-date targets and N-1 work on these as on every other
# source. A <METRIC>_TARGET column and Weight By stay daily_summary only.

_DATA_SOURCES_KEY = 'data_sources'
_DATA_SOURCES_TTL = 60       # seconds; a save clears it straight away
_SOURCE_META_TTL = 600       # columns and names; Load Data clears them too
_data_sources_cache: list | None = None
_data_sources_cache_at: float = 0.0
_source_columns_cache: dict = {}   # table -> (read at, {COLUMN: type})
_source_names_cache: dict = {}     # source id -> (read at, [metric names])


def _clear_data_source_caches() -> None:
    global _data_sources_cache
    _data_sources_cache = None
    _source_columns_cache.clear()
    _source_names_cache.clear()


def _stored_data_sources() -> list:
    stored = _read_kpi_layout_value(_DATA_SOURCES_KEY)
    if not isinstance(stored, list):
        return []
    return [s for s in stored if isinstance(s, dict) and kds.is_source_id(s.get("id"))]


def _data_sources() -> list:
    """Every Settings > Data Sources entry, in the order added."""
    global _data_sources_cache, _data_sources_cache_at
    if _data_sources_cache is not None and time.time() - _data_sources_cache_at < _DATA_SOURCES_TTL:
        return _data_sources_cache
    try:
        _data_sources_cache = _stored_data_sources()
        _data_sources_cache_at = time.time()
    except Exception as e:
        logger.warning("KPI data sources read failed: %s", e)
        return _data_sources_cache or []
    return _data_sources_cache


def _data_source(source) -> dict | None:
    """The entry for a ds_... source id, or None for any other source."""
    if not kds.is_source_id(source):
        return None
    return next((s for s in _data_sources() if s["id"] == source), None)


def _allowed_source_table(table) -> bool:
    return (isinstance(table, str) and re.fullmatch(rf"{kds.IDENTIFIER}\.{kds.IDENTIFIER}", table) is not None
            and table.split(".")[0].startswith(("COMPUTE_", "PRODUCT_")))


def _source_columns(table: str) -> dict:
    """{COLUMN: data_type} of a COMPUTE_ or PRODUCT_ table; {} if not found."""
    if not _allowed_source_table(table):
        return {}
    cached = _source_columns_cache.get(table)
    if cached and time.time() - cached[0] < _SOURCE_META_TTL:
        return cached[1]
    with _meta_lock:
        cached = _source_columns_cache.get(table)
        if cached and time.time() - cached[0] < _SOURCE_META_TTL:
            return cached[1]
        columns = dict(_vehicle_table_columns(table))
        _source_columns_cache[table] = (time.time(), columns)
        return columns


def _checked_data_source(entry: dict) -> dict:
    """The entry, once its table and columns are checked against the live
    table (they go into the SQL). Raises with the reason when they are not."""
    error = kds.validate_entry(entry, _source_columns(entry.get("table")), [])
    if error:
        raise ValueError(error)
    return entry


def _data_source_metrics(entry: dict) -> list:
    """The metric names a data source offers: a wide table's number columns,
    or every name in a long table's name column, as stored (case kept)."""
    _checked_data_source(entry)
    if entry["shape"] == "wide":
        return kds.wide_metric_columns(entry, _source_columns(entry["table"]))
    cached = _source_names_cache.get(entry["id"])
    if cached and time.time() - cached[0] < _SOURCE_META_TTL:
        return cached[1]
    with _meta_lock:
        cached = _source_names_cache.get(entry["id"])
        if cached and time.time() - cached[0] < _SOURCE_META_TTL:
            return cached[1]
        split = entry["split_column"]
        with SnowflakeClient() as sf:
            rows, _ = sf.execute(f"""
                SELECT DISTINCT {split}::VARCHAR
                FROM {_VEHICLE_DB}.{entry['table']}
                WHERE {split} IS NOT NULL
                ORDER BY 1
            """)
        names = [str(r[0]) for r in rows]
        _source_names_cache[entry["id"]] = (time.time(), names)
        return names


def _fetch_data_source(entry: dict, metrics: list, fetch_start, fetch_end) -> dict:
    """{date: {metric: value}} for the given metrics of one data source, in
    one query. Metrics not in its live list are left out."""
    live = set(_data_source_metrics(entry))
    metrics = [m for m in dict.fromkeys(metrics) if m in live]
    if not metrics:
        return {}
    sql, params = kds.fetch_sql({**entry, "table": f"{_VEHICLE_DB}.{entry['table']}"},
                                metrics, fetch_start, fetch_end)
    with SnowflakeClient() as sf:
        rows, names = sf.execute(sql, params)
    return kds.rows_to_by_date(entry, metrics, rows, names)


def _source_tables() -> list:
    """Every table and view in the COMPUTE_ and PRODUCT_ schemas, as SCHEMA.TABLE."""
    with SnowflakeClient() as sf:
        rows, _ = sf.execute(f"""
            SELECT table_schema, table_name
            FROM {_VEHICLE_DB}.INFORMATION_SCHEMA.TABLES
            WHERE table_schema LIKE %s ESCAPE '!' OR table_schema LIKE %s ESCAPE '!'
            ORDER BY table_schema, table_name
        """, list(kds.SCHEMA_PATTERNS))
    return [f"{r[0]}.{r[1]}" for r in rows]


def _data_source_uses(source_id: str) -> int:
    """How many places in the saved layout use the source."""
    return kds.references(_read_kpi_layout_value('layout') or {}, source_id)


@router.get("/kpi/data-sources/tables")
def kpi_data_source_tables(request: Request, user: str = Depends(require_page_api('kpi'))):
    """The tables that can be added. Read only when the Add or edit form
    opens, as listing every table takes a few seconds. Admin only."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the data sources"}
    try:
        return {"status": "ok", "tables": _source_tables()}
    except Exception as e:
        logger.error(f"KPI data source tables read failed: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/kpi/data-sources/columns")
def kpi_data_source_columns(request: Request, table: str, user: str = Depends(require_page_api('kpi'))):
    """One table's columns, read live so new columns show straight away. Admin only."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the data sources"}
    try:
        _source_columns_cache.pop(table, None)
        return {"status": "ok", "columns": [{"column": c, "type": t} for c, t in _source_columns(table).items()]}
    except Exception as e:
        logger.error(f"KPI data source columns read failed: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/kpi/data-sources")
def kpi_data_sources_get(request: Request, user: str = Depends(require_page_api('kpi'))):
    """The built-in sources and the saved data sources, with how many places
    use each. Admin only."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the data sources"}
    try:
        layout = _read_kpi_layout_value('layout') or {}
        sources = _stored_data_sources()
        uses = {s["id"]: kds.references(layout, s["id"]) for s in sources}
        built_in = kds.built_in_sources(
            {"snowflake": len(_snowflake_metric_columns()),
             "sharepoint": len(_sharepoint_metric_columns()),
             "autotrader": len([c for c in _AT_METRICS if c not in _AT_HIDDEN_METRICS])},
            {s: kds.references(layout, s) for s in KPI_SOURCES},
            sharepoint_sources.sources_of_type(sharepoint_sources.KPI_FILE_TYPE, enabled_only=False))
    except Exception as e:
        logger.error(f"KPI data sources read failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "built_in": built_in, "sources": sources, "uses": uses}


@router.post("/kpi/data-sources")
def kpi_data_source_save(request: Request, payload: dict, user: str = Depends(require_page_api('kpi'))):
    """Adds a data source, or saves changes to one (by its id). Admin only."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the data sources"}
    entry = (payload or {}).get("source")
    if not isinstance(entry, dict):
        return {"status": "error", "message": "bad data source"}
    entry = dict(entry)
    try:
        sources = _stored_data_sources()
        source_id = entry.get("id")
        if source_id:
            if not any(s["id"] == source_id for s in sources):
                return {"status": "error", "message": "That data source no longer exists"}
        else:
            if len(sources) >= kds.SOURCES_MAX:
                return {"status": "error", "message": f"At most {kds.SOURCES_MAX} data sources"}
            entry["id"] = kds.new_id(str(entry.get("name") or ""), {s["id"] for s in sources})
        table = entry.get("table")
        if not _allowed_source_table(table):
            return {"status": "error", "message": "Choose a COMPUTE_ or PRODUCT_ table"}
        _source_columns_cache.pop(table, None)
        columns = _source_columns(table)
        others = [s for s in sources if s["id"] != entry["id"]]
        error = kds.validate_entry(entry, columns, others)
        if error:
            return {"status": "error", "message": error}
        if entry["shape"] == "wide":
            # Each date must be one row, or a day's figure would be one of
            # several rows picked at random.
            d = entry["date_column"]
            with SnowflakeClient() as sf:
                rows, _ = sf.execute(f"SELECT COUNT(*) - COUNT(DISTINCT {d}::DATE) FROM {_VEHICLE_DB}.{table}")
            if rows and rows[0][0]:
                return {"status": "error", "message":
                        f"{table} has more than one row for some dates. Choose One Row Per Date Per Name, "
                        "or a table with one row per date"}
        entry = kds.clean_entry(entry)
        saved = [entry if s["id"] == entry["id"] else s for s in sources]
        if entry not in saved:
            saved.append(entry)
        _write_kpi_layout_value(_DATA_SOURCES_KEY, saved)
        _clear_data_source_caches()
    except Exception as e:
        logger.error(f"KPI data source save failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "source": entry, "sources": saved}


@router.delete("/kpi/data-sources")
def kpi_data_source_delete(request: Request, id: str, user: str = Depends(require_page_api('kpi'))):
    """Removes a data source no chart uses. Admin only."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the data sources"}
    try:
        sources = _stored_data_sources()
        entry = next((s for s in sources if s["id"] == id), None)
        if entry is None:
            return {"status": "error", "message": "That data source no longer exists"}
        uses = _data_source_uses(id)
        if uses:
            return {"status": "error", "message":
                    f"{entry['name']} is used in {uses} place{'s' if uses != 1 else ''} on the charts. "
                    "Change or remove those charts first"}
        saved = [s for s in sources if s["id"] != id]
        _write_kpi_layout_value(_DATA_SOURCES_KEY, saved)
        _clear_data_source_caches()
    except Exception as e:
        logger.error(f"KPI data source delete failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "sources": saved}


# Sections are added, removed, renamed and reordered in the page's Settings
# popup and stored in kpi_layout under 'sections'. This list is only used
# until the first save.
KPI_DEFAULT_SECTIONS = ["Upper Funnel", "Results", "Stock", "Pricing", "Margin"]

KPI_DEFAULT_LAYOUT = {"rows": []}


@router.get("/kpi/metrics")
def kpi_metrics(request: Request, user: str = Depends(require_page_api('kpi'))):
    live_cols = _get_daily_summary_columns()
    metrics = []
    for col in _snowflake_metric_columns():
        metrics.append({
            "source": "snowflake",
            "column": col,
            "description": "",
            "suggested_agg": _guess_agg(col),
            "has_n1": True,   # N-1 is looked up from the metric's own history
            "has_target": f"{col}_TARGET" in live_cols,
        })
    sp_descriptions = _kpi_sp_last.get("descriptions") or {}
    for col in _sharepoint_metric_columns():
        metrics.append({
            "source": "sharepoint",
            "column": col,
            "description": sp_descriptions.get(col, "From a SharePoint spreadsheet"),
            "suggested_agg": _guess_agg(col),
            # N-1 is worked out from the spreadsheet's own history.
            "has_n1": True,
            "has_target": False,
        })
    for col, description in _AT_METRICS.items():
        if col in _AT_HIDDEN_METRICS:
            continue
        metrics.append({
            "source": "autotrader",
            "column": col,
            "description": description,
            "suggested_agg": "sum" if col in _AT_SUM_METRICS else "avg",
            "has_n1": True,
            "has_target": False,
        })
    for entry in _data_sources():
        try:
            names = _data_source_metrics(entry)
        except Exception as e:
            logger.warning("KPI data source %s metric list failed: %s", entry["id"], e)
            continue
        for col in names:
            metrics.append({
                "source": entry["id"],
                "source_name": entry["name"],
                "column": col,
                "description": f"From {entry['table']}",
                "suggested_agg": _guess_agg(col),
                "has_n1": True,
                "has_target": False,
            })
    return {"status": "ok", "metrics": metrics}


# ── KPI chart periods ────────────────────────────────────────────────────────
# How many months, weeks and days the charts cover is a page setting (see
# /kpi/settings). The page sends the counts with each data request; they are
# clamped here so a bad value cannot ask Snowflake for years of data.
#
# Each chart also runs a set number of periods past the current one
# (months_after / weeks_after / days_after), with no actuals yet, so the N-1
# and target lines show what is coming. days_total includes the days after.

KPI_DEFAULT_SETTINGS = {
    "months": 14,          # including the current month
    "weeks": 10,           # including the current week
    "months_after": 1,     # months shown after the current one
    "weeks_after": 1,      # weeks shown after the current one
    "days_after": 10,      # days shown after today
    "days_window": 35,     # days on screen at once in a day chart
    "days_total": 90,      # days available to scroll across
    "label_angle": 0,      # tilt of the value labels on the data points
    "label_below": 0,      # 1 = value labels under the line's points, 0 = above
    "section_font_px": 13,  # section heading, e.g. "Upper Funnel" (was 0.82rem)
    "title_font_px": 14,    # chart titles (was 0.88rem)
    "subtitle_font_px": 12,  # optional line under a chart title (was 0.78rem)
    "period_font_px": 12,    # monthly / weekly / daily at the right of the title line
    "toolbar_font_px": 14,   # every button and label in the top bar
    "at_font_px": 13,        # Autotrader figures boxes (Published, Capped etc.)
    "at_table_font_px": 11,  # Settings > Autotrader rows table
    "legend_font_px": 10,   # series names in the legend above stacked charts
    # Line thickness in px on line charts. Halves allowed, so these are the
    # only settings that are not whole numbers (see _clamp_setting).
    "main_line_px": 2.5,     # the chart's own line
    "n1_line_px": 1.5,       # last year
    "target_line_px": 1.5,   # target, line and stacked charts
    # 1 = every user sees the Targets button and can set a chart's fixed or
    # per-date target; 0 = the admin only (Settings > Targets).
    "targets_for_all": 1,
}

# Line colours, #rrggbb. Kept apart from the number settings above.
KPI_COLOUR_SETTINGS = {
    "main_line_colour": "#0d3d6e",
    "n1_line_colour": "#6b7280",
    "target_line_colour": "#e53e3e",
}
_KPI_HALF_SETTINGS = {"main_line_px", "n1_line_px", "target_line_px"}

KPI_SETTING_LIMITS = {
    "months": (1, 24),
    "weeks": (1, 104),
    "months_after": (0, 12),
    "weeks_after": (0, 26),
    "days_after": (0, 90),
    "days_window": (7, 400),
    # At least days_after + 1 as well (today must be in it): see _period_args.
    "days_total": (7, 400),
    "label_angle": (-90, 90),
    "label_below": (0, 1),
    "section_font_px": (10, 28),
    "title_font_px": (10, 28),
    "subtitle_font_px": (8, 24),
    "period_font_px": (8, 24),
    "toolbar_font_px": (10, 22),
    "at_font_px": (9, 22),
    "at_table_font_px": (8, 16),
    "legend_font_px": (8, 24),
    "main_line_px": (0.5, 8),
    "n1_line_px": (0.5, 8),
    "target_line_px": (0.5, 8),
    "targets_for_all": (0, 1),
}


def _setting_number(name: str, value):
    """The setting as its type: a whole number, or for line thickness the
    nearest half. Raises TypeError / ValueError when it is not a number."""
    if name in _KPI_HALF_SETTINGS:
        return round(float(value) * 2) / 2
    return int(value)


def _clamp_setting(name: str, value):
    low, high = KPI_SETTING_LIMITS[name]
    try:
        value = _setting_number(name, value)
    except (TypeError, ValueError):
        value = KPI_DEFAULT_SETTINGS[name]
    return min(max(value, low), high)


def _colour_setting(name: str, value) -> str:
    return value.lower() if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) \
        else KPI_COLOUR_SETTINGS[name]


def _period_args(source: dict) -> tuple:
    """(months, weeks, days_total, after) from a request or settings dict,
    clamped. after = (months_after, weeks_after, days_after). days_total is
    raised if needed so the day chart still reaches back to today."""
    get = lambda name, key=None: _clamp_setting(name, source.get(key or name, KPI_DEFAULT_SETTINGS[name]))
    after = (get("months_after"), get("weeks_after"), get("days_after"))
    days = max(get("days_total", "days"), after[2] + 1)
    return get("months"), get("weeks"), days, after


def _kpi_periods(today: datetime.date, months: int, weeks: int, days_total: int, after: tuple = None):
    """Month starts, week Mondays and day dates for the KPI charts, oldest first.
    The months and weeks end with the current, part-elapsed one, then `after`
    more (no actuals yet, but their N-1 and target are shown)."""
    months_after, weeks_after, days_after = after or (
        KPI_DEFAULT_SETTINGS["months_after"], KPI_DEFAULT_SETTINGS["weeks_after"], KPI_DEFAULT_SETTINGS["days_after"])
    first_of_this_month = today.replace(day=1)
    month_firsts = [first_of_this_month]
    for _ in range(months - 1):
        month_firsts.insert(0, (month_firsts[0] - datetime.timedelta(days=1)).replace(day=1))
    for _ in range(months_after):
        month_firsts.append((month_firsts[-1] + datetime.timedelta(days=32)).replace(day=1))

    current_week_monday = _week_monday_of(today)
    week_mondays = [current_week_monday - datetime.timedelta(weeks=i) for i in range(weeks - 1, -weeks_after - 1, -1)]

    daily_end = today + datetime.timedelta(days=days_after)
    daily_start = daily_end - datetime.timedelta(days=days_total - 1)
    dates = [daily_start + datetime.timedelta(days=i) for i in range(days_total)]
    return month_firsts, week_mondays, dates


def _month_dates(first: datetime.date) -> list:
    return [first.replace(day=d) for d in range(1, calendar.monthrange(first.year, first.month)[1] + 1)]


def _week_dates(monday: datetime.date) -> list:
    return [monday + datetime.timedelta(days=i) for i in range(7)]


# ── KPI chart targets ────────────────────────────────────────────────────────
# A target belongs to the METRIC (source + column), so two charts on the same
# column share it. Each chart picks where its target line comes from:
#
#   none       - no target
#   fixed      - one figure per day, stored across the whole range the same way
#                as the Google Ads targets
#   import     - per-date figures pasted into the page (see /kpi/target-dates)
#   snowflake  - the column's own <METRIC>_TARGET in daily_summary
#
# Fixed and per-date figures are stored under separate keys in chart_targets,
# so switching a chart to fixed keeps the per-date figures for switching back.
# Both live in the PROD cluster, like the layout, so every environment shows
# the same targets.

KPI_TARGET_MODES = ("none", "fixed", "import", "snowflake")


def _kpi_target_key(kind: str, source: str, metric: str) -> str:
    return f"kpi:{kind}:{source}:{metric}"


def _roll_up_kpi_targets(by_date: dict, dates: list, week_mondays: list,
                         month_firsts: list, agg: str) -> tuple[list, list, list]:
    """Daily, weekly and monthly target arrays from per-date targets.

    A summed metric's period target is the average daily target times the
    period's FULL length, as on the Google Ads page — summing only the dates
    present would make the target dive on a part-covered week or month. An
    averaged metric's period target is just the average daily target.
    A period with no targeted date is None, never 0.
    """
    def _period(period_dates):
        values = [by_date[d] for d in period_dates if d in by_date]
        if not values:
            return None
        mean = sum(values) / len(values)
        return round(mean * len(period_dates) if agg == "sum" else mean, 4)

    daily = [by_date.get(d) for d in dates]
    weekly = [_period(_week_dates(m)) for m in week_mondays]
    monthly = [_period(_month_dates(f)) for f in month_firsts]
    return daily, weekly, monthly


def _stored_kpi_targets(kind: str, source: str, metric: str, dates: list,
                        week_mondays: list, month_firsts: list, agg: str):
    key = _kpi_target_key(kind, source, metric)
    start = min(dates[0], week_mondays[0], month_firsts[0])
    last_month = month_firsts[-1]
    end = max(dates[-1], week_mondays[-1] + datetime.timedelta(days=6),
              last_month.replace(day=calendar.monthrange(last_month.year, last_month.month)[1]))
    with get_prod_pricer_connection() as conn:
        by_date = get_chart_targets(conn, [key], start, end).get(key, {})
    return _roll_up_kpi_targets(by_date, dates, week_mondays, month_firsts, agg)


@router.get("/kpi/custom-data")
def kpi_custom_data(request: Request, metric: str, agg: str = "sum", source: str = "snowflake",
                    target_mode: str = "snowflake", months: int = KPI_DEFAULT_SETTINGS["months"],
                    weeks: int = KPI_DEFAULT_SETTINGS["weeks"],
                    days: int = KPI_DEFAULT_SETTINGS["days_total"],
                    current_month: bool = False,
                    current_week: bool = False,
                    current_day: bool = False,
                    refresh: str = "",
                    sp_refresh: str = "",
                    weight_by: str = "",
                    user: str = Depends(require_page_api('kpi'))):
    """One chart's data. The page now uses /kpi/custom-data-batch; kept for
    single requests. See _custom_data."""
    _refresh_daily_summary_columns(refresh)
    return _custom_data(metric, agg, source, target_mode, months, weeks, days,
                        current_month, current_week, current_day, sp_refresh, weight_by)


def _ds_fetch_range(today, months, weeks, days, after=None):
    """daily_summary dates a chart needs: a year further back than anything
    shown, for N-1, and forward to the end of the last month, week or day
    shown, so every period's target total is complete. Depends only on the
    period settings, so every chart in one load needs the same range."""
    month_firsts, week_mondays, dates = _kpi_periods(today, months, weeks, days, after)
    prev_week_mondays = [_prev_iso_week_monday(m) for m in week_mondays]
    fetch_start = min(_shift_years(dates[0], -1), _shift_years(month_firsts[0], -1),
                      prev_week_mondays[0])
    last_month = month_firsts[-1]
    fetch_end = max(dates[-1], week_mondays[-1] + datetime.timedelta(days=6),
                    last_month.replace(day=calendar.monthrange(last_month.year, last_month.month)[1]))
    return fetch_start, fetch_end


def _fetch_daily_summary(columns: list, fetch_start, fetch_end) -> dict:
    """{date: {COLUMN: value}} for the given daily_summary columns, in one query.
    Column names must come from the live column list (they are interpolated)."""
    cols = ["DATE"] + [c for c in dict.fromkeys(columns) if c != "DATE"]
    with SnowflakeClient() as sf:
        rows, names = sf.execute(f"""
            SELECT {', '.join(cols)}
            FROM PROD_CARSUPERMARKET_DB.product_slt_kpis.daily_summary
            WHERE DATE >= %s AND DATE <= %s
            ORDER BY DATE
        """, [str(fetch_start), str(fetch_end)])
    names = [n.upper() for n in names]
    out = {}
    for r in rows:
        row = dict(zip(names, r))
        d = row.pop("DATE")
        d = d if isinstance(d, datetime.date) else datetime.date.fromisoformat(str(d)[:10])
        out[d] = row
    return out


def _custom_data(metric: str, agg: str, source: str, target_mode: str, months: int, weeks: int,
                 days: int, current_month: bool, current_week: bool, current_day: bool,
                 sp_refresh: str = "", weight_by: str = "",
                 prefetched: dict | None = None, stored_targets: dict | None = None,
                 after: tuple | None = None, multiply_source: str = "", multiply_metric: str = "",
                 at_values=None, sp_data: dict | None = None,
                 source_data: dict | None = None) -> dict:
    """Daily/weekly/monthly series for one metric, plus its N-1 and target.
    current_month / current_week / current_day: also fill the current
    (part-elapsed) month, week or day. Today only has stock figures in
    daily_summary; other columns are NULL until tomorrow's build.
    refresh: Load Data's click token — re-reads the column list once per click.
    sp_refresh: the same token, sent only when the Load Data button is clicked
    — asks SharePoint now whether each file has been saved since its copy (see
    _get_sharepoint_data).
    weight_by: on an Average chart, a daily_summary count column to weight
    each day by, so a week or month is the average over every vehicle rather
    than the plain average of the daily averages (a 3-order day would
    otherwise count as much as a 20-order day).
    prefetched: {date: {COLUMN: value}} already read from daily_summary for the
    whole load (see kpi_custom_data_batch); without it this reads its own.
    stored_targets: {target key: {date: value}} already read, likewise.
    multiply_source / multiply_metric: Multiply By. Each day's figure (and its
    target) is multiplied by the second metric's figure for the same day,
    before the days are added or averaged into weeks and months, so e.g.
    slots x cost per slot per day, as a Sum, gives the right cost for any
    week or month.
    at_values: _at_setting_values() already built for the whole load.
    sp_data: _get_sharepoint_data() already built for the whole load.
    source_data: {data source id: {date: {metric: value}}} already read for
    the whole load (Settings > Data Sources); a source missing from it is
    read here."""
    if source == "snowflake":
        metric = metric.upper()
        weight_by = weight_by.upper()
    if multiply_source == "snowflake":
        multiply_metric = multiply_metric.upper()
    error = _validate_metric(source, metric)
    if error:
        return {"status": "error", "message": error}
    if multiply_metric:
        error = _validate_metric(multiply_source, multiply_metric)
        if error:
            return {"status": "error", "message": f"Multiply by: {error}"}
    if agg not in ("sum", "avg"):
        return {"status": "error", "message": f"Unknown agg: {agg}"}
    if weight_by:
        if source != "snowflake" or agg != "avg":
            return {"status": "error", "message": "Weight by is only for Average charts on daily_summary"}
        if weight_by not in _snowflake_metric_columns():
            return {"status": "error", "message": f"Unknown weight column: {weight_by}"}
    if target_mode not in KPI_TARGET_MODES:
        return {"status": "error", "message": f"Unknown target mode: {target_mode}"}
    months, weeks, days, after = _period_args({"months": months, "weeks": weeks, "days": days,
                                               **dict(zip(("months_after", "weeks_after", "days_after"), after or ()))})
    try:
        today = datetime.date.today()
        month_firsts, week_mondays, dates = _kpi_periods(today, months, weeks, days, after)
        prev_week_mondays = [_prev_iso_week_monday(m) for m in week_mondays]

        def _num(val):
            try:
                return float(val) if val is not None else None
            except (TypeError, ValueError):
                return None

        def _round(v):
            if v is None:
                return None
            # 4 places, not 2: a ratio like price position (0.9876) shown as a
            # percent needs them for 98.76%. Display decimals are set per chart.
            return int(v) if float(v).is_integer() else round(v, 4)

        def _agg(vals):
            known = [v for v in vals if v is not None]
            if not known:
                return None
            total = sum(known)
            return _round(total if agg == 'sum' else total / len(known))

        target_col = _target_column(metric) if source == "snowflake" else None
        stale_date = None
        sources = {source, multiply_source} if multiply_metric else {source}
        if "snowflake" in sources and prefetched is None:
            # All column names come from the live column list — safe to interpolate
            fetch_start, fetch_end = _ds_fetch_range(today, months, weeks, days, after)
            columns = ([metric] if source == "snowflake" else []) + ([target_col] if target_col else []) \
                + ([weight_by] if weight_by else []) \
                + ([multiply_metric] if multiply_metric and multiply_source == "snowflake" else [])
            prefetched = _fetch_daily_summary(columns, fetch_start, fetch_end)
        sp_by_date = {}
        if "sharepoint" in sources:
            sp = sp_data if sp_data is not None else _get_sharepoint_data(sp_refresh)
            if sp.get("status") != "ok":
                return {"status": "error", "message": sp.get("message", "SharePoint data unavailable")}
            stale_date = sp.get("stale_date") if sp.get("stale") else None
            sp_by_date = sp.get("by_date", {})
        if "autotrader" in sources and at_values is None:
            at_values = _at_setting_values()
        source_data = dict(source_data or {})
        for src_id in sources:
            entry = _data_source(src_id)
            if entry is not None and src_id not in source_data:
                fetch_start, fetch_end = _ds_fetch_range(today, months, weeks, days, after)
                wanted = [m for s, m in ((source, metric), (multiply_source, multiply_metric)) if s == src_id and m]
                source_data[src_id] = _fetch_data_source(entry, wanted, fetch_start, fetch_end)

        # One metric's figure for one day, from whichever source it is in.
        # N-1 is looked up from the metric's own history (below), the same way
        # for every source, so the <METRIC>_N_MINUS_1 column is not read.
        def lookup(src, met):
            if src == "snowflake":
                return lambda d: _num((prefetched.get(d) or {}).get(met))
            if src == "autotrader":
                return lambda d: _num(at_values(d, met))
            if src in source_data:
                src_rows = source_data[src]
                return lambda d: _num((src_rows.get(d) or {}).get(met))
            return lambda d: _num(sp_by_date.get(d.isoformat(), {}).get(met))

        base_at = lookup(source, metric)
        if multiply_metric:
            factor_at = lookup(multiply_source, multiply_metric)

            def times(v, d):
                f = factor_at(d) if v is not None else None
                return v * f if f is not None else None
            value_at = lambda d: times(base_at(d), d)
        else:
            times = lambda v, d: v
            value_at = base_at
        by_date: dict = {
            d: {
                't': times(_num(row.get(target_col)), d) if target_col else None,
                'w': _num(row.get(weight_by)) if weight_by else None,
            }
            for d, row in (prefetched or {}).items()
        }

        # One week's or month's figure from its days. With weight_by, each
        # day's average counts in proportion to that day's count: sum(average
        # x count) / sum(count), which is the average over every vehicle in the
        # period. Days with no average or no count are left out.
        def _period(period_dates):
            if not weight_by:
                return _agg([value_at(d) for d in period_dates])
            pairs = [(value_at(d), by_date.get(d, {}).get('w')) for d in period_dates]
            pairs = [(v, w) for v, w in pairs if v is not None and w]
            total_weight = sum(w for _, w in pairs)
            return _round(sum(v * w for v, w in pairs) / total_weight) if total_weight else None

        # N-1 from the metric's own history, for both sources: the same date
        # a year back for days (as daily_summary's DATEADD(YEAR, -1)), the same
        # month a year back for months. Runs to the end of the day chart,
        # future days included.
        daily_n1_at = lambda d: value_at(_shift_years(d, -1))
        monthly_n1 = [_period(_month_dates(f.replace(year=f.year - 1))) for f in month_firsts]

        month_abbrs = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

        # ── Daily ─────────────────────────────────────────────────────────────
        # Today is left empty unless the chart asks for it; future days always
        # are (only their N-1 and target are shown).
        values = [None if d > today or (d == today and not current_day) else _round(value_at(d))
                  for d in dates]
        values_n1 = [_round(daily_n1_at(d)) for d in dates]

        # ── Monthly (current month left empty — it is part elapsed — unless
        # the chart asks for it; next month always empty) ────────────────────
        first_of_this_month = today.replace(day=1)
        monthly_values = [None if f > first_of_this_month or (f == first_of_this_month and not current_month)
                          else _period(_month_dates(f))
                          for f in month_firsts]
        monthly_labels = [month_abbrs[f.month - 1] + ' ' + str(f.year)[2:] for f in month_firsts]

        # ── Weekly (current week left empty unless asked for; next week always
        # empty) ──────────────────────────────────────────────────────────────
        # Weekly N-1 = same ISO week NUMBER of the previous year, aggregated
        # from the metric's own daily history.
        current_week_monday = _week_monday_of(today)
        weekly_labels = [f"W{m.isocalendar()[1]} ({m.day}-{month_abbrs[m.month-1]})" for m in week_mondays]
        weekly_n1_labels = [f"W{pm.isocalendar()[1]} ({pm.day}-{month_abbrs[pm.month-1]})" for pm in prev_week_mondays]
        weekly_values = [None if m > current_week_monday or (m == current_week_monday and not current_week)
                         else _period(_week_dates(m))
                         for m in week_mondays]
        weekly_values_n1 = [_period(_week_dates(pm)) for pm in prev_week_mondays]

        # ── Target ───────────────────────────────────────────────────────────
        values_target = weekly_values_target = monthly_values_target = None
        if target_mode == "snowflake" and target_col:
            values_target = [_round(by_date.get(d, {}).get('t')) for d in dates]
            weekly_values_target = [_agg([by_date.get(d, {}).get('t') for d in _week_dates(m)]) for m in week_mondays]
            monthly_values_target = [_agg([by_date.get(d, {}).get('t') for d in _month_dates(f)]) for f in month_firsts]
        elif target_mode in ("fixed", "import"):
            key = _kpi_target_key(target_mode, source, metric)
            if stored_targets is not None:
                values_target, weekly_values_target, monthly_values_target = _roll_up_kpi_targets(
                    stored_targets.get(key, {}), dates, week_mondays, month_firsts, agg)
            else:
                values_target, weekly_values_target, monthly_values_target = _stored_kpi_targets(
                    target_mode, source, metric, dates, week_mondays, month_firsts, agg)

        return {
            "status": "ok",
            "today": today.isoformat(),
            "source": source,
            "metric": metric,
            "agg": agg,
            "target_mode": target_mode,
            # Set when SharePoint could not be reached and an older copy was used.
            "stale_date": stale_date,
            "has_n1": values_n1 is not None,
            "has_target": values_target is not None and any(v is not None for v in values_target),
            "dates": [d.isoformat() for d in dates],
            "values": values,
            "values_n1": values_n1,
            "values_target": values_target,
            "monthly_labels": monthly_labels,
            "monthly_values": monthly_values,
            "monthly_values_n1": monthly_n1,
            "monthly_values_target": monthly_values_target,
            # First date of each month / week, so the page can place chart notes.
            "monthly_starts": [f.isoformat() for f in month_firsts],
            "weekly_starts": [m.isoformat() for m in week_mondays],
            "weekly_labels": weekly_labels,
            "weekly_n1_labels": weekly_n1_labels,
            "weekly_values": weekly_values,
            "weekly_values_n1": weekly_values_n1,
            "weekly_values_target": weekly_values_target,
        }
    except Exception as e:
        logger.error(f"KPI custom data failed for {source}:{metric}: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/kpi/custom-data-batch")
def kpi_custom_data_batch(request: Request, payload: dict, user: str = Depends(require_page_api('kpi'))):
    """Every chart's data for one page load in a single request. payload:
    {items: [{source, metric, agg, target_mode, current_month, current_week,
    current_day, weight_by}], months, weeks, days, refresh, sp_refresh}.
    Reads every daily_summary column needed in one Snowflake query, the
    stored targets in one database call and SharePoint once, then builds each
    item from those. Returns {status, results} with results in item order."""
    payload = payload or {}
    items = payload.get("items")
    if not isinstance(items, list) or len(items) > 500:
        return {"status": "error", "message": "items must be a list of at most 500"}
    _refresh_daily_summary_columns(str(payload.get("refresh") or ""))
    months, weeks, days, after = _period_args(payload)
    sp_refresh = str(payload.get("sp_refresh") or "")

    def norm(item):
        item = item if isinstance(item, dict) else {}
        source = item.get("source", "snowflake")
        metric = str(item.get("metric") or "")
        weight_by = str(item.get("weight_by") or "")
        if source == "snowflake":
            metric, weight_by = metric.upper(), weight_by.upper()
        multiply_source = str(item.get("multiply_source") or "snowflake")
        multiply_metric = str(item.get("multiply_metric") or "")
        if multiply_source == "snowflake":
            multiply_metric = multiply_metric.upper()
        return {"source": source, "metric": metric, "agg": item.get("agg", "sum"),
                "target_mode": item.get("target_mode", "snowflake"),
                "current_month": bool(item.get("current_month")),
                "current_week": bool(item.get("current_week")),
                "current_day": bool(item.get("current_day")),
                "weight_by": weight_by,
                "multiply_source": multiply_source if multiply_metric else "",
                "multiply_metric": multiply_metric}

    items = [norm(i) for i in items]

    try:
        today = datetime.date.today()
        # Every daily_summary column any item needs, read once. Only names in
        # the live column list are used, so they are safe to interpolate; an
        # unknown one gets its error from _custom_data below.
        live = set(_snowflake_metric_columns())
        columns = []
        for i in items:
            if i["multiply_source"] == "snowflake" and i["multiply_metric"] in live:
                columns.append(i["multiply_metric"])
            if i["source"] != "snowflake" or i["metric"] not in live:
                continue
            columns.append(i["metric"])
            target_col = _target_column(i["metric"])
            if target_col:
                columns.append(target_col)
            if i["weight_by"] in live:
                columns.append(i["weight_by"])
        prefetched = None
        if columns:
            fetch_start, fetch_end = _ds_fetch_range(today, months, weeks, days, after)
            prefetched = _fetch_daily_summary(columns, fetch_start, fetch_end)

        # Stored (fixed / imported) targets for every item that uses them, in one call.
        stored = None
        keys = list(dict.fromkeys(_kpi_target_key(i["target_mode"], i["source"], i["metric"])
                                  for i in items if i["target_mode"] in ("fixed", "import")))
        if keys:
            month_firsts, week_mondays, dates = _kpi_periods(today, months, weeks, days, after)
            last_month = month_firsts[-1]
            start = min(dates[0], week_mondays[0], month_firsts[0])
            end = max(dates[-1], week_mondays[-1] + datetime.timedelta(days=6),
                      last_month.replace(day=calendar.monthrange(last_month.year, last_month.month)[1]))
            with get_prod_pricer_connection() as conn:
                stored = get_chart_targets(conn, keys, start, end)

        # SharePoint once (and its Load Data check once), before the items use it.
        used = {i["source"] for i in items} | {i["multiply_source"] for i in items}
        sp = _get_sharepoint_data(sp_refresh) if "sharepoint" in used else None
        # Autotrader settings and allowance history read once for every item.
        at_values = _at_setting_values() if "autotrader" in used else None

        # Each Settings > Data Sources table read once, for every metric of it
        # any item needs. One that fails only fails the charts using it.
        wanted = {}
        for i in items:
            for src_id, met in ((i["source"], i["metric"]), (i["multiply_source"], i["multiply_metric"])):
                if met and _data_source(src_id) is not None:
                    wanted.setdefault(src_id, []).append(met)
        source_data, source_errors = {}, {}
        if wanted:
            fetch_start, fetch_end = _ds_fetch_range(today, months, weeks, days, after)
            for src_id, mets in wanted.items():
                try:
                    source_data[src_id] = _fetch_data_source(_data_source(src_id), mets, fetch_start, fetch_end)
                except Exception as e:
                    logger.error("KPI data source %s read failed: %s", src_id, e)
                    source_errors[src_id] = f"{_data_source(src_id)['name']}: {e}"
    except Exception as e:
        logger.error(f"KPI batch data failed: {e}")
        return {"status": "error", "message": str(e)}

    # Build each different item once; repeats share the result.
    built = {}
    results = []
    for i in items:
        key = json.dumps(i, sort_keys=True)
        failed = source_errors.get(i["source"]) or (source_errors.get(i["multiply_source"]) if i["multiply_metric"] else None)
        if failed:
            built[key] = {"status": "error", "message": failed}
        if key not in built:
            built[key] = _custom_data(
                i["metric"], i["agg"], i["source"], i["target_mode"], months, weeks, days,
                i["current_month"], i["current_week"], i["current_day"], "", i["weight_by"],
                prefetched=prefetched if prefetched is not None else {}, stored_targets=stored, after=after,
                multiply_source=i["multiply_source"], multiply_metric=i["multiply_metric"],
                at_values=at_values, sp_data=sp, source_data=source_data)
        results.append(built[key])
    out = {"status": "ok", "results": results}
    if sp and sp.get("status") == "ok":
        # Files being downloaded now: the page asks /api/sharepoint/status
        # until they have arrived, then reloads the charts.
        out["sp_pending"] = sp.get("pending") or []
        out["sp_keys"] = sp.get("keys") or []
        out["sp_check_seconds"] = sp.get("check_seconds")
    return out


# ── KPI target editing ───────────────────────────────────────────────────────

@router.get("/kpi/metric-target")
def kpi_get_metric_target(request: Request, source: str, metric: str,
                          user: str = Depends(require_page_api('kpi'))):
    """What is stored for one metric: the fixed figure and a summary of the
    imported dates. Read separately from the chart data so the popup shows
    what is stored even for dates no chart is drawing."""
    if source == "snowflake":
        metric = metric.upper()
    error = _validate_metric(source, metric)
    if error:
        return {"status": "error", "message": error}
    fixed_key = _kpi_target_key("fixed", source, metric)
    import_key = _kpi_target_key("import", source, metric)
    try:
        with get_prod_pricer_connection() as conn:
            fixed = get_chart_target_ranges(conn, [fixed_key]).get(fixed_key)
            imported = get_chart_target_summary(conn, import_key)
    except Exception as e:
        logger.error("Reading KPI target failed for %s:%s: %s", source, metric, e)
        return {"status": "error", "message": str(e)}
    has_snowflake = source == "snowflake" and _target_column(metric) is not None
    return {
        "status": "ok",
        "fixed": fixed['value'] if fixed else None,
        "imported": None if not imported else {
            "count": imported['count'],
            "first": imported['first'].isoformat(),
            "last": imported['last'].isoformat(),
        },
        "has_snowflake_target": has_snowflake,
    }


@router.post("/kpi/metric-target/fixed")
async def kpi_save_fixed_target(request: Request, user: str = Depends(require_page_api('kpi'))):
    """Body: {"source", "metric", "value": number|null}. null removes the fixed
    figure. Open to every KPI user while targets_for_all is on, from the
    Targets popup."""
    refused = _targets_refused(user)
    if refused:
        return {"status": "error", "message": refused}
    body = await request.json()
    source, metric = body.get('source'), body.get('metric')
    if source == "snowflake" and isinstance(metric, str):
        metric = metric.upper()
    error = _validate_metric(source, metric)
    if error:
        return {"status": "error", "message": error}
    key = _kpi_target_key("fixed", source, metric)
    try:
        with get_prod_pricer_connection() as conn:
            if body.get('value') is None:
                # The whole fixed figure goes, past dates included — it is one
                # figure, not a history.
                delete_chart_target(conn, key, datetime.date(1900, 1, 1))
                return {"status": "ok", "fixed": None}
            value, error = _target_value_or_error(body.get('value'))
            if error:
                return {"status": "error", "message": error}
            start, end = _target_write_range()
            save_chart_targets(conn, [(key, value)], start, end, user)
    except Exception as e:
        logger.error("Saving KPI fixed target failed for %s:%s: %s", source, metric, e)
        return {"status": "error", "message": f"Save failed: {e}"}
    return {"status": "ok", "fixed": value}


KPI_PASTE_MAX_ROWS = 5000

# A chart that keeps its own target (a ratio chart, or one with added metrics)
# stores its per-date targets under its own id, not a metric: its figures are
# the chart's result (e.g. a conversion %), which no single metric holds.
# Keyed by id rather than title, so renaming the chart keeps them.
_ROW_ID = r"[A-Za-z0-9_-]{1,40}"


def _kpi_row_target_key(row_id: str) -> str:
    return f"kpi:row:{row_id}"


def _target_dates_key(source, metric, row_id) -> tuple[str | None, str | None]:
    """(key, error) for a per-date target list: a metric's, or a chart's own."""
    if row_id is not None:
        if not isinstance(row_id, str) or not re.fullmatch(_ROW_ID, row_id):
            return None, "bad chart id"
        return _kpi_row_target_key(row_id), None
    if source == "snowflake" and isinstance(metric, str):
        metric = metric.upper()
    error = _validate_metric(source, metric)
    if error:
        return None, error
    return _kpi_target_key("import", source, metric), None


@router.get("/kpi/target-dates")
def kpi_get_target_dates(request: Request, source: str = None, metric: str = None, row_id: str = None,
                         user: str = Depends(require_page_api('kpi'))):
    """Every stored per-date target for a metric (source + metric) or for a
    chart's own target (row_id), oldest first: {rows: [[date, value], ...]}."""
    key, error = _target_dates_key(source, metric, row_id)
    if error:
        return {"status": "error", "message": error}
    try:
        with get_prod_pricer_connection() as conn:
            by_date = get_chart_targets(conn, [key], datetime.date(1900, 1, 1),
                                        datetime.date(2999, 12, 31)).get(key, {})
    except Exception as e:
        logger.error("Reading KPI target dates failed for %s: %s", key, e)
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "rows": [[d.isoformat(), v] for d, v in sorted(by_date.items())]}


@router.post("/kpi/target-dates")
async def kpi_save_target_dates(request: Request, user: str = Depends(require_page_api('kpi'))):
    """Body: {"source", "metric", "rows": [{"date", "value"}]} or {"row_id", "rows"}.

    Replaces the whole list: the box on the page shows every stored date, so a
    date taken out of it is removed. Empty rows removes them all. Open to
    every KPI user while targets_for_all is on, from the Targets popup.
    """
    refused = _targets_refused(user)
    if refused:
        return {"status": "error", "message": refused}
    body = await request.json()
    key, error = _target_dates_key(body.get('source'), body.get('metric'), body.get('row_id'))
    if error:
        return {"status": "error", "message": error}
    raw_rows = body.get('rows')
    if not isinstance(raw_rows, list):
        return {"status": "error", "message": "bad rows"}
    if len(raw_rows) > KPI_PASTE_MAX_ROWS:
        return {"status": "error", "message": f"Too many rows (limit {KPI_PASTE_MAX_ROWS})"}

    by_date = {}
    for index, row in enumerate(raw_rows, start=1):
        if not isinstance(row, dict):
            return {"status": "error", "message": f"Row {index}: bad row"}
        try:
            d = datetime.date.fromisoformat(str(row.get('date')))
        except (TypeError, ValueError):
            return {"status": "error", "message": f"Row {index}: bad date {row.get('date')!r}"}
        value, error = _target_value_or_error(row.get('value'))
        if error:
            return {"status": "error", "message": f"Row {index} ({d}): {error}"}
        if d in by_date:
            return {"status": "error", "message": f"{d} is in the list twice"}
        by_date[d] = value

    try:
        with get_prod_pricer_connection() as conn:
            replace_chart_target_dates(conn, key, sorted(by_date.items()), user)
    except Exception as e:
        logger.error("Saving KPI target dates failed for %s: %s", key, e)
        return {"status": "error", "message": f"Save failed: {e}"}
    return {"status": "ok", "count": len(by_date)}


@router.get("/kpi/targets/overview")
def kpi_targets_overview(request: Request, user: str = Depends(require_page_api('kpi'))):
    """Every stored KPI target, for the Targets tab in Settings:
    [{kind: fixed|dates|row, source, metric, row_id, count, first, last, value}].
    A fixed figure is one value stored across many dates, so it comes back as
    that value; per-date lists come back as a count and date range."""
    try:
        with get_prod_pricer_connection() as conn:
            rows = get_chart_target_overview(conn, "kpi:")
    except Exception as e:
        logger.error("Reading KPI target overview failed: %s", e)
        return {"status": "error", "message": str(e)}
    out = []
    for key, count, first, last, low, high in rows:
        parts = key.split(":", 3)
        item = {"count": count, "first": first.isoformat(), "last": last.isoformat(),
                "value": float(low) if low == high else None}
        if parts[1] == "row" and len(parts) == 3:
            item.update(kind="row", row_id=parts[2])
        elif parts[1] in ("fixed", "import") and len(parts) == 4:
            item.update(kind="fixed" if parts[1] == "fixed" else "dates", source=parts[2], metric=parts[3])
        else:
            continue
        out.append(item)
    return {"status": "ok", "targets": out}


# ── Layout and settings ──────────────────────────────────────────────────────

def _validate_layout_metric(source: str, metric) -> str | None:
    """Layout-save check for a chart's metric. A Snowflake column only has to
    look like a column name, not exist right now: the whole layout is saved in
    one go, so one chart on a column that was renamed or dropped from
    daily_summary would otherwise block every save, including the one that
    moves it to the new column. /kpi/custom-data still checks the live list
    before any SQL runs, and that chart shows the error instead."""
    if source == "snowflake":
        if not isinstance(metric, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", metric):
            return f"Bad metric: {metric}"
        return None
    # A data source's metric likewise only has to look right: an event that
    # stops being sent should not block every layout save.
    if kds.is_source_id(source):
        if _data_source(source) is None:
            return f"Unknown data source: {source}"
        if not isinstance(metric, str) or not metric.strip() or len(metric) > 200:
            return f"Bad metric: {metric}"
        return None
    return _validate_metric(source, metric)


# ── Chart types ──────────────────────────────────────────────────────────────
# "line" (the default): one metric with N-1 and a target, month / week / day.
# "stacked": stacked columns, one segment per series, month / week / day, no
# N-1. The page fetches each series with /kpi/custom-data and stacks them.
# row["metric"] / row["source"] hold the first series, so code that reads a
# chart's metric (titles, notes) still has one.
KPI_CHART_TYPES = ("line", "stacked")
_KPI_SERIES_MAX = 12
_HEX_COLOUR = r"#[0-9a-fA-F]{6}"


def _validate_stacked_row(row: dict) -> str | None:
    chart_type = row.get("chart_type", "line")
    if chart_type not in KPI_CHART_TYPES:
        return f"bad chart type: {chart_type}"
    if chart_type != "stacked":
        return None
    series = row.get("series")
    if not isinstance(series, list) or not series:
        return "A stacked chart needs at least one series"
    if len(series) > _KPI_SERIES_MAX:
        return f"At most {_KPI_SERIES_MAX} series in a chart"
    for s in series:
        if not isinstance(s, dict) or set(s) - {"source", "metric", "label", "colour", "multiply_by", "within_below", "axis"}:
            return "bad series"
        if s.get("axis", "primary") not in ("primary", "secondary"):
            return "bad series axis"
        if not isinstance(s.get("within_below", False), bool):
            return "bad within_below"
        error = _validate_layout_metric(s.get("source", "snowflake"), s.get("metric"))
        if error:
            return error
        # Multiply By on a series: each day of its metric times the same day
        # of a second metric (e.g. PPC clicks x cost per PPC click).
        multiply_by = s.get("multiply_by")
        if multiply_by is not None:
            if not isinstance(multiply_by, dict) or set(multiply_by) - {"source", "metric"}:
                return "bad series multiply_by"
            error = _validate_layout_metric(multiply_by.get("source", "snowflake"), multiply_by.get("metric"))
            if error:
                return error
        label = s.get("label", "")
        if not isinstance(label, str) or len(label) > 60:
            return "Series names must be 60 characters or fewer"
        colour = s.get("colour")
        if colour is not None and (not isinstance(colour, str) or not re.fullmatch(_HEX_COLOUR, colour)):
            return f"bad colour: {colour}"
    theme = row.get("theme")
    if theme is not None and (not isinstance(theme, str) or len(theme) > 40):
        return "bad theme"
    if row.get("column_layout", "stacked") not in ("stacked", "side_by_side"):
        return "bad column_layout"
    for flag in ("pct", "seg_labels"):
        if not isinstance(row.get(flag, False), bool):
            return f"bad {flag}"
    if row.get("divide_by") is not None or row.get("weight_by") is not None:
        return "A stacked chart cannot be divided or weighted"
    if row.get("metric_plus") or row.get("divide_plus"):
        return "A stacked chart cannot have added metrics"
    if row.get("target_mode", "none") not in ("none", "fixed", "import"):
        return "A stacked chart's target must be none, fixed or per date"
    target = row.get("stack_target")
    if target is not None and (isinstance(target, bool) or not isinstance(target, (int, float))):
        return "bad stack_target"
    return None


# Colour themes for multi-series charts: [{id, name, colours: [hex, ...]}],
# edited in Settings > Appearance. Series take a theme's colours in order;
# a series past the last colour is grey (colours are never repeated, so two
# series never look the same). The built-in ones are used until any are saved.
# Checked with the dataviz palette validator (light surface): Standard and
# Brand blue pass every check, colour-blind separation included. Blues is an
# ordered ramp, dark to light, for series that have an order (e.g. price bands).
KPI_DEFAULT_THEMES = [
    {"id": "standard", "name": "Standard",
     "colours": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]},
    {"id": "brand", "name": "Brand blue",
     "colours": ["#256abf", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#4a3aa7"]},
    {"id": "blues", "name": "Blues (ordered)",
     "colours": ["#0d366b", "#1c5cab", "#2a78d6", "#5598e7", "#86b6ef"]},
]


def _validate_themes(themes) -> str | None:
    if not isinstance(themes, list) or not themes:
        return "At least one colour theme is needed"
    if len(themes) > 30:
        return "At most 30 colour themes"
    ids = set()
    for t in themes:
        if not isinstance(t, dict) or set(t) - {"id", "name", "colours", "transparency"}:
            return "bad theme"
        # How see-through the theme's colours are, 0 (solid) to 90 %.
        transparency = t.get("transparency", 0)
        if isinstance(transparency, bool) or not isinstance(transparency, int) or not 0 <= transparency <= 90:
            return f"{t.get('name')}: transparency must be 0 to 90 %"
        theme_id, name, colours = t.get("id"), t.get("name"), t.get("colours")
        if not isinstance(theme_id, str) or not re.fullmatch(r"[a-z0-9_-]{1,40}", theme_id) or theme_id in ids:
            return "bad theme id"
        ids.add(theme_id)
        if not isinstance(name, str) or not name.strip() or len(name) > 40:
            return "Theme names must be 1 to 40 characters"
        if not isinstance(colours, list) or not 1 <= len(colours) <= 20:
            return f"{name}: a theme needs 1 to 20 colours"
        if any(not isinstance(c, str) or not re.fullmatch(_HEX_COLOUR, c) for c in colours):
            return f"{name}: colours must be #rrggbb"
    return None


@router.post("/kpi/themes")
def kpi_themes_save(request: Request, payload: dict, user: str = Depends(require_page_api('kpi'))):
    """The colour themes for multi-series charts. Admin only."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the colour themes"}
    themes = (payload or {}).get("themes")
    err = _validate_themes(themes)
    if err:
        return {"status": "error", "message": err}
    themes = [{**t, "name": t["name"].strip(), "colours": [c.lower() for c in t["colours"]]} for t in themes]
    try:
        _write_kpi_layout_value('themes', themes)
    except Exception as e:
        logger.error(f"KPI themes save failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "themes": themes}


# Display names for raw columns, shared by every chart: {"source|column": name},
# e.g. {"snowflake|AT_PORTAL_ENQUIRIES": "AT enquiries"}. Typed in a chart's
# series list or in Settings > Column Names. Until first saved, it is built
# from the names already typed on stacked chart series.
# SharePoint columns are spreadsheet headers, so any printable text.
_METRIC_NAME_KEY = rf"(snowflake|sharepoint|autotrader|{kds.SOURCE_ID})\|[^\x00-\x1f]{{1,150}}"


def _seed_metric_names(layout: dict) -> dict:
    names = {}
    for row in layout.get("rows", []):
        for s in row.get("series") or []:
            label = (s.get("label") or "").strip()
            if label and s.get("metric"):
                names.setdefault(f"{s.get('source', 'snowflake')}|{s['metric']}", label)
    return names


def _validate_metric_names(names) -> str | None:
    if not isinstance(names, dict) or len(names) > 500:
        return "bad column names"
    for key, name in names.items():
        if not isinstance(key, str) or not re.fullmatch(_METRIC_NAME_KEY, key):
            return f"bad column: {key}"
        if not isinstance(name, str) or not name.strip() or len(name) > 60:
            return f"{key.split('|', 1)[-1]}: names must be 1 to 60 characters"
    return None


@router.post("/kpi/metric-names")
def kpi_metric_names_save(request: Request, payload: dict, user: str = Depends(require_page_api('kpi'))):
    """The shared column display names. Admin only. Replaces the whole list."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change column names"}
    names = (payload or {}).get("names")
    err = _validate_metric_names(names)
    if err:
        return {"status": "error", "message": err}
    names = {k: v.strip() for k, v in names.items()}
    try:
        _write_kpi_layout_value('metric_names', names)
    except Exception as e:
        logger.error(f"KPI column names save failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "names": names}


# How each raw column's figure shows in the point popup's breakdown of a
# calculated chart: {"source|column": {"fmt", "decimals"}}. Set in
# Settings > Column Names. A column with no entry shows as a plain number.
_METRIC_FORMATS = ("num", "pct", "pct100", "k", "km", "gbp", "gbpkm")


def _validate_metric_formats(formats) -> str | None:
    if not isinstance(formats, dict) or len(formats) > 500:
        return "bad column formats"
    for key, f in formats.items():
        if not isinstance(key, str) or not re.fullmatch(_METRIC_NAME_KEY, key):
            return f"bad column: {key}"
        column = key.split("|", 1)[-1]
        if not isinstance(f, dict) or set(f) - {"fmt", "decimals"}:
            return f"{column}: bad format"
        if f.get("fmt") not in _METRIC_FORMATS:
            return f"{column}: bad format: {f.get('fmt')}"
        decimals = f.get("decimals", 0)
        if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 3:
            return f"{column}: bad decimals: {decimals}"
    return None


@router.post("/kpi/metric-formats")
def kpi_metric_formats_save(request: Request, payload: dict, user: str = Depends(require_page_api('kpi'))):
    """The shared column formats. Admin only. Replaces the whole list."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change column formats"}
    formats = (payload or {}).get("formats")
    err = _validate_metric_formats(formats)
    if err:
        return {"status": "error", "message": err}
    formats = {k: {"fmt": f["fmt"], "decimals": f.get("decimals", 0)} for k, f in formats.items()}
    try:
        _write_kpi_layout_value('metric_formats', formats)
    except Exception as e:
        logger.error(f"KPI column formats save failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "formats": formats}


# A calculated target (target_mode "calc", plain line charts only): two other
# targets combined, target_calc = {"a": ref, "op": "times" | "divide", "b": ref},
# e.g. orders = enquiries target x conversion % target, or average price =
# revenue target / orders target. The page works it out (kpi_targets.js). A ref:
#   {"kind": "metric", "source", "metric", "mode": "import" | "fixed" | "snowflake"}
#   {"kind": "chart", "row_id"}   that chart's own target, as it draws it
#   {"kind": "value", "value"}    a typed figure (12% is 0.12)
_CALC_REF_MODES = ("import", "fixed", "snowflake")


def _validate_calc_ref(ref, row_id) -> str | None:
    if not isinstance(ref, dict):
        return "bad calculated target"
    kind = ref.get("kind")
    if kind == "metric":
        if ref.get("mode") not in _CALC_REF_MODES:
            return "bad calculated target source"
        return _validate_layout_metric(ref.get("source", "snowflake"), ref.get("metric"))
    if kind == "chart":
        ref_id = ref.get("row_id")
        if not isinstance(ref_id, str) or not re.fullmatch(_ROW_ID, ref_id):
            return "bad calculated target chart"
        if ref_id == row_id:
            return "A calculated target cannot use its own chart"
        return None
    if kind == "value":
        value = ref.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "The calculated target's figure must be a number"
        return None
    return "bad calculated target"


def _validate_target_calc(row: dict) -> str | None:
    calc = row.get("target_calc")
    if row.get("target_mode") != "calc":
        return "target_calc is only for a calculated target" if calc is not None else None
    if row.get("chart_type", "line") != "line" or row.get("divide_by") or row.get("metric_plus"):
        return "Only a plain line chart can have a calculated target"
    if not isinstance(calc, dict) or calc.get("op") not in ("times", "divide"):
        return "bad calculated target"
    for side in ("a", "b"):
        error = _validate_calc_ref(calc.get(side), row.get("id"))
        if error:
            return error
    return None


def _validate_layout(layout: dict, sections: list) -> str | None:
    """Return an error message if the layout payload is invalid, else None."""
    rows = layout.get("rows") if isinstance(layout, dict) else None
    if not isinstance(rows, list):
        return "layout.rows must be a list"
    for row in rows:
        if not isinstance(row, dict):
            return "each row must be an object"
        if row.get("type") != "custom":
            return f"bad row type: {row.get('type')}"
        if row.get("section") not in sections:
            return f"bad section: {row.get('section')}"
        error = _validate_layout_metric(row.get("source", "snowflake"), row.get("metric"))
        if error:
            return error
        if row.get("agg") not in ("sum", "avg"):
            return f"bad agg: {row.get('agg')}"
        title = row.get("title", row["metric"])
        if not isinstance(title, str) or not title.strip() or len(title) > 100:
            return "bad title"
        subtitle = row.get("subtitle", "")
        if not isinstance(subtitle, str) or len(subtitle) > 200:
            return "bad subtitle"
        if not isinstance(row.get("invert", False), bool):
            return "bad invert"
        # Hidden in Settings > Charts: kept, but not shown or loaded.
        if not isinstance(row.get("hidden", False), bool):
            return "bad hidden"
        # Which current (part-elapsed) periods to show: {"m": bool, "w": bool,
        # "d": bool}. Rows saved before the day option hold a single bool,
        # meaning month and week.
        current = row.get("include_current", False)
        if isinstance(current, dict):
            if set(current) - {"m", "w", "d"} or not all(isinstance(v, bool) for v in current.values()):
                return "bad include_current"
        elif not isinstance(current, bool):
            return "bad include_current"
        if not isinstance(row.get("data_labels", True), bool):
            return "bad data_labels"
        if row.get("fmt", "num") not in ("num", "pct", "pct100", "k", "km", "gbp", "gbpkm"):
            return f"bad fmt: {row.get('fmt')}"
        decimals = row.get("decimals", 0)
        if not isinstance(decimals, int) or not (0 <= decimals <= 3):
            return f"bad decimals: {decimals}"
        # Hover box format, set apart from the labels: "" = the figure in full.
        if row.get("hover_fmt", "") not in ("", "num", "pct", "pct100", "k", "km", "gbp", "gbpkm"):
            return f"bad hover_fmt: {row.get('hover_fmt')}"
        hover_decimals = row.get("hover_decimals")
        if hover_decimals is not None and (isinstance(hover_decimals, bool) or not isinstance(hover_decimals, int)
                                           or not 0 <= hover_decimals <= 4):
            return f"bad hover_decimals: {hover_decimals}"
        if row.get("target_mode", "snowflake") not in KPI_TARGET_MODES + ("inputs", "calc"):
            return f"bad target mode: {row.get('target_mode')}"
        # Vertical axis minimum and maximum per period (m/w/d). Missing or null
        # minimum = start at 0; missing or null maximum = worked out from the data.
        for field in ("y_min", "y_max"):
            limits = row.get(field)
            if limits is None:
                continue
            if not isinstance(limits, dict) or set(limits) - {"m", "w", "d"}:
                return f"bad {field}"
            for value in limits.values():
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                    return f"bad {field} value"
        y_min = row.get("y_min") or {}
        for key, value in (row.get("y_max") or {}).items():
            if value is not None and value <= (y_min.get(key) or 0):
                return "axis maximum must be above its minimum"
        # Multiply By: each day of the metric times the same day of a second
        # metric, worked out on the server (_custom_data). Plain charts only.
        multiply_by = row.get("multiply_by")
        if multiply_by is not None:
            if not isinstance(multiply_by, dict) or set(multiply_by) - {"source", "metric"}:
                return "bad multiply_by"
            error = _validate_layout_metric(multiply_by.get("source", "snowflake"), multiply_by.get("metric"))
            if error:
                return error
            if row.get("divide_by") or row.get("metric_plus") or row.get("chart_type") == "stacked":
                return "Multiply By can only be used on a plain chart, without Divided By or added metrics"
        # Ratio chart: the metric divided by a second metric. The page fetches
        # both and divides. Its target is one of: a fixed figure stored on the
        # row itself (ratio_target); "import", per-date figures pasted for the
        # chart (kpi:row:<id>); or "inputs", each metric's own target (the
        # source chosen per metric in input_targets) added and divided the same
        # way as the actuals.
        divide_by = row.get("divide_by")
        if divide_by is not None:
            if not isinstance(divide_by, dict):
                return "bad divide_by"
            error = _validate_layout_metric(divide_by.get("source", "snowflake"), divide_by.get("metric"))
            if error:
                return error
            if row.get("ratio_as", "dec") not in ("dec", "pct"):
                return f"bad ratio_as: {row.get('ratio_as')}"
        # Metrics added to the metric (metric_plus) or to the divide-by metric
        # (divide_plus), e.g. orders / (calls + emails). Each may carry an
        # operator (op: + - * /) joining it to the ones before it; the page
        # works it out on each period's totals (Sum) or averages (Average).
        # Like a ratio chart, the target is on the row.
        for field in ("metric_plus", "divide_plus"):
            plus = row.get(field)
            if plus is None:
                continue
            if not isinstance(plus, list) or len(plus) > 10:
                return f"bad {field}"
            for p in plus:
                if not isinstance(p, dict):
                    return f"bad {field}"
                # How the metric joins the ones before it; missing = plus.
                if p.get("op", "+") not in ("+", "-", "*", "/"):
                    return f"bad {field} operator: {p.get('op')}"
                error = _validate_layout_metric(p.get("source", "snowflake"), p.get("metric"))
                if error:
                    return error
        if row.get("divide_plus") and divide_by is None:
            return "divide_plus needs divide_by"
        if row.get("metric_plus") or row.get("divide_plus"):
            if row.get("weight_by") is not None:
                return "A chart with added metrics cannot be weighted"
        if divide_by is not None or row.get("metric_plus"):
            if row.get("target_mode", "none") not in ("none", "fixed", "import", "inputs"):
                return "a ratio or added-metrics chart's target must be none, fixed, import or inputs"
            ratio_target = row.get("ratio_target")
            if ratio_target is not None and (isinstance(ratio_target, bool) or not isinstance(ratio_target, (int, float))):
                return "bad ratio_target"
            input_targets = row.get("input_targets")
            if input_targets is not None:
                if not isinstance(input_targets, dict) or len(input_targets) > 22:
                    return "bad input_targets"
                for key, mode in input_targets.items():
                    if not isinstance(key, str) or not re.fullmatch(_METRIC_NAME_KEY, key) \
                            or mode not in ("fixed", "import", "snowflake"):
                        return f"bad input target: {key}"
        elif row.get("target_mode") == "inputs" or row.get("input_targets") is not None:
            return "Only a ratio or added-metrics chart can take its target from its inputs"
        error = _validate_target_calc(row)
        if error:
            return error
        error = _validate_row_vehicles(row.get("vehicles"))
        if error:
            return error
        error = _validate_stacked_row(row)
        if error:
            return error
        # Average charts only: a daily_summary count column to weight the
        # week and month figures by (see kpi_custom_data). Checked against the
        # live columns when the chart loads, like the metric itself.
        weight_by = row.get("weight_by")
        if weight_by is not None:
            if row.get("agg") != "avg" or row.get("source", "snowflake") != "snowflake":
                return "Weight by is only for Average charts on daily_summary"
            error = _validate_layout_metric("snowflake", weight_by)
            if error:
                return error
    return None


def _read_kpi_layout_value(key: str):
    # Layout and settings live in the PROD cluster so local edits and prod
    # serve the same dashboard. The deployed dev Lambda cannot reach PROD, so
    # it keeps its own copy in the dev cluster.
    with get_prod_pricer_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS kpi_layout (key VARCHAR(50) PRIMARY KEY, value TEXT)")
            cur.execute("SELECT value FROM kpi_layout WHERE key = %s", (key,))
            row = cur.fetchone()
        conn.commit()
    return json.loads(row[0]) if row else None


def _write_kpi_layout_value(key: str, value) -> None:
    _write_kpi_layout_values({key: value})


def _write_kpi_layout_values(values: dict) -> None:
    """Write several keys in one transaction: all are saved or none are."""
    with get_prod_pricer_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS kpi_layout (key VARCHAR(50) PRIMARY KEY, value TEXT)")
            for key, value in values.items():
                cur.execute(
                    "INSERT INTO kpi_layout (key, value) VALUES (%s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                    (key, json.dumps(value))
                )
        conn.commit()


def _targets_refused(user) -> str | None:
    """Why this user may not change targets, or None. Other users may only
    while Settings > Targets lets every user set them."""
    if is_admin(user):
        return None
    settings = _kpi_settings(_read_kpi_layout_value('settings'))
    return None if settings["targets_for_all"] == 1 else "Only the admin can change KPI targets"


def _kpi_settings(stored: dict | None) -> dict:
    """Stored settings filled out with defaults and clamped to the limits."""
    merged = {**KPI_DEFAULT_SETTINGS, **(stored or {})}
    settings = {name: _clamp_setting(name, merged[name]) for name in KPI_DEFAULT_SETTINGS}
    settings.update({name: _colour_setting(name, (stored or {}).get(name)) for name in KPI_COLOUR_SETTINGS})
    settings["days_total"] = max(settings["days_total"], settings["days_after"] + 1)
    settings["days_window"] = min(settings["days_window"], settings["days_total"])
    return settings


@router.get("/kpi/layout")
def kpi_layout_get(request: Request, user: str = Depends(require_page_api('kpi'))):
    try:
        layout = _read_kpi_layout_value('layout') or KPI_DEFAULT_LAYOUT
        settings = _kpi_settings(_read_kpi_layout_value('settings'))
        notes = _read_kpi_layout_value('notes') or []
        sections = _kpi_sections(layout)
        themes = _read_kpi_layout_value('themes') or KPI_DEFAULT_THEMES
        metric_names = _read_kpi_layout_value('metric_names')
        if metric_names is None:
            metric_names = _seed_metric_names(layout)
        metric_formats = _read_kpi_layout_value('metric_formats') or {}
    except Exception as e:
        # Report it rather than serving an empty layout: the page saves the
        # whole layout in one go, so a save made on an empty one would wipe
        # every saved chart.
        message = getattr(e, "detail", None) or str(e)
        logger.error(f"KPI layout load failed: {message}")
        return {"status": "error", "message": message}
    # The hard-coded Sessions and Apex Orders rows were removed: every chart is
    # now created on the page. Drop them from a layout saved before that.
    layout = {**layout, "rows": [r for r in layout.get("rows", []) if r.get("type") == "custom"]}
    try:
        raw = _read_kpi_layout_value(_AT_PERIODS_KEY) or []
        at_periods = _at_allowance_periods()
        at_ppc_periods = _at_ppc_periods(raw)
    except Exception as e:
        logger.warning("Autotrader settings read failed: %s", e)
        at_periods, at_ppc_periods = [], []
    return {"status": "ok", "layout": layout, "sections": sections, "settings": settings,
            "setting_limits": KPI_SETTING_LIMITS, "notes": notes, "themes": themes,
            "metric_names": metric_names, "metric_formats": metric_formats, "at_periods": at_periods, "at_ppc_periods": at_ppc_periods,
            "hidden_sections": [s for s in (_read_kpi_layout_value('hidden_sections') or []) if s in sections],
            "collapsed_rows": _user_row_list(_COLLAPSED_KEY, user),
            "rolling_rows": _user_row_list(_ROLLING_KEY, user),
            "my_charts": _user_row_list(_MY_CHARTS_KEY, user)}


# ── Per-user chart row lists ─────────────────────────────────────────────────
# Each user's own collapsed rows, rows showing the 7-day average, and My
# Charts, stored as {user: [row ids]} so they follow the person to any
# browser. The section row order stays the admin's; My Charts keeps the order
# the user set.
_COLLAPSED_KEY = 'collapsed_rows'
_ROLLING_KEY = 'rolling_rows'
_MY_CHARTS_KEY = 'my_charts'


def _user_row_list(key: str, user: str) -> list:
    try:
        return (_read_kpi_layout_value(key) or {}).get(user, [])
    except Exception as e:
        logger.warning("%s read failed: %s", key, e)
        return []


def _save_user_row_list(key: str, user: str, payload: dict, keep_order: bool) -> dict:
    rows = (payload or {}).get("rows")
    if not isinstance(rows, list) or len(rows) > 1000 or not all(isinstance(r, str) and 0 < len(r) <= 40 for r in rows):
        return {"status": "error", "message": "bad rows"}
    try:
        stored = _read_kpi_layout_value(key) or {}
        stored[user] = list(dict.fromkeys(rows)) if keep_order else sorted(set(rows))
        _write_kpi_layout_value(key, stored)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"KPI {key} save failed: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/kpi/collapsed-rows")
def kpi_collapsed_rows_save(request: Request, payload: dict, user: str = Depends(require_page_api('kpi'))):
    return _save_user_row_list(_COLLAPSED_KEY, user, payload, keep_order=False)


@router.post("/kpi/rolling-rows")
def kpi_rolling_rows_save(request: Request, payload: dict, user: str = Depends(require_page_api('kpi'))):
    return _save_user_row_list(_ROLLING_KEY, user, payload, keep_order=False)


@router.post("/kpi/my-charts")
def kpi_my_charts_save(request: Request, payload: dict, user: str = Depends(require_page_api('kpi'))):
    return _save_user_row_list(_MY_CHARTS_KEY, user, payload, keep_order=True)


# ── Chart notes ──────────────────────────────────────────────────────────────
# Dated notes marking business changes (a new finance deal, a change to which
# cars go on Autotrader). Each draws a dotted line on the day chart and on the
# week and month containing that date. row_id None = every chart, otherwise
# only that chart row. The whole list is saved in one go, like the layout.
_KPI_NOTES_MAX = 500


def _validate_notes(notes) -> str | None:
    if not isinstance(notes, list):
        return "notes must be a list"
    if len(notes) > _KPI_NOTES_MAX:
        return f"At most {_KPI_NOTES_MAX} notes"
    seen = set()
    for note in notes:
        if not isinstance(note, dict):
            return "bad note"
        note_id = note.get("id")
        if not isinstance(note_id, str) or not note_id or len(note_id) > 40 or note_id in seen:
            return "bad note id"
        seen.add(note_id)
        try:
            datetime.date.fromisoformat(note.get("date"))
        except (TypeError, ValueError):
            return "Note date must be YYYY-MM-DD"
        title = note.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 100:
            return "Note title must be 1 to 100 characters"
        text = note.get("text", "")
        if not isinstance(text, str) or len(text) > 2000:
            return "Note text must be 2000 characters or fewer"
        row_id = note.get("row_id")
        if row_id is not None and (not isinstance(row_id, str) or len(row_id) > 40):
            return "bad note row_id"
        if set(note) - {"id", "date", "title", "text", "row_id"}:
            return "Unknown note field"
    return None


@router.post("/kpi/notes")
def kpi_notes_save(request: Request, payload: dict, user: str = Depends(require_page_api('kpi'))):
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change chart notes"}
    notes = (payload or {}).get("notes")
    err = _validate_notes(notes)
    if err:
        return {"status": "error", "message": err}
    try:
        _write_kpi_layout_value('notes', notes)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"KPI notes save failed: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/kpi/layout")
def kpi_layout_save(request: Request, payload: dict, user: str = Depends(require_page_api('kpi'))):
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the KPI charts"}
    layout = (payload or {}).get("layout")
    try:
        sections = _kpi_sections(_read_kpi_layout_value('layout') or KPI_DEFAULT_LAYOUT)
        err = _validate_layout(layout or {}, sections)
        if err:
            return {"status": "error", "message": err}
        _write_kpi_layout_value('layout', layout)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"KPI layout save failed: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/kpi/row-target")
def kpi_row_target_save(request: Request, payload: dict, user: str = Depends(require_page_api('kpi'))):
    """One chart's target, from the Targets popup, for users who cannot save
    the whole layout. Body: {"row_id", "target_mode": "fixed"|"import",
    "value": number|null}. Only the target fields of that one chart change.
    value is the chart's own fixed figure on a ratio, added-metrics or stacked
    chart; a plain chart's fixed figure belongs to the metric and is saved
    through /kpi/metric-target/fixed instead."""
    refused = _targets_refused(user)
    if refused:
        return {"status": "error", "message": refused}
    payload = payload or {}
    row_id, mode, value = payload.get("row_id"), payload.get("target_mode"), payload.get("value")
    if mode not in ("fixed", "import"):
        return {"status": "error", "message": "The target must be a fixed figure or per date"}
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
        return {"status": "error", "message": "The fixed target must be a number of 0 or more"}
    try:
        stored = _read_kpi_layout_value('layout') or KPI_DEFAULT_LAYOUT
        layout = json.loads(json.dumps(stored))
        row = next((r for r in layout.get("rows", []) if r.get("id") == row_id), None)
        if row is None:
            return {"status": "error", "message": "That chart no longer exists - reload the page"}
        # The chart's own fixed figure is saved whichever target it draws, so
        # the Fixed Figure box keeps what was typed while Per Date is chosen.
        if row.get("chart_type") == "stacked" or row.get("divide_by") is not None or row.get("metric_plus"):
            if mode == "fixed" and value is None:
                return {"status": "error", "message": "Enter a fixed figure"}
            row["stack_target" if row.get("chart_type") == "stacked" else "ratio_target"] = value
        row["target_mode"] = mode
        row.pop("input_targets", None)
        row.pop("target_calc", None)
        err = _validate_layout(layout, _kpi_sections(stored))
        if err:
            return {"status": "error", "message": err}
        _write_kpi_layout_value('layout', layout)
        return {"status": "ok", "row": row}
    except Exception as e:
        logger.error(f"KPI row target save failed: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/kpi/client-error")
def kpi_client_error(request: Request, payload: dict, user: str = Depends(require_page_api('kpi'))):
    """Logs an error the KPI page hit in the browser, which would otherwise
    only show in that user's browser console."""
    message = str((payload or {}).get("message", ""))[:1000]
    stack = str((payload or {}).get("stack", ""))[:4000]
    logger.error(f"KPI page error for {user}: {message}\n{stack}")
    return {"status": "ok"}


@router.get("/kpi/at-allowance")
def kpi_at_allowance(request: Request, user: str = Depends(require_page_api('kpi'))):
    """Car/Van advert allowance, asked for live from the Autotrader advertisers
    API on every page load and Load Data so a contract change shows straight
    away, plus the published / capped / unpublished counts from the latest day
    in daily_summary (the first Autotrader pull of that morning)."""
    import requests
    from list_processor.autotrader_client import AutoTraderClient
    result = {"status": "ok"}
    try:
        result["capacity"] = AutoTraderClient().get_car_van_advert_allowance(requests.Session())
    except Exception as e:
        logger.error(f"Autotrader allowance request failed: {e}")
        result["capacity_error"] = str(e)
    if isinstance(result.get("capacity"), int):
        _record_at_allowance(result["capacity"])
    try:
        with SnowflakeClient() as sf:
            rows, _ = sf.execute("""
                SELECT date, stock_advertised_on_at, stock_capped_on_at,
                    stock_selectively_not_published_on_at
                FROM PROD_CARSUPERMARKET_DB.PRODUCT_SLT_KPIS.DAILY_SUMMARY
                WHERE date <= CURRENT_DATE AND stock_advertised_on_at IS NOT NULL
                ORDER BY date DESC
                LIMIT 1
            """)
        if rows:
            day, published, capped, unpublished = rows[0]
            result["counts"] = {"date": str(day), "published": published,
                                "capped": capped, "unpublished": unpublished}
    except Exception as e:
        logger.error(f"Autotrader advert counts query failed: {e}")
        result["counts_error"] = str(e)
    return result


# ── Autotrader allowance and costs (Settings > Autotrader) ───────────────────
# The dated rows in Settings are what the charts read. Each applies from its
# From date until its To date, or, with no To, until the next row's From.
# A day no row covers has no figures (unknown, not zero). Rows come from:
#   source 'invoice'  Apply Invoices To Rows: one row for every change in the
#                     imported monthly PDFs (see utils/at_invoices.py), with
#                     From and To. What was actually charged.
#   source 'email'    an adjustment email, read or typed in. Estimated: used
#                     after the last invoice, until that month's invoice is
#                     imported and applied (Apply replaces email rows for the
#                     days the invoices cover).
# Row fields. Costs on email rows are per calendar month, as in Autotrader's
# emails. Costs on invoice rows are the invoiced cost for the row's own days
# (From to To, which invoice rows always have):
#   from           YYYY-MM-DD
#   to             YYYY-MM-DD, or None to run until the next row
#   base           contracted advert slots
#   flex_pct       Bonus Advert Allowance as a % of base; flex slots = base x
#                  flex %, rounded to a whole slot. Free
#   tactical       tactical advert slots
#   me_cost        Market Extension package (£)
#   finance_cost   Finance Calculator (£)
#   tactical_cost  tactical adverts (£)
#   connect_cost   Connect Retail Strategy (£)
#   source         'invoice' or 'email'
# An email row's month cost becomes a day's cost by dividing by the days in
# that month; an invoice row's cost by dividing by the row's days. Nothing is
# rounded in storage or in the chart figures.
# The cost per PPC advert view changes separately, so it has its own dated
# list under 'at_ppc_cost_periods': {from, ppc_view_cost}.
# The API total allowance is still saved once a day under
# 'at_allowance_history' (/kpi/at-allowance), shown in Settings as a check.
_AT_PERIODS_KEY = "at_allowance_periods"
_AT_PPC_KEY = "at_ppc_cost_periods"
_AT_HISTORY_KEY = "at_allowance_history"
_AT_PERIOD_FIELDS = ("from", "base", "flex_pct", "tactical", "me_cost", "finance_cost",
                     "tactical_cost", "connect_cost", "to", "source")
_AT_ROW_SOURCES = ("invoice", "email")
_AT_PPC_FIELDS = ("from", "ppc_view_cost")

# The chart metrics worked out from those settings: name -> description.
# Fixed in code because each is a calculation, not a column. PPC is never in
# the costs here: it has its own metric (at_cost_per_ppc_click).
_AT_METRICS = {
    "at_base_slots": "Base (contracted) advert slots, from the Settings > Autotrader Costs rows (invoices, then adjustment emails)",
    "at_flex_slots": "Flex slots (Bonus Advert Allowance): base x 10%, rounded to a whole slot. Free",
    "at_allowance_incl_flex": "Allowance including flex: base + flex",
    "at_tactical_slots": "Tactical advert slots",
    "at_total_allowance": "Total slots: base + flex + tactical",
    "at_charged_slots": "Charged slots: base + tactical (flex is free)",
    "at_cost_base": "Market Extension package cost per day (£). Sum for a week or month",
    "at_cost_finance": "Finance Calculator cost per day (£). Adds cost, not slots. Sum for a week or month",
    "at_cost_tactical": "Tactical adverts cost per day (£). Sum for a week or month",
    "at_cost_connect": "Connect Retail Strategy cost per day (£). Charged by the day, not by slot",
    "at_cost_slots": "The same figure as at_cost_total (Connect is now shared over the slots too)",
    "at_cost_total": ("Total Autotrader cost per day (£): Market Extension + Finance Calculator + tactical + "
                      "Connect. Excludes PPC"),
    "at_cost_per_slot_day": ("Cost per slot per day: total cost (Connect included) / total slots (flex included). "
                             "For a week or month, use at_cost_total (Sum) divided by at_total_allowance (Sum) "
                             "instead of averaging this"),
    "at_cost_per_slot_month": "Cost per slot per day x the days in that month",
    "at_estimated": "1 on days costed from an adjustment email row (no invoice yet), 0 on days from an invoice row",
    "at_cost_per_ppc_click": ("Cost per PPC click (£) for each date, from the PPC Cost rows: each invoiced "
                              "month's PPC cost / its clicks, then the latest price after the last invoice"),
    "at_cost_per_ppc_view": ("The same figure as at_cost_per_ppc_click, under its old name (kept so charts "
                             "made with it still work)"),
}
# Still work in saved charts, but not offered in the metric lists.
_AT_HIDDEN_METRICS = {"at_cost_per_ppc_view", "at_cost_slots", "at_cost_connect"}
# Costs (and the estimated-day count) add up over a week or month; the rest are averages.
_AT_SUM_METRICS = {"at_cost_base", "at_cost_finance", "at_cost_tactical", "at_cost_connect",
                   "at_cost_slots", "at_cost_total", "at_estimated"}


def _record_at_allowance(capacity: int) -> None:
    """Save today's API total allowance, if it is not already saved."""
    try:
        today = datetime.date.today().isoformat()
        history = _read_kpi_layout_value(_AT_HISTORY_KEY) or {}
        if history.get(today) != capacity:
            history[today] = capacity
            _write_kpi_layout_value(_AT_HISTORY_KEY, history)
    except Exception as e:
        logger.warning("Saving the Autotrader allowance history failed: %s", e)


def _validate_at_rows(rows, fields: tuple, optional: tuple, what: str, max_rows: int = 200) -> str | None:
    if not isinstance(rows, list) or len(rows) > max_rows:
        return f"{what}: at most {max_rows} rows"
    seen = set()
    for p in rows:
        if not isinstance(p, dict) or set(p) - set(fields):
            return f"{what}: bad row"
        try:
            datetime.date.fromisoformat(p.get("from"))
        except (TypeError, ValueError):
            return f"{what}: every row needs a From date"
        if p["from"] in seen:
            return f"{what}: two rows start on {p['from']}"
        seen.add(p["from"])
        for field in fields[1:]:
            v = p.get(field)
            if field == "to":
                if v is None:
                    continue
                try:
                    if datetime.date.fromisoformat(v) < datetime.date.fromisoformat(p["from"]):
                        return f"{what} {p['from']}: the To date is before the From date"
                except (TypeError, ValueError):
                    return f"{what} {p['from']}: bad To date"
                continue
            if field == "source":
                if v not in _AT_ROW_SOURCES:
                    return f"{what} {p['from']}: bad source"
                continue
            if v is None and field in optional:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                return f"{what} {p['from']}: every figure must be a number of 0 or more"
    return None


def _at_ppc_periods(allowance_rows: list) -> list:
    """The PPC cost rows. Before they had their own list the cost was on the
    allowance rows, so those are used until the list is first saved."""
    rows = _read_kpi_layout_value(_AT_PPC_KEY)
    if rows is not None:
        return rows
    return [{"from": p["from"], "ppc_view_cost": p["ppc_view_cost"]}
            for p in allowance_rows if p.get("ppc_view_cost") is not None]


def _at_row_from_old(p: dict) -> dict:
    """A row saved before the costs were split by product: {from, base,
    flex_pct, total, slot_cost}, where base and tactical slots were each
    charged slot_cost a month. Turned into the same charge in the new fields,
    so the old figures still apply until the row is replaced."""
    base = p.get("base") or 0
    incl_flex = base + at_invoices.flex_slots(base, p.get("flex_pct") or 0)
    tactical = max(0, p["total"] - incl_flex) if p.get("total") is not None else 0
    slot_cost = p.get("slot_cost") or 0
    return {"from": p["from"], "base": base, "flex_pct": p.get("flex_pct") or 0, "tactical": tactical,
            "me_cost": slot_cost * base, "finance_cost": 0,
            "tactical_cost": slot_cost * tactical, "connect_cost": 0}


def _at_allowance_periods() -> list:
    """The Settings rows, sorted by date, without the PPC cost they used to
    carry. Rows saved before To and Source existed run until the next row
    and count as email rows."""
    rows = []
    for p in (_read_kpi_layout_value(_AT_PERIODS_KEY) or []):
        if "me_cost" not in p and "slot_cost" in p:
            p = _at_row_from_old(p)
        row = {k: p.get(k) for k in _AT_PERIOD_FIELDS}
        row["source"] = row["source"] or "email"
        rows.append(row)
    return sorted(rows, key=lambda p: p["from"])


@router.post("/kpi/at-periods")
def kpi_at_periods_save(request: Request, payload: dict, user: str = Depends(require_page_api('kpi'))):
    """Save the dated Autotrader allowance rows and PPC cost rows, together.
    Admin only."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the Autotrader settings"}
    periods = (payload or {}).get("periods")
    ppc = (payload or {}).get("ppc_periods")
    # Up to 1,000: Apply Invoices adds a row for every change, several a month.
    err = (_validate_at_rows(periods, _AT_PERIOD_FIELDS, (), "Autotrader row", max_rows=1000)
           or _validate_at_rows(ppc, _AT_PPC_FIELDS, (), "PPC Cost"))
    if not err:
        no_to = next((p for p in periods if p.get("source") == "invoice" and not p.get("to")), None)
        if no_to:
            err = f"Autotrader row {no_to['from']}: invoice rows need a To date (their costs are for From to To)"
    if err:
        return {"status": "error", "message": err}
    periods = sorted(periods, key=lambda p: p["from"])
    ppc = sorted(ppc, key=lambda p: p["from"])
    try:
        _write_kpi_layout_values({_AT_PERIODS_KEY: periods, _AT_PPC_KEY: ppc})
    except Exception as e:
        logger.error(f"Autotrader settings save failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "periods": periods, "ppc_periods": ppc}


# ── Autotrader invoices (Settings > Autotrader > Invoices) ───────────────────
# The page sends each PDF as base64 text in JSON, as the Supplier Stock
# Evaluator upload does: a raw PDF body through API Gateway arrives broken
# unless the API is set up for binary types. Invoices are about 45 KB.
_AT_INVOICE_MAX_BYTES = 5 * 1024 * 1024


def _at_invoice_summary() -> dict:
    with get_prod_pricer_connection() as conn:
        invoices = at_invoices.list_invoices(conn)
        cover = at_invoices.coverage(at_invoices.read_lines(conn))
    return {"status": "ok", "invoices": invoices, "coverage": cover}


@router.get("/kpi/at-invoices")
def kpi_at_invoices(request: Request, user: str = Depends(require_page_api('kpi'))):
    """The imported invoices, oldest first, and the days they cover."""
    try:
        return _at_invoice_summary()
    except Exception as e:
        logger.error(f"Autotrader invoice list failed: {e}")
        return {"status": "error", "message": str(e)}


@router.get("/kpi/at-invoices/rows")
def kpi_at_invoice_rows(request: Request, user: str = Depends(require_page_api('kpi'))):
    """The Settings rows the imported invoices give: one per change, with
    From and To (see at_invoices.rows_from_lines). Nothing is saved here: the
    page puts them in its rows, then Save saves them."""
    try:
        with get_prod_pricer_connection() as conn:
            lines = at_invoices.read_lines(conn)
            ppc_rows, ppc_missing = at_invoices.ppc_rows(conn)
        return {"status": "ok", "rows": at_invoices.rows_from_lines(lines),
                "coverage": at_invoices.coverage(lines),
                "ppc_rows": ppc_rows, "ppc_missing": ppc_missing}
    except Exception as e:
        logger.error(f"Autotrader invoice rows failed: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/kpi/at-invoices/import")
def kpi_at_invoice_import(request: Request, payload: dict, user: str = Depends(require_page_api('kpi'))):
    """Import one invoice PDF: {filename, data (base64)}. Admin only. The PDF
    is kept in S3; its lines replace any already stored for that invoice."""
    import base64
    import binascii
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can import invoices"}
    filename = str((payload or {}).get("filename") or "")[:200]
    try:
        pdf = base64.b64decode(str((payload or {}).get("data") or ""), validate=True)
    except (binascii.Error, ValueError):
        return {"status": "error", "message": f"{filename}: the file could not be read"}
    if not pdf or len(pdf) > _AT_INVOICE_MAX_BYTES:
        return {"status": "error", "message": f"{filename}: empty, or larger than 5 MB"}
    try:
        invoice = at_invoices.parse_invoice(pdf)
    except Exception as e:
        # Anything raised here would otherwise reach the app's HTML error
        # page, which the Settings popup cannot read.
        if not isinstance(e, ValueError):
            logger.error(f"Autotrader invoice read failed for {filename}: {e}")
        return {"status": "error", "message": f"{filename}: {e}"}

    # Keep the original PDF. Not saving it is reported, but does not stop the
    # import: the figures are what the charts need.
    s3_key = f"applications/pricing/uk-vehicle-list-evaluator/{_S3_ENV}/autotrader_invoices/{invoice['invoice_number']}.pdf"
    s3_error = None
    try:
        import boto3
        boto3.client("s3", region_name=s3_bucket_region).put_object(
            Bucket=s3_bucketname, Key=s3_key, Body=pdf, ContentType="application/pdf")
    except Exception as e:
        logger.warning(f"Autotrader invoice PDF not saved to S3: {e}")
        s3_key, s3_error = None, str(e)
    try:
        with get_prod_pricer_connection() as conn:
            replaced = at_invoices.save_invoice(conn, invoice, s3_key, user, _uk_now())
    except ValueError as e:
        return {"status": "error", "message": f"{filename}: {e}"}
    except Exception as e:
        logger.error(f"Autotrader invoice save failed: {e}")
        return {"status": "error", "message": f"{filename}: {e}"}
    lines_total = sum(l["charge"] for l in invoice["lines"])
    return {"status": "ok", "invoice_number": invoice["invoice_number"], "replaced": replaced,
            "lines": len(invoice["lines"]), "lines_total": float(lines_total), "s3_error": s3_error,
            "ppc": float(invoice["ppc"]), "ppc_clicks": invoice["ppc_clicks"]}


@router.post("/kpi/at-invoices/delete")
def kpi_at_invoice_delete(request: Request, payload: dict, user: str = Depends(require_page_api('kpi'))):
    """Delete one imported invoice and its lines: {invoice_number}. Admin only.
    The PDF stays in S3."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can delete invoices"}
    number = str((payload or {}).get("invoice_number") or "")
    try:
        with get_prod_pricer_connection() as conn:
            if not at_invoices.delete_invoice(conn, number):
                return {"status": "error", "message": f"Invoice {number} not found"}
        return _at_invoice_summary()
    except Exception as e:
        logger.error(f"Autotrader invoice delete failed: {e}")
        return {"status": "error", "message": str(e)}


def _at_setting_values():
    """A function (date, metric) -> value for the _AT_METRICS, from the
    Settings rows (invoice rows and adjustment email rows, see the comment
    above _AT_PERIODS_KEY). Reads them once, so build it once per load."""
    import bisect
    raw = _read_kpi_layout_value(_AT_PERIODS_KEY) or []
    periods = _at_allowance_periods()
    starts = [p["from"] for p in periods]
    ppc = sorted(_at_ppc_periods(raw), key=lambda p: p["from"])
    ppc_starts = [p["from"] for p in ppc]

    def day_figures(d: datetime.date):
        """{base, flex, tactical, cost_base, cost_finance, cost_tactical,
        cost_connect, estimated} for one day, or None when no row covers it."""
        iso = d.isoformat()
        i = bisect.bisect_right(starts, iso) - 1
        if i < 0:
            return None      # before the first row
        p = periods[i]
        if p.get("to") and iso > p["to"]:
            return None      # after this row's To and before the next row: unknown, not zero
        if p["source"] == "invoice" and p.get("to"):
            days = (datetime.date.fromisoformat(p["to"]) - datetime.date.fromisoformat(p["from"])).days + 1
        else:
            days = calendar.monthrange(d.year, d.month)[1]
        return {"base": p["base"], "flex": at_invoices.flex_slots(p["base"], p["flex_pct"]),
                "tactical": p["tactical"], "cost_base": p["me_cost"] / days,
                "cost_finance": p["finance_cost"] / days, "cost_tactical": p["tactical_cost"] / days,
                "cost_connect": p["connect_cost"] / days,
                "estimated": 0 if p["source"] == "invoice" else 1}

    def value(d: datetime.date, metric: str):
        if metric in ("at_cost_per_ppc_click", "at_cost_per_ppc_view"):
            k = bisect.bisect_right(ppc_starts, d.isoformat()) - 1
            return ppc[k]["ppc_view_cost"] if k >= 0 else None
        f = day_figures(d)
        if f is None:
            return None
        total_slots = f["base"] + f["flex"] + f["tactical"]
        # Connect is shared over the slots like every other product.
        cost_slots = f["cost_base"] + f["cost_finance"] + f["cost_tactical"] + f["cost_connect"]
        per_slot_day = cost_slots / total_slots if total_slots else None
        return {
            "at_base_slots": f["base"],
            "at_flex_slots": f["flex"],
            "at_allowance_incl_flex": f["base"] + f["flex"],
            "at_tactical_slots": f["tactical"],
            "at_total_allowance": total_slots,
            "at_charged_slots": f["base"] + f["tactical"],
            "at_cost_base": f["cost_base"],
            "at_cost_finance": f["cost_finance"],
            "at_cost_tactical": f["cost_tactical"],
            "at_cost_connect": f["cost_connect"],
            "at_cost_slots": cost_slots,
            "at_cost_total": cost_slots,
            "at_cost_per_slot_day": per_slot_day,
            "at_cost_per_slot_month": (per_slot_day * calendar.monthrange(d.year, d.month)[1]
                                       if per_slot_day is not None else None),
            "at_estimated": f["estimated"],
        }.get(metric)

    return value


def _kpi_sections(layout: dict) -> list:
    """The saved section list, plus any section a chart is in that the list
    is missing (e.g. no list saved yet, so the defaults are used). Without
    this the page has nowhere to put that chart and fails to load."""
    sections = list(_read_kpi_layout_value('sections') or KPI_DEFAULT_SECTIONS)
    for row in layout.get("rows", []):
        name = row.get("section")
        if isinstance(name, str) and name and name not in sections:
            sections.append(name)
    return sections


def _section_slug(name: str) -> str:
    """Matches sectionDomId in kpi.html, so two names never share a page element."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


@router.post("/kpi/sections")
def kpi_sections_save(request: Request, payload: dict, user: str = Depends(require_page_api('kpi'))):
    """Save the section list in order. Each item is {name, from}: `from` is the
    section's name before this edit (null for a new one), so a rename carries
    its charts across. A section left out is removed, which is refused while
    it still holds charts. Admin only."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the KPI sections"}
    items = (payload or {}).get("sections")
    if not isinstance(items, list) or not items:
        return {"status": "error", "message": "At least one section is needed"}
    if len(items) > 50:
        return {"status": "error", "message": "At most 50 sections"}
    # hidden: sections hidden in Settings > Sections (kept, with their charts,
    # but not shown as tabs and their charts not loaded).
    names, renames, slugs, hidden = [], {}, set(), []
    for item in items:
        if not isinstance(item, dict):
            return {"status": "error", "message": "bad section"}
        name = item.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 60:
            return {"status": "error", "message": "Section names must be 1 to 60 characters"}
        name = name.strip()
        slug = _section_slug(name)
        if not slug:
            return {"status": "error", "message": f"'{name}' needs at least one letter or number"}
        if slug in slugs:
            return {"status": "error", "message": f"Two sections are named too alike: '{name}'"}
        slugs.add(slug)
        names.append(name)
        if item.get("hidden") is True:
            hidden.append(name)
        old = item.get("from")
        if old is not None:
            if not isinstance(old, str):
                return {"status": "error", "message": "bad section"}
            renames[old] = name
    try:
        layout = _read_kpi_layout_value('layout') or KPI_DEFAULT_LAYOUT
        current = _kpi_sections(layout)
        rows = layout.get("rows", [])
        for old in renames:
            if old not in current:
                return {"status": "error", "message": f"Section '{old}' no longer exists - reload the page"}
        removed = [s for s in current if s not in renames]
        in_use = [s for s in removed if any(r.get("section") == s for r in rows)]
        if in_use:
            return {"status": "error",
                    "message": f"Move or remove the charts in {', '.join(in_use)} before removing it"}
        new_rows = [{**r, "section": renames.get(r.get("section"), r.get("section"))} for r in rows]
        new_layout = {**layout, "rows": new_rows}
        _write_kpi_layout_values({'layout': new_layout, 'sections': names, 'hidden_sections': hidden})
    except Exception as e:
        logger.error(f"KPI sections save failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "sections": names, "layout": new_layout, "hidden_sections": hidden}


@router.post("/kpi/settings")
def kpi_settings_save(request: Request, payload: dict, user: str = Depends(require_page_api('kpi'))):
    """Chart periods, data labels, lines and sizes, shared by everyone. Admin only."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the KPI settings"}
    raw = (payload or {}).get("settings") or {}
    for name, (low, high) in KPI_SETTING_LIMITS.items():
        try:
            value = _setting_number(name, raw.get(name))
        except (TypeError, ValueError):
            return {"status": "error", "message": f"{name} must be a number"}
        if not low <= value <= high:
            return {"status": "error", "message": f"{name} must be between {low} and {high}"}
    for name in KPI_COLOUR_SETTINGS:
        if not isinstance(raw.get(name), str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", raw[name]):
            return {"status": "error", "message": f"{name} must be a colour like #0d3d6e"}
    settings = _kpi_settings(raw)
    if int(raw["days_window"]) > int(raw["days_total"]):
        return {"status": "error", "message": "Days shown cannot be more than the days available"}
    if int(raw["days_after"]) >= int(raw["days_total"]):
        return {"status": "error", "message": "Days available must be more than the days after today"}
    try:
        _write_kpi_layout_value('settings', settings)
    except Exception as e:
        logger.error(f"KPI settings save failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "settings": settings}


# ── View Vehicles ────────────────────────────────────────────────────────────
# Clicking a point on a chart opens a popup; its View Vehicles button lists the
# vehicles behind that day, week or month. Nothing at vehicle level is loaded
# with the charts — each click runs one query for that date range only.
#
# What is set where, all on the page:
#   Settings popup  - for each vehicle table, the columns to show, in order,
#                     with headings, and the grey-out rules ('vehicle_view' in
#                     kpi_layout, a list keyed by the real table name)
#   Edit Chart      - the chart's table, date column, filters and whether the
#                     list is a date range or a single-day snapshot
#                     (row["vehicles"])
#
# Each chart picks its own table (shown by its real Snowflake name); the columns
# for that table are configured once in Settings and reused by every chart that
# uses it. Table and column names are checked against INFORMATION_SCHEMA before
# any query runs, so they can be put into the SQL; filter values are always
# bound as parameters.

_VEHICLE_DB = "PROD_CARSUPERMARKET_DB"
_VEHICLE_TABLE_DEFAULT = "PRODUCT_SALES_PERFORMANCE.SALES_PERFORMANCE"
_VEHICLE_ROW_LIMIT = 5000
_VEHICLE_FILTERS_MAX = 20
_IDENTIFIER = r"[A-Z][A-Z0-9_$]*"
_VEHICLE_DATE_TYPES = {"DATE", "TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ", "TIMESTAMP"}
# Operators a chart filter can use: those taking no value, and those taking one.
_FILTER_OPS_NO_VALUE = {"is true", "is false", "is empty", "is not empty"}
_FILTER_OPS_VALUE = {"=", "!=", ">", ">=", "<", "<=", "in", "not in", "contains"}
# How a column's values show in the View Vehicles list (set per column in Settings).
_VEHICLE_FORMATS = {"number", "gbp", "pct", "text", "date", "datetime", "link"}


def _vehicle_view_config(stored) -> dict:
    """One table's View Vehicles setting with defaults filled in. grey_rules:
    [{column, op, value}] like a chart filter; a row matching ANY of them shows
    in light grey (cancelled deals by default), checked whether or not the
    column is shown. A setting saved with the older single grey_column /
    grey_value pair becomes a one-rule list."""
    stored = stored if isinstance(stored, dict) else {}
    if "grey_rules" in stored:
        grey_rules = stored["grey_rules"] or []
    else:
        column = stored.get("grey_column", "INVOICE_STATUS")
        grey_rules = [{"column": column, "op": "=", "value": stored.get("grey_value", "STATUS_CANCELLED")}] if column else []
    return {"table": stored.get("table") or _VEHICLE_TABLE_DEFAULT,
            "columns": stored.get("columns") or [],
            "grey_rules": grey_rules,
            "filters": stored.get("filters") or []}


def _vehicle_views(stored) -> list:
    """Every saved per-table View Vehicles config, as a list of
    {table, columns, grey_rules}. The older single-view dict (one global table,
    shared by every chart) is migrated to a one-element list. Later entries for
    the same table win, so a save that re-adds a table replaces its config."""
    if isinstance(stored, dict):
        stored = [stored]
    if not isinstance(stored, list):
        stored = []
    by_table = {}
    for entry in stored:
        if not isinstance(entry, dict):
            continue
        view = _vehicle_view_config(entry)
        by_table[view["table"]] = view
    return list(by_table.values())


def _vehicle_view_for_table(stored, table: str) -> dict:
    """The saved config for one table, or an empty default if none is saved yet
    (so a newly picked table lists nothing until its columns are set up)."""
    for view in _vehicle_views(stored):
        if view["table"] == table:
            return view
    return {"table": table, "columns": [], "grey_rules": [], "filters": []}


def _vehicle_tables() -> list:
    """Every table and view in the PRODUCT_ schemas, as SCHEMA.TABLE."""
    # Snowflake's LIKE has no default escape character, so the escape must be
    # named: without ESCAPE, 'PRODUCT\_%' looks for a literal backslash and
    # matches nothing, leaving the picker with only the table already chosen.
    with SnowflakeClient() as sf:
        rows, _ = sf.execute(f"""
            SELECT table_schema, table_name
            FROM {_VEHICLE_DB}.INFORMATION_SCHEMA.TABLES
            WHERE table_schema LIKE %s ESCAPE '!'
            ORDER BY table_schema, table_name
        """, ["PRODUCT!_%"])
    return [f"{r[0]}.{r[1]}" for r in rows]


# Column lists shared by every Lambda container through Aurora. Each container
# runs one request at a time and keeps its own memory, so with several users a
# per-process cache misses on most requests, and the INFORMATION_SCHEMA lookup
# behind it takes 1-2.5 s (Logs page, 2026-10-20). Load Data clears it.
_SHARED_COLUMNS_TTL = 600  # seconds
_shared_columns_ready = False


def _shared_columns_table(conn) -> None:
    global _shared_columns_ready
    if _shared_columns_ready:
        return
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS snowflake_column_cache ("
                    " table_name VARCHAR(200) PRIMARY KEY,"
                    " columns JSONB NOT NULL,"
                    " read_at TIMESTAMPTZ NOT NULL DEFAULT NOW())")
    conn.commit()
    _shared_columns_ready = True


def _read_shared_columns(table: str) -> list | None:
    try:
        with get_database_connection() as conn:
            _shared_columns_table(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT columns FROM snowflake_column_cache WHERE table_name = %s"
                            " AND read_at > NOW() - make_interval(secs => %s)",
                            (table, _SHARED_COLUMNS_TTL))
                row = cur.fetchone()
        return [tuple(c) for c in row[0]] if row else None
    except Exception as e:
        logger.warning("Shared column cache read failed for %s: %s", table, e)
        return None


def _write_shared_columns(table: str, columns: list) -> None:
    try:
        with get_database_connection() as conn:
            _shared_columns_table(conn)
            with conn.cursor() as cur:
                cur.execute("INSERT INTO snowflake_column_cache (table_name, columns, read_at)"
                            " VALUES (%s, %s, NOW()) ON CONFLICT (table_name)"
                            " DO UPDATE SET columns = EXCLUDED.columns, read_at = NOW()",
                            (table, json.dumps(columns)))
            conn.commit()
    except Exception as e:
        logger.warning("Shared column cache write failed for %s: %s", table, e)


def _clear_shared_columns() -> None:
    try:
        with get_database_connection() as conn:
            _shared_columns_table(conn)
            with conn.cursor() as cur:
                cur.execute("DELETE FROM snowflake_column_cache")
            conn.commit()
    except Exception as e:
        logger.warning("Shared column cache clear failed: %s", e)


def _vehicle_table_columns(table: str) -> list:
    """[(column, data_type)] of SCHEMA.TABLE in table order; [] if it does not exist.
    Read from the shared Aurora copy when it is under 10 minutes old."""
    if not isinstance(table, str) or not re.fullmatch(rf"{_IDENTIFIER}\.{_IDENTIFIER}", table):
        return []
    shared = _read_shared_columns(table)
    if shared is not None:
        return shared
    schema, name = table.split(".")
    with SnowflakeClient() as sf:
        rows, _ = sf.execute(f"""
            SELECT column_name, data_type
            FROM {_VEHICLE_DB}.INFORMATION_SCHEMA.COLUMNS
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
        """, [schema, name])
    columns = [(str(r[0]).upper(), str(r[1]).upper()) for r in rows]
    # An empty list (table missing) is not shared, so a table created later is
    # picked up on the next request.
    if columns:
        _write_shared_columns(table, columns)
    return columns


def _validate_vehicle_view(view) -> str | None:
    if not isinstance(view, dict) or set(view) - {"table", "columns", "grey_rules", "filters"}:
        return "bad vehicle view"
    error = _validate_filter_list(view.get("grey_rules", []), "grey-out rule")
    if error:
        return error
    # Quick filters shown above the list: [{column, label}].
    quick = view.get("filters", [])
    if not isinstance(quick, list) or len(quick) > _VEHICLE_FILTERS_MAX:
        return f"At most {_VEHICLE_FILTERS_MAX} quick filters"
    quick_seen = set()
    for q in quick:
        if not isinstance(q, dict) or set(q) - {"column", "label"}:
            return "bad quick filter"
        name = q.get("column")
        if not isinstance(name, str) or not re.fullmatch(_IDENTIFIER, name) or name in quick_seen:
            return f"Choose a different column for each quick filter: {name}"
        quick_seen.add(name)
        label = q.get("label", "")
        if not isinstance(label, str) or len(label) > 60:
            return "Quick filter names must be 60 characters or fewer"
    table = view.get("table")
    if not isinstance(table, str) or not re.fullmatch(rf"{_IDENTIFIER}\.{_IDENTIFIER}", table):
        return "bad vehicle table"
    columns = view.get("columns")
    if not isinstance(columns, list) or len(columns) > 300:
        return "bad vehicle columns"
    seen = set()
    for col in columns:
        if not isinstance(col, dict) or set(col) - {"column", "label", "format", "decimals", "summary", "blank_grey"}:
            return "bad vehicle column"
        if not isinstance(col.get("blank_grey", False), bool):
            return f"bad blank-on-grey setting for {col.get('column')}"
        if col.get("summary", "none") not in ("none", "sum", "avg"):
            return f"bad total/average setting for {col.get('column')}"
        if col.get("format", "text") not in _VEHICLE_FORMATS:
            return f"bad format for {col.get('column')}"
        decimals = col.get("decimals", 0)
        if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 4:
            return f"bad decimals for {col.get('column')}"
        name = col.get("column")
        if not isinstance(name, str) or not re.fullmatch(_IDENTIFIER, name) or name in seen:
            return f"bad vehicle column: {name}"
        seen.add(name)
        label = col.get("label", "")
        if not isinstance(label, str) or len(label) > 60:
            return "Column headings must be 60 characters or fewer"
    return None


def _validate_row_vehicles(vehicles) -> str | None:
    """A chart's View Vehicles setting: its table, date column, filters and
    whether the list is a date range or a single-day snapshot. An absent table
    falls back to the default table, and an absent date_mode to a range, so
    charts saved before this feel no change."""
    if vehicles is None:
        return None
    if not isinstance(vehicles, dict) or set(vehicles) - {"table", "date_column", "filters", "date_mode"}:
        return "bad vehicles setting"
    table = vehicles.get("table")
    if table is not None and (not isinstance(table, str) or not re.fullmatch(rf"{_IDENTIFIER}\.{_IDENTIFIER}", table)):
        return "bad vehicle table"
    if vehicles.get("date_mode", "range") not in ("range", "snapshot"):
        return "bad date mode"
    date_column = vehicles.get("date_column")
    if not isinstance(date_column, str) or not re.fullmatch(_IDENTIFIER, date_column):
        return "Choose the date column for View Vehicles"
    return _validate_filter_list(vehicles.get("filters", []), "View Vehicles filter")


def _validate_filter_list(filters, name: str) -> str | None:
    """A list of {column, op, value}: a chart's View Vehicles filters, or the
    Settings grey-out rules. Columns are checked against the live table when
    the list is used."""
    if not isinstance(filters, list) or len(filters) > _VEHICLE_FILTERS_MAX:
        return f"At most {_VEHICLE_FILTERS_MAX} of each {name}"
    for f in filters:
        if not isinstance(f, dict) or set(f) - {"column", "op", "value"}:
            return f"bad {name}"
        if not isinstance(f.get("column"), str) or not re.fullmatch(_IDENTIFIER, f["column"]):
            return f"Choose a column for each {name}"
        op = f.get("op")
        if op not in _FILTER_OPS_NO_VALUE | _FILTER_OPS_VALUE:
            return f"bad operator in a {name}: {op}"
        value = f.get("value", "")
        if not isinstance(value, str) or len(value) > 500:
            return f"Values in a {name} must be 500 characters or fewer"
        if op in _FILTER_OPS_VALUE and not value.strip():
            return f"Enter a value for the {f['column']} {name}"
    return None


def _filter_sql(f: dict, data_type: str) -> tuple[str, list]:
    """One filter as SQL plus its bound values. The column has been checked
    against the live list, so it can go into the SQL."""
    col, op, value = f["column"], f["op"], (f.get("value") or "").strip()
    if op == "is true":
        return f"{col} = TRUE", []
    if op == "is false":
        return f"{col} = FALSE", []
    if op == "is empty":
        return f"({col} IS NULL OR TRIM({col}::VARCHAR) = '')", []
    if op == "is not empty":
        return f"({col} IS NOT NULL AND TRIM({col}::VARCHAR) <> '')", []
    if op in ("in", "not in"):
        values = [v.strip() for v in value.split(",") if v.strip()]
        placeholders = ", ".join(["%s"] * len(values))
        # Compared as text so one list works for text and number columns alike.
        return f"{col}::VARCHAR {'NOT IN' if op == 'not in' else 'IN'} ({placeholders})", values
    if op == "contains":
        return f"{col}::VARCHAR ILIKE %s", [f"%{value}%"]
    # Comparisons follow the column's type: true/false columns as true/false,
    # number columns as numbers, date columns as dates (value YYYY-MM-DD),
    # everything else as text.
    if data_type == "BOOLEAN":
        flag = {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False}.get(value.lower())
        if flag is None or op not in ("=", "!="):
            raise ValueError(f"The {col} filter is true/false: use is true / is false, or equals true / false")
        return f"{col} {op} %s", [flag]
    if data_type in _SNOWFLAKE_NUMBER_TYPES:
        try:
            return f"{col} {op} %s", [float(value)]
        except ValueError:
            raise ValueError(f"The {col} filter needs a number, not '{value}'")
    if data_type in _VEHICLE_DATE_TYPES:
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"The {col} filter needs a date as YYYY-MM-DD, not '{value}'")
        return f"{col}::DATE {op} %s", [value]
    return f"{col}::VARCHAR {op} %s", [value]


def _json_cell(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    try:
        return float(value)   # Decimal
    except (TypeError, ValueError):
        return str(value)


@router.get("/kpi/vehicle-columns")
def kpi_vehicle_columns(request: Request, table: str = "", user: str = Depends(require_page_api('kpi'))):
    """Live table list, the saved per-table configs, and the chosen table's
    columns, read when the Settings or Edit Chart popup opens so columns added to
    a table show up straight away. `table` picks which table's columns to return
    (defaults to the first saved config, or the default table)."""
    try:
        stored = _read_kpi_layout_value('vehicle_view')
        views = _vehicle_views(stored)
        table = table or (views[0]["table"] if views else _VEHICLE_TABLE_DEFAULT)
        view = _vehicle_view_for_table(stored, table)
        columns = _vehicle_table_columns(table)
        tables = _vehicle_tables() if is_admin(user) else []
    except Exception as e:
        message = getattr(e, "detail", None) or str(e)
        logger.error(f"KPI vehicle columns failed: {message}")
        return {"status": "error", "message": message}
    return {"status": "ok", "view": view, "views": views, "table": table, "tables": tables,
            "columns": [{"column": c, "type": t} for c, t in columns]}


@router.post("/kpi/vehicle-view")
def kpi_vehicle_view_save(request: Request, payload: dict, user: str = Depends(require_page_api('kpi'))):
    """One table's View Vehicles columns, saved (added or replaced) in the list.
    Admin only."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the View Vehicles columns"}
    view = (payload or {}).get("view")
    err = _validate_vehicle_view(view)
    if err:
        return {"status": "error", "message": err}
    try:
        if not _vehicle_table_columns(view["table"]):
            return {"status": "error", "message": f"Table {view['table']} was not found"}
        view = _vehicle_view_config(view)
        views = [v for v in _vehicle_views(_read_kpi_layout_value('vehicle_view')) if v["table"] != view["table"]]
        views.append(view)
        _write_kpi_layout_value('vehicle_view', views)
    except Exception as e:
        logger.error(f"KPI vehicle view save failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "view": view, "views": views}


@router.get("/kpi/vehicles")
def kpi_vehicles(request: Request, row_id: str, start: str, end: str,
                 user: str = Depends(require_page_api('kpi'))):
    """The vehicles behind one chart point: the chart's own table, date column
    and filters, with that table's Settings columns. A range list covers start
    to end (inclusive); a snapshot list is the single end day, so a weekly or
    monthly point on a per-vehicle-per-day table (stock) returns one day rather
    than every vehicle times every day."""
    try:
        start_date = datetime.date.fromisoformat(start)
        end_date = datetime.date.fromisoformat(end)
    except ValueError:
        return {"status": "error", "message": "Dates must be YYYY-MM-DD"}
    if start_date > end_date or (end_date - start_date).days > 400:
        return {"status": "error", "message": "Bad date range"}
    try:
        layout = _read_kpi_layout_value('layout') or KPI_DEFAULT_LAYOUT
        row = next((r for r in layout.get("rows", []) if r.get("id") == row_id), None)
        if not row:
            return {"status": "error", "message": "Chart not found - reload the page"}
        vehicles = row.get("vehicles")
        if not vehicles:
            return {"status": "error", "message": "View Vehicles is not set up for this chart"}
        table = vehicles.get("table") or _VEHICLE_TABLE_DEFAULT
        view = _vehicle_view_for_table(_read_kpi_layout_value('vehicle_view'), table)
        live = dict(_vehicle_table_columns(table))
        if not live:
            return {"status": "error", "message": f"Table {table} was not found"}

        # Ticked columns that have since gone from the table are left out
        # rather than failing the whole list.
        shown = [c for c in view["columns"] if c["column"] in live]
        missing = [c["column"] for c in view["columns"] if c["column"] not in live]
        if not shown:
            return {"status": "error", "message": f"No View Vehicles columns are set up for {table} - set them in Settings"}
        date_col = vehicles["date_column"]
        if date_col not in live:
            return {"status": "error", "message": f"Date column {date_col} is not in {table}"}
        if vehicles.get("date_mode", "range") == "snapshot":
            where, params = [f"{date_col}::DATE = %s"], [end_date.isoformat()]
        else:
            where, params = [f"{date_col}::DATE BETWEEN %s AND %s"], [start_date.isoformat(), end_date.isoformat()]
        for f in vehicles.get("filters", []):
            if f["column"] not in live:
                return {"status": "error", "message": f"Filter column {f['column']} is not in {table}"}
            try:
                sql, values = _filter_sql(f, live[f["column"]])
            except ValueError as e:
                return {"status": "error", "message": str(e)}
            where.append(sql)
            params.extend(values)

        select_cols = ", ".join(c["column"] for c in shown)
        # Grey-out check, read whether or not its columns are shown: one extra
        # true/false at the end of each row, after the shown columns, true when
        # ANY grey-out rule matches. A rule on a column that has gone from the
        # table is skipped rather than failing the list.
        grey_parts, grey_params = [], []
        for rule in view["grey_rules"]:
            if rule["column"] not in live:
                continue
            try:
                sql, values = _filter_sql(rule, live[rule["column"]])
            except ValueError as e:
                return {"status": "error", "message": f"Grey-out rule: {e}"}
            grey_parts.append(f"({sql})")
            grey_params.extend(values)
        grey = bool(grey_parts)
        if grey:
            select_cols += f", COALESCE({' OR '.join(grey_parts)}, FALSE) AS GREY_ROW__"
            # The SELECT's placeholders come before the WHERE's.
            params = grey_params + params
        # Quick filter columns not already shown are read as extra values after
        # the grey flag. Each filter says which value in the row to use.
        shown_index = {c["column"]: i for i, c in enumerate(shown)}
        next_index = len(shown) + (1 if grey else 0)
        quick_filters = []
        for q in view["filters"]:
            if q["column"] not in live:
                continue
            if q["column"] in shown_index:
                index = shown_index[q["column"]]
            else:
                select_cols += f", {q['column']}"
                index = next_index
                shown_index[q["column"]] = index
                next_index += 1
            quick_filters.append({"column": q["column"], "label": q.get("label") or q["column"],
                                  "type": live[q["column"]], "index": index})
        with SnowflakeClient() as sf:
            rows, _ = sf.execute(f"""
                SELECT {select_cols}
                FROM {_VEHICLE_DB}.{table}
                WHERE {' AND '.join(where)}
                ORDER BY {date_col}
                LIMIT {_VEHICLE_ROW_LIMIT + 1}
            """, params)
    except Exception as e:
        message = getattr(e, "detail", None) or str(e)
        logger.error(f"KPI vehicles failed for {row_id}: {message}")
        return {"status": "error", "message": message}
    return {
        "status": "ok",
        "columns": [{"column": c["column"], "label": c.get("label") or c["column"],
                     "type": live[c["column"]], "format": c.get("format"),
                     "decimals": c.get("decimals"), "summary": c.get("summary", "none"),
                     "blank_grey": bool(c.get("blank_grey"))} for c in shown],
        # Each row is the shown columns, then (when grey-out is on) its
        # true/false grey flag as one extra value at the end.
        "rows": [[_json_cell(v) for v in r] for r in rows[:_VEHICLE_ROW_LIMIT]],
        "grey_flag": grey,
        "quick_filters": quick_filters,
        "truncated": len(rows) > _VEHICLE_ROW_LIMIT,
        "row_limit": _VEHICLE_ROW_LIMIT,
        "missing_columns": missing,
        "table": table,
        "date_mode": vehicles.get("date_mode", "range"),
    }


# Model run through Snowflake Cortex (SNOWFLAKE.CORTEX.COMPLETE) rather than a
# third-party API — this account has no Anthropic API key, only a Cortex
# entitlement, so the model has to be one Cortex serves. claude-sonnet-5 is
# confirmed working on this account (2026-08-24) despite eu-west-1 normally
# being outside Anthropic's own regional availability for Claude.
_CORTEX_MODEL = "claude-sonnet-5"


def _cortex_complete(messages: list[dict], max_tokens: int, temperature: float) -> dict:
    """Run a chat completion through Snowflake Cortex and return the parsed
    response object (the same shape COMPLETE() returns: choices/usage)."""
    with SnowflakeClient() as client:
        rows, _ = client.execute(
            "SELECT SNOWFLAKE.CORTEX.COMPLETE(%s, PARSE_JSON(%s), PARSE_JSON(%s))",
            [_CORTEX_MODEL, json.dumps(messages),
             json.dumps({"temperature": temperature, "max_tokens": max_tokens})],
        )
    result = rows[0][0]
    return json.loads(result) if isinstance(result, str) else result


@router.post("/kpi/ai-summary")
def kpi_ai_summary(request: Request, payload: dict, user: str = Depends(require_page_api('kpi'))):

    prompt = (payload or {}).get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        return {"status": "error", "message": "Missing prompt"}

    try:
        data = _cortex_complete([{"role": "user", "content": prompt}],
                                 max_tokens=4096, temperature=0.3)
        usage = data.get("usage", {})
        logger.info(
            "Cortex AI summary: model=%s tokens_in=%s tokens_out=%s",
            _CORTEX_MODEL, usage.get("prompt_tokens"), usage.get("completion_tokens"),
        )
        summary = data["choices"][0]["messages"].strip()
        return {"status": "ok", "summary": summary}
    except Exception as e:
        logger.error("KPI AI summary request failed: %s", e)
        return {"status": "error", "message": str(e)}


# ── SharePoint metrics ───────────────────────────────────────────────────────
# Every KPI Daily Metrics file in Settings > SharePoint: a date column plus
# number columns, one row per day. Every column that holds only numbers is a
# metric; Column Renames there change a header's metric name, and a renamed
# column is kept even if a cell holds text (that cell is skipped). The files
# themselves are read, checked for changes and cached by
# utils/sharepoint_sources; this only turns each copy into metrics by date.

# Load Data click token last acted on, so the many chart requests from one
# click ask SharePoint once.
_sp_refresh_token = ""
_sp_refresh_lock = threading.Lock()

# (key, copy saved time, copy download time, settings) -> parsed copy.
_sp_parsed: dict = {}
# The last combined result, for _validate_metric (which must not wait on a read).
_kpi_sp_last: dict = {}


def _sp_parse_date(val) -> str | None:
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        try:
            return (datetime.date(1899, 12, 30) + datetime.timedelta(days=int(val))).isoformat()
        except Exception:
            return None
    s = str(val).strip()[:10]
    try:
        datetime.date.fromisoformat(s)
        return s
    except ValueError:
        return None


def _sp_number(val):
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val).strip())
    except (TypeError, ValueError):
        return None


def _sp_cutoff() -> str:
    """Far enough back for the longest periods the settings allow plus their
    N-1 a year earlier: 24 months + 12, or 104 weeks + a year of weeks."""
    cutoff = datetime.date.today().replace(day=1)
    for _ in range(38):
        cutoff = (cutoff - datetime.timedelta(days=1)).replace(day=1)
    return cutoff.isoformat()


def _parse_kpi_copy(src: dict, values: list) -> dict:
    """{columns, by_date} from one KPI Daily Metrics file's copy. Row 0 is the
    header row."""
    if not values:
        raise ValueError("no rows")
    headers = [str(h).strip() if h is not None else "" for h in values[0]]
    date_header = (src.get("date_header") or "Date").strip()
    date_idx = next((i for i, h in enumerate(headers) if h.lower() == date_header.lower()), None)
    if date_idx is None:
        raise ValueError(f"date column '{date_header}' not found. Found: {[h for h in headers if h]}")
    renames = {str(k).strip(): str(v).strip() for k, v in (src.get("column_names") or {}).items()
               if str(v).strip()}
    cutoff = _sp_cutoff()
    dated = []
    for row in values[1:]:
        d = _sp_parse_date(row[date_idx] if date_idx < len(row) else None)
        if d and d >= cutoff:
            dated.append((d, row))

    # A number column is one where every filled cell is a number and at least
    # one cell is filled. Text columns (notes, labels) are left out, unless the
    # column is listed under Column Renames.
    number_columns = []
    for i, header in enumerate(headers):
        if i == date_idx or not header:
            continue
        filled = [row[i] for _, row in dated if i < len(row) and row[i] not in (None, "")]
        if header in renames or (filled and all(_sp_number(v) is not None for v in filled)):
            number_columns.append((i, renames.get(header, header)))

    by_date: dict[str, dict] = {}
    for d, row in dated:
        day = {}
        for i, metric in number_columns:
            if i < len(row) and row[i] not in (None, ""):
                v = _sp_number(row[i])
                if v is not None:
                    day[metric] = v
        if day:
            by_date.setdefault(d, {}).update(day)
    return {"columns": [m for _, m in number_columns], "by_date": by_date}


def _uk_text(ts) -> str:
    import zoneinfo
    return ts.astimezone(zoneinfo.ZoneInfo("Europe/London")).strftime("%d %b %H:%M") if ts else "unknown"


def _get_sharepoint_data(sp_refresh: str = "") -> dict:
    """Every switched-on KPI Daily Metrics file, merged by date.
    sp_refresh: a Load Data click token - the first request carrying it asks
    SharePoint about every file now rather than waiting for its interval.

    Never waits for a download: a file saved since its copy is downloaded by
    the worker, and comes back listed in "pending" so the page can redraw once
    it has arrived."""
    global _sp_refresh_token, _kpi_sp_last
    check = "auto"
    if sp_refresh:
        with _sp_refresh_lock:
            if sp_refresh != _sp_refresh_token:
                _sp_refresh_token = sp_refresh
                check = "now"
    try:
        sources = sharepoint_sources.sources_of_type(sharepoint_sources.KPI_FILE_TYPE)
    except Exception as e:
        logger.error("KPI SharePoint: file list unavailable: %s", e)
        return {"status": "error", "message": f"SharePoint file list unavailable: {e}"}

    def one(src):
        try:
            return src, sharepoint_sources.get_copy(src["source_key"], check=check)
        except Exception as e:
            return src, sharepoint_sources.Copy(error=str(e))

    copies = []
    if sources:
        with ThreadPoolExecutor(max_workers=min(4, len(sources))) as pool:
            copies = list(pool.map(one, sources))

    columns, by_date, descriptions = [], {}, {}
    pending, stale, errors = [], [], []
    for src, copy in copies:
        if copy.pending:
            pending.append({"key": src["source_key"], "name": src["name"]})
        if copy.values is None:
            if not copy.pending:
                errors.append(f"{src['name']}: {copy.error or 'no data'}")
            continue
        if copy.error:
            stale.append(f"{src['name']} from {_uk_text(copy.saved_at)}")
        sig = (src["source_key"], copy.modified, copy.downloaded_at, src.get("date_header"),
               json.dumps(src.get("column_names") or {}, sort_keys=True))
        part = _sp_parsed.get(sig)
        if part is None:
            try:
                part = _parse_kpi_copy(src, copy.values)
            except Exception as e:
                errors.append(f"{src['name']}: {e}")
                continue
            for old in [k for k in _sp_parsed if k[0] == src["source_key"]]:
                _sp_parsed.pop(old, None)
            _sp_parsed[sig] = part
        for col in part["columns"]:
            if col not in descriptions:
                columns.append(col)
                descriptions[col] = f"From the {src['name']} spreadsheet"
        for d, values in part["by_date"].items():
            day = by_date.setdefault(d, {})
            for col, v in values.items():
                if descriptions.get(col) == f"From the {src['name']} spreadsheet":
                    day[col] = v
    for e in errors:
        logger.warning("KPI SharePoint: %s", e)
    result = {
        "status": "ok",
        "columns": columns,
        "by_date": by_date,
        "descriptions": descriptions,
        "pending": pending,
        "errors": errors,
        "stale": bool(stale),
        "stale_date": "; ".join(stale) if stale else None,
        "keys": [s["source_key"] for s in sources],
        "check_seconds": min((max(1, int(s["check_minutes"])) * 60 for s in sources), default=900),
    }
    _kpi_sp_last = result
    return result


def warm_sharepoint_cache() -> None:
    """Run the check for every SharePoint file. Called by the /home cache warmer."""
    sharepoint_sources.check_due()
    logger.info("Home cache warm (SharePoint files) complete")


@router.get("/weekly-business-review", response_class=HTMLResponse)
def weekly_business_review_page(request: Request, user: str = Depends(require_page('weekly_business_review'))):
    _track(request, 'page_view', '/weekly-business-review')
    return templates.TemplateResponse("weekly_business_review.html", {"request": request, "user": user, "BASE_URL": BASE_URL})


@router.get("/weekly-business-review/data")
def weekly_business_review_data(request: Request, user: str = Depends(require_page_api('weekly_business_review'))):
    """Return monthly Net Deliveries B2C (b2c_units): last 3 complete months, current month, next 3 months."""
    try:
        today = datetime.date.today()
        first_of_this_month = today.replace(day=1)

        def _shift_month(d: datetime.date, n: int) -> datetime.date:
            m = d.month - 1 + n
            return datetime.date(d.year + m // 12, m % 12 + 1, 1)

        month_firsts = [_shift_month(first_of_this_month, n) for n in range(-3, 4)]
        # Fetch an extra year back so each displayed month's N-1 (12 months prior) is covered
        fetch_start = _shift_month(month_firsts[0], -12)
        fetch_end = month_firsts[-1]

        with SnowflakeClient() as sf:
            cost_rows, cost_cols = sf.execute("""
                SELECT MONTH_START, COST_AMOUNT
                FROM PROD_CARSUPERMARKET_DB.SOURCE_FINANCE.MANAGEMENT_ACCOUNTS
                WHERE COST_TYPE = 'b2c_units'
                  AND MONTH_START >= %s AND MONTH_START <= %s
            """, [str(fetch_start), str(fetch_end)])
            budget_rows, budget_cols = sf.execute("""
                SELECT MONTH_YEAR, VALUE
                FROM PROD_CARSUPERMARKET_DB.SOURCE_FINANCE.BUDGET
                WHERE ITEM = 'b2c_units'
                  AND MONTH_YEAR >= %s AND MONTH_YEAR <= %s
            """, [str(fetch_start), str(fetch_end)])

        cost_col_map = {c.upper(): i for i, c in enumerate(cost_cols)}
        budget_col_map = {c.upper(): i for i, c in enumerate(budget_cols)}

        def _num(val):
            try:
                return round(float(val)) if val is not None else None
            except (TypeError, ValueError):
                return None

        actual_by_month: dict[str, float] = {}
        for r in cost_rows:
            d = str(r[cost_col_map['MONTH_START']])[:10]
            actual_by_month[d] = _num(r[cost_col_map['COST_AMOUNT']])

        target_by_month: dict[str, float] = {}
        for r in budget_rows:
            d = str(r[budget_col_map['MONTH_YEAR']])[:10]
            target_by_month[d] = _num(r[budget_col_map['VALUE']])

        month_abbrs = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

        monthly_labels             = [month_abbrs[mf.month - 1] + ' ' + str(mf.year)[2:] for mf in month_firsts]
        # Actual only exists up to the current month; N-1 (actual, shifted -12) and
        # Target (budget) are forward-looking and populated for the full window
        monthly_b2c_units          = [actual_by_month.get(mf.isoformat()) if mf <= first_of_this_month else None for mf in month_firsts]
        monthly_b2c_units_ly       = [actual_by_month.get(_shift_month(mf, -12).isoformat()) for mf in month_firsts]
        monthly_b2c_units_target   = [target_by_month.get(mf.isoformat()) for mf in month_firsts]

        return {
            "status": "ok",
            "today": today.isoformat(),
            "monthly_labels": monthly_labels,
            "monthly_b2c_units": monthly_b2c_units,
            "monthly_b2c_units_ly": monthly_b2c_units_ly,
            "monthly_b2c_units_target": monthly_b2c_units_target,
        }
    except Exception as e:
        logger.error(f"Weekly Business Review data failed: {e}")
        return {"status": "error", "message": str(e)}
