"""KPI dashboard: Snowflake daily summary, SharePoint sessions, Cortex AI summary."""

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
import calendar
import json
import time
import logging
import datetime


from utils.db_utils import (get_database_connection, get_prod_pricer_connection,
                            get_chart_targets, get_chart_target_ranges, save_chart_targets,
                            delete_chart_target, upsert_chart_target_dates,
                            get_chart_target_summary, get_ga4_campaign_ids,
                            add_ga4_campaign, remove_ga4_campaign)
from utils.snowflake_client import SnowflakeClient
from utils.sharepoint_client import SharePointClient

from .core import (BASE_URL, templates, _track, get_current_user, is_admin,
                   require_page, require_page_api, _uk_now)

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
#   sharepoint - a number column of the "Daily Spend and Enquiries" table
#
# The column lists are read live, so new columns appear without code changes.
# This list only supplies descriptions and the sensible weekly/monthly rollup
# for columns we know: 'sum' for daily flow events, 'avg' for stock levels and
# pre-averaged metrics. Columns not listed here get a guessed default.
KPI_METRICS = [
    ("ORDERS", "Apex orders acknowledged on the day", "sum"),
    ("ORDERS_RATE_TOTAL", "Orders financed on a standard rate (non interest-free)", "sum"),
    ("ORDERS_CASH_TOTAL", "Orders paid in cash (no finance)", "sum"),
    ("ORDERS_RATE_0_15K", "Rate-financed orders with vehicle price £0-15k", "sum"),
    ("ORDERS_RATE_15_25K", "Rate-financed orders with vehicle price £15-25k", "sum"),
    ("ORDERS_RATE_25K_PLUS", "Rate-financed orders with vehicle price £25k+", "sum"),
    ("ORDERS_CASH_0_15K", "Cash orders with vehicle price £0-15k", "sum"),
    ("ORDERS_CASH_15_25K", "Cash orders with vehicle price £15-25k", "sum"),
    ("ORDERS_CASH_25K_PLUS", "Cash orders with vehicle price £25k+", "sum"),
    ("ORDERS_PRICE_CHANGED_LAST_7_DAYS", "Orders where the vehicle had a price change in the 7 days before the order", "sum"),
    ("ORDERS_PUBLISHED_LAST_7_DAYS", "Orders where the vehicle was first advertised in the 7 days before the order", "sum"),
    ("ORDERS_SUB_12K_UNDER_30_DIS", "Orders with base price under £12k (no admin fee) and under 30 days in stock - the Apex rule that keeps cars off Autotrader", "sum"),
    ("AVG_DIS_ORDERS_SUB_12K_UNDER_30_DIS", "Average days in stock of orders under £12k and under 30 days in stock", "avg"),
    ("AVG_DAYS_ADVERTISED_ORDERS_SUB_12K_UNDER_30_DIS", "Average days advertised of orders under £12k and under 30 days in stock", "avg"),
    ("AVG_AT_PRICE_POSITION_ORDERS_SUB_12K_UNDER_30_DIS", "Average Autotrader price position of rate orders under £12k and under 30 days in stock", "avg"),
    ("AVG_AT_RETAIL_RATING_ORDERS_SUB_12K_UNDER_30_DIS", "Average Autotrader retail rating of orders under £12k and under 30 days in stock", "avg"),
    ("PURCHASES", "Vehicles purchased into stock on the day", "sum"),
    ("PUBLISHED", "Vehicles first advertised on the day", "sum"),
    ("STOCK_ON_SALE", "Advertised vehicles in stock (end of day)", "avg"),
    ("STOCK_ON_SALE_STORES", "Advertised stock physically located at a retail store", "avg"),
    ("STOCK_ON_SALE_RATE", "Advertised stock on standard rate finance (non interest-free)", "avg"),
    ("STOCK_ON_SALE_0PCT", "Advertised stock on 0% interest-free finance", "avg"),
    ("STOCK_ON_SALE_RATE_EV", "Advertised electric vehicles on standard rate finance", "avg"),
    ("TOTAL_STOCK", "All vehicles in stock, advertised or not (end of day)", "avg"),
    ("STOCK_45_PLUS_DAYS", "Vehicles in stock for 45 days or more", "avg"),
    ("STOCK_60_PLUS_DAYS", "Vehicles in stock for 60 days or more", "avg"),
    ("AVG_DAYS_IN_STOCK", "Average days in stock across all vehicles", "avg"),
    ("AVG_GPU_M_STOCK_ON_SALE", "Average GPU-M (£) of advertised stock", "avg"),
    ("AVG_GPU_M_B2B2C", "Average GPU-M (£) of advertised B2B2C stock", "avg"),
    ("AVG_GPU_M_CONSIGNMENT", "Average GPU-M (£) of advertised consignment stock", "avg"),
    ("AVG_GPU_M_C2B2C_PX", "Average GPU-M (£) of advertised part-exchange stock", "avg"),
    ("AVG_GPU_M_C2B2C_OTHER", "Average GPU-M (£) of advertised C2B2C (non-PX) stock", "avg"),
    ("AVG_AT_PRICE_POSITION_UNSOLD_STOCK_RATE", "Average Autotrader price position of unsold advertised rate stock", "avg"),
    ("AVG_AT_PRICE_POSITION_UNSOLD_STOCK_0PCT", "Average Autotrader price position of unsold advertised 0% stock", "avg"),
    ("AVG_AT_PRICE_POSITION_ORDERS_RATE", "Average Autotrader price position of rate vehicles ordered that day", "avg"),
    ("AVG_AT_PRICE_POSITION_ORDERS_0PCT", "Average Autotrader price position of 0% vehicles ordered that day", "avg"),
]
_KPI_METRIC_INFO = {col: (desc, agg) for col, desc, agg in KPI_METRICS}

KPI_SOURCES = ("snowflake", "sharepoint")

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


def _get_daily_summary_column_types() -> list:
    global _ds_columns_cache, _ds_columns_cache_at
    if _ds_columns_cache is not None and time.time() - _ds_columns_cache_at < _DS_COLUMNS_TTL:
        return _ds_columns_cache
    try:
        with SnowflakeClient() as sf:
            rows, _ = sf.execute("""
                SELECT column_name, data_type
                FROM PROD_CARSUPERMARKET_DB.INFORMATION_SCHEMA.COLUMNS
                WHERE table_schema = 'PRODUCT_SLT_KPIS' AND table_name = 'DAILY_SUMMARY'
                ORDER BY ordinal_position
            """)
        _ds_columns_cache = [(str(r[0]).upper(), str(r[1]).upper()) for r in rows]
        _ds_columns_cache_at = time.time()
    except Exception as e:
        logger.warning("daily_summary column discovery failed: %s", e)
        if _ds_columns_cache is None:
            # Minimal fallback: the known metric columns plus the orders siblings
            _ds_columns_cache = ([(c, "NUMBER") for c, _, _ in KPI_METRICS]
                                 + [("ORDERS_TARGET", "NUMBER")])
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
    """Rollup for a column with no entry in KPI_METRICS: averages and stock
    levels average, everything else is a daily count that adds up."""
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
    if source not in KPI_SOURCES:
        return f"Unknown source: {source}"
    if not isinstance(metric, str) or not metric.strip():
        return "Missing metric"
    if source == "snowflake":
        if metric not in _snowflake_metric_columns():
            return f"Unknown metric: {metric}"
    else:
        # Only checked against the list when the SharePoint data is already in
        # memory — reading it can mean a slow SharePoint fetch, which a layout
        # save should not wait on.
        cached_columns = _kpi_sp_cache.get("columns") if _kpi_sp_cache.get("status") == "ok" else None
        if cached_columns is not None and metric not in cached_columns:
            return f"Unknown SharePoint column: {metric}"
        if len(metric) > 200:
            return "Metric name is too long"
    return None


KPI_SECTIONS = ["Upper Funnel", "Results", "Stock", "Pricing", "Margin"]

KPI_DEFAULT_LAYOUT = {"rows": []}


@router.get("/kpi/metrics")
def kpi_metrics(request: Request, user: str = Depends(require_page_api('kpi'))):
    live_cols = _get_daily_summary_columns()
    metrics = []
    for col in _snowflake_metric_columns():
        desc, agg = _KPI_METRIC_INFO.get(col, ("", _guess_agg(col)))
        metrics.append({
            "source": "snowflake",
            "column": col,
            "description": desc,
            "suggested_agg": agg,
            "has_n1": True,   # N-1 is looked up from the metric's own history
            "has_target": f"{col}_TARGET" in live_cols,
        })
    for col in _sharepoint_metric_columns():
        metrics.append({
            "source": "sharepoint",
            "column": col,
            "description": "From the Daily Spend and Enquiries spreadsheet",
            "suggested_agg": _guess_agg(col),
            # N-1 is worked out from the spreadsheet's own history.
            "has_n1": True,
            "has_target": False,
        })
    return {"status": "ok", "sections": KPI_SECTIONS, "metrics": metrics}


# ── KPI chart periods ────────────────────────────────────────────────────────
# How many months, weeks and days the charts cover is a page setting (see
# /kpi/settings). The page sends the counts with each data request; they are
# clamped here so a bad value cannot ask Snowflake for years of data.
#
# The day series always runs KPI_FUTURE_DAYS past today, so the target line
# shows what is coming. days_total includes those future days.

KPI_FUTURE_DAYS = 10

KPI_DEFAULT_SETTINGS = {
    "months": 14,          # including the current month
    "weeks": 10,           # including the current week
    "days_window": 35,     # days on screen at once in a day chart
    "days_total": 90,      # days available to scroll across
    "label_angle": 0,      # tilt of the value labels on the data points
}

KPI_SETTING_LIMITS = {
    "months": (1, 24),
    "weeks": (1, 104),
    "days_window": (7, 400),
    "days_total": (KPI_FUTURE_DAYS + 1, 400),
    "label_angle": (-90, 90),
}


def _clamp_setting(name: str, value) -> int:
    low, high = KPI_SETTING_LIMITS[name]
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = KPI_DEFAULT_SETTINGS[name]
    return min(max(value, low), high)


def _kpi_periods(today: datetime.date, months: int, weeks: int, days_total: int):
    """Month starts, week Mondays and day dates for the KPI charts, oldest first.
    The last month and week are the current, part-elapsed ones."""
    first_of_this_month = today.replace(day=1)
    month_firsts = [first_of_this_month]
    for _ in range(months - 1):
        month_firsts.insert(0, (month_firsts[0] - datetime.timedelta(days=1)).replace(day=1))

    current_week_monday = _week_monday_of(today)
    week_mondays = [current_week_monday - datetime.timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]

    daily_end = today + datetime.timedelta(days=KPI_FUTURE_DAYS)
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
#   import     - per-date figures from an uploaded CSV
#   snowflake  - the column's own <METRIC>_TARGET in daily_summary
#
# Fixed and imported figures are stored under separate keys in chart_targets,
# so switching a chart to fixed keeps the imported dates for switching back.
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
                    include_current: bool = False,
                    user: str = Depends(require_page_api('kpi'))):
    """Daily/weekly/monthly series for one metric, plus its N-1 and target.
    include_current: also fill the current (part-elapsed) week and month."""
    if source == "snowflake":
        metric = metric.upper()
    error = _validate_metric(source, metric)
    if error:
        return {"status": "error", "message": error}
    if agg not in ("sum", "avg"):
        return {"status": "error", "message": f"Unknown agg: {agg}"}
    if target_mode not in KPI_TARGET_MODES:
        return {"status": "error", "message": f"Unknown target mode: {target_mode}"}
    months = _clamp_setting("months", months)
    weeks = _clamp_setting("weeks", weeks)
    days = _clamp_setting("days_total", days)
    try:
        today = datetime.date.today()
        month_firsts, week_mondays, dates = _kpi_periods(today, months, weeks, days)
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

        target_col = None
        stale_date = None
        if source == "snowflake":
            # N-1 is looked up from the metric's own history (below), the same
            # way for both sources, so the <METRIC>_N_MINUS_1 column is not read.
            # It also only runs to yesterday, which left future days blank.
            target_col = _target_column(metric)
            # Fetch a year further back than anything shown, for N-1, and
            # forward to the end of next month, so the current month's target
            # total is complete.
            fetch_start = min(_shift_years(dates[0], -1), _shift_years(month_firsts[0], -1),
                              prev_week_mondays[0])
            _nm = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
            fetch_end = max(dates[-1], (_nm + datetime.timedelta(days=32)).replace(day=1) - datetime.timedelta(days=1))
            # All column names come from the live column list — safe to interpolate
            select_cols = ["DATE", metric] + ([target_col] if target_col else [])
            with SnowflakeClient() as sf:
                rows, cols = sf.execute(f"""
                    SELECT {', '.join(select_cols)}
                    FROM PROD_CARSUPERMARKET_DB.product_slt_kpis.daily_summary
                    WHERE DATE >= %s AND DATE <= %s
                    ORDER BY DATE
                """, [str(fetch_start), str(fetch_end)])
            col_map = {c.upper(): i for i, c in enumerate(cols)}
            by_date: dict = {}
            for r in rows:
                d = r[col_map['DATE']]
                d = d if isinstance(d, datetime.date) else datetime.date.fromisoformat(str(d)[:10])
                by_date[d] = {
                    'v':  _num(r[col_map[metric]]),
                    't':  _num(r[col_map[target_col]]) if target_col else None,
                }
            value_at = lambda d: by_date.get(d, {}).get('v')
        else:
            sp = _get_sharepoint_data()
            if sp.get("status") != "ok":
                return {"status": "error", "message": sp.get("message", "SharePoint data unavailable")}
            stale_date = sp.get("stale_date") if sp.get("stale") else None
            sp_by_date = sp.get("by_date", {})
            value_at = lambda d: _num(sp_by_date.get(d.isoformat(), {}).get(metric))

        # N-1 from the metric's own history, for both sources: the same date
        # a year back for days (as daily_summary's DATEADD(YEAR, -1)), the same
        # month a year back for months. Runs to the end of the day chart,
        # future days included.
        daily_n1_at = lambda d: value_at(_shift_years(d, -1))
        monthly_n1 = [_agg([value_at(d) for d in _month_dates(f.replace(year=f.year - 1))]) for f in month_firsts]

        month_abbrs = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']

        # ── Daily ─────────────────────────────────────────────────────────────
        values = [_round(value_at(d)) for d in dates]
        values_n1 = [_round(daily_n1_at(d)) for d in dates]

        # ── Monthly (current month left empty — it is part elapsed — unless
        # the chart asks for it) ─────────────────────────────────────────────
        first_of_this_month = month_firsts[-1]
        monthly_values = [None if f == first_of_this_month and not include_current else _agg([value_at(d) for d in _month_dates(f)])
                          for f in month_firsts]
        monthly_labels = [month_abbrs[f.month - 1] + ' ' + str(f.year)[2:] for f in month_firsts]

        # ── Weekly (current week left empty) ─────────────────────────────────
        # Weekly N-1 = same ISO week NUMBER of the previous year, aggregated
        # from the metric's own daily history.
        current_week_monday = week_mondays[-1]
        weekly_labels = [f"W{m.isocalendar()[1]} ({m.day}-{month_abbrs[m.month-1]})" for m in week_mondays]
        weekly_n1_labels = [f"W{pm.isocalendar()[1]} ({pm.day}-{month_abbrs[pm.month-1]})" for pm in prev_week_mondays]
        weekly_values = [None if m == current_week_monday and not include_current else _agg([value_at(d) for d in _week_dates(m)])
                         for m in week_mondays]
        weekly_values_n1 = [_agg([value_at(d) for d in _week_dates(pm)]) for pm in prev_week_mondays]

        # ── Target ───────────────────────────────────────────────────────────
        values_target = weekly_values_target = monthly_values_target = None
        if target_mode == "snowflake" and target_col:
            values_target = [_round(by_date.get(d, {}).get('t')) for d in dates]
            weekly_values_target = [_agg([by_date.get(d, {}).get('t') for d in _week_dates(m)]) for m in week_mondays]
            monthly_values_target = [_agg([by_date.get(d, {}).get('t') for d in _month_dates(f)]) for f in month_firsts]
        elif target_mode in ("fixed", "import"):
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
    """Body: {"source", "metric", "value": number|null}. null removes the fixed figure."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change KPI targets"}
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


KPI_IMPORT_MAX_ROWS = 5000


@router.post("/kpi/metric-target/import")
async def kpi_import_targets(request: Request, user: str = Depends(require_page_api('kpi'))):
    """Body: {"source", "metric", "rows": [{"date": "YYYY-MM-DD", "value": number}, ...]}.

    Only the dates in the file are written: a stored date is overwritten, a new
    one is added, and every other stored date is left alone.
    """
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change KPI targets"}
    body = await request.json()
    source, metric = body.get('source'), body.get('metric')
    if source == "snowflake" and isinstance(metric, str):
        metric = metric.upper()
    error = _validate_metric(source, metric)
    if error:
        return {"status": "error", "message": error}
    raw_rows = body.get('rows') or []
    if not raw_rows:
        return {"status": "error", "message": "The file has no rows"}
    if len(raw_rows) > KPI_IMPORT_MAX_ROWS:
        return {"status": "error", "message": f"Too many rows (limit {KPI_IMPORT_MAX_ROWS})"}

    by_date = {}
    for index, row in enumerate(raw_rows, start=1):
        try:
            d = datetime.date.fromisoformat(str(row.get('date')))
        except (TypeError, ValueError, AttributeError):
            return {"status": "error", "message": f"Row {index}: bad date {row.get('date')!r}"}
        value, error = _target_value_or_error(row.get('value'))
        if error:
            return {"status": "error", "message": f"Row {index} ({d}): {error}"}
        # A date repeated in the file: the later row wins, as it would in the sheet.
        by_date[d] = value

    key = _kpi_target_key("import", source, metric)
    try:
        with get_prod_pricer_connection() as conn:
            upsert_chart_target_dates(conn, key, sorted(by_date.items()), user)
            summary = get_chart_target_summary(conn, key)
    except Exception as e:
        logger.error("Importing KPI targets failed for %s:%s: %s", source, metric, e)
        return {"status": "error", "message": f"Import failed: {e}"}
    return {
        "status": "ok",
        "written": len(by_date),
        "imported": None if not summary else {
            "count": summary['count'],
            "first": summary['first'].isoformat(),
            "last": summary['last'].isoformat(),
        },
    }


# ── Layout and settings ──────────────────────────────────────────────────────

def _validate_layout(layout: dict) -> str | None:
    """Return an error message if the layout payload is invalid, else None."""
    rows = layout.get("rows") if isinstance(layout, dict) else None
    if not isinstance(rows, list):
        return "layout.rows must be a list"
    for row in rows:
        if not isinstance(row, dict):
            return "each row must be an object"
        if row.get("type") != "custom":
            return f"bad row type: {row.get('type')}"
        if row.get("section") not in KPI_SECTIONS:
            return f"bad section: {row.get('section')}"
        error = _validate_metric(row.get("source", "snowflake"), row.get("metric"))
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
        if not isinstance(row.get("include_current", False), bool):
            return "bad include_current"
        if row.get("fmt", "num") not in ("num", "pct", "pct100", "k"):
            return f"bad fmt: {row.get('fmt')}"
        decimals = row.get("decimals", 0)
        if not isinstance(decimals, int) or not (0 <= decimals <= 3):
            return f"bad decimals: {decimals}"
        if row.get("target_mode", "snowflake") not in KPI_TARGET_MODES:
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
        # Ratio chart: the metric divided by a second metric. The page fetches
        # both and divides; its target is a figure stored on the row itself,
        # because the stored per-metric targets are for the numerator alone.
        divide_by = row.get("divide_by")
        if divide_by is not None:
            if not isinstance(divide_by, dict):
                return "bad divide_by"
            error = _validate_metric(divide_by.get("source", "snowflake"), divide_by.get("metric"))
            if error:
                return error
            if row.get("ratio_as", "dec") not in ("dec", "pct"):
                return f"bad ratio_as: {row.get('ratio_as')}"
            if row.get("target_mode", "none") not in ("none", "fixed"):
                return "a ratio chart's target must be none or fixed"
            ratio_target = row.get("ratio_target")
            if ratio_target is not None and (isinstance(ratio_target, bool) or not isinstance(ratio_target, (int, float))):
                return "bad ratio_target"
    return None


def _read_kpi_layout_value(key: str):
    # Layout and settings always live in the PROD cluster so local/dev edits
    # and prod serve the same dashboard
    with get_prod_pricer_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS kpi_layout (key VARCHAR(50) PRIMARY KEY, value TEXT)")
            cur.execute("SELECT value FROM kpi_layout WHERE key = %s", (key,))
            row = cur.fetchone()
        conn.commit()
    return json.loads(row[0]) if row else None


def _write_kpi_layout_value(key: str, value) -> None:
    with get_prod_pricer_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS kpi_layout (key VARCHAR(50) PRIMARY KEY, value TEXT)")
            cur.execute(
                "INSERT INTO kpi_layout (key, value) VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, json.dumps(value))
            )
        conn.commit()


def _kpi_settings(stored: dict | None) -> dict:
    """Stored settings filled out with defaults and clamped to the limits."""
    merged = {**KPI_DEFAULT_SETTINGS, **(stored or {})}
    settings = {name: _clamp_setting(name, merged[name]) for name in KPI_DEFAULT_SETTINGS}
    settings["days_window"] = min(settings["days_window"], settings["days_total"])
    return settings


@router.get("/kpi/layout")
def kpi_layout_get(request: Request, user: str = Depends(require_page_api('kpi'))):
    try:
        layout = _read_kpi_layout_value('layout') or KPI_DEFAULT_LAYOUT
        settings = _kpi_settings(_read_kpi_layout_value('settings'))
        notes = _read_kpi_layout_value('notes') or []
    except Exception as e:
        logger.error(f"KPI layout load failed: {e}")
        layout, settings, notes = KPI_DEFAULT_LAYOUT, _kpi_settings(None), []
    # The hard-coded Sessions and Apex Orders rows were removed: every chart is
    # now created on the page. Drop them from a layout saved before that.
    layout = {**layout, "rows": [r for r in layout.get("rows", []) if r.get("type") == "custom"]}
    return {"status": "ok", "layout": layout, "sections": KPI_SECTIONS, "settings": settings,
            "setting_limits": KPI_SETTING_LIMITS, "notes": notes}


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
    err = _validate_layout(layout or {})
    if err:
        return {"status": "error", "message": err}
    try:
        _write_kpi_layout_value('layout', layout)
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"KPI layout save failed: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/kpi/settings")
def kpi_settings_save(request: Request, payload: dict, user: str = Depends(require_page_api('kpi'))):
    """Chart periods and label angle, shared by everyone. Admin only."""
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the KPI settings"}
    raw = (payload or {}).get("settings") or {}
    for name, (low, high) in KPI_SETTING_LIMITS.items():
        try:
            value = int(raw.get(name))
        except (TypeError, ValueError):
            return {"status": "error", "message": f"{name} must be a whole number"}
        if not low <= value <= high:
            return {"status": "error", "message": f"{name} must be between {low} and {high}"}
    settings = _kpi_settings(raw)
    if int(raw["days_window"]) > int(raw["days_total"]):
        return {"status": "error", "message": "Days shown cannot be more than the days available"}
    try:
        _write_kpi_layout_value('settings', settings)
    except Exception as e:
        logger.error(f"KPI settings save failed: {e}")
        return {"status": "error", "message": str(e)}
    return {"status": "ok", "settings": settings}


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


_kpi_sp_cache: dict = {}
_kpi_sp_cache_at: float = 0.0

# S3 cache for the aggregated SharePoint payload (small JSON, not the
# spreadsheet itself). Shared key (not env-scoped) so one fetch warms local,
# dev and prod. The SharePoint API is slow and unreliable — the S3 copy also
# serves as a stale fallback.
# The source spreadsheet is refreshed once per day between 08:00 and 09:00 UK,
# so a cached payload is fresh if fetched after the most recent 09:00 UK.
_SP_S3_BUCKET = "aa-raw-sources-uk"
_SP_S3_KEY = "applications/pricing/uk-vehicle-list-evaluator/shared/cache/kpi_sharepoint_v4.json"


def _sp_cache_is_fresh(cached_at: float) -> bool:
    import zoneinfo
    uk = zoneinfo.ZoneInfo("Europe/London")
    now_uk = datetime.datetime.now(uk)
    boundary = now_uk.replace(hour=9, minute=0, second=0, microsecond=0)
    if boundary > now_uk:
        boundary -= datetime.timedelta(days=1)
    return cached_at >= boundary.timestamp()


def _sp_s3_load() -> tuple[float, str | None, dict | None]:
    """Return (cached_at, date_iso, data) from the S3 cache, or (0.0, None, None)."""
    try:
        import boto3
        obj = boto3.client("s3", region_name="eu-west-1").get_object(Bucket=_SP_S3_BUCKET, Key=_SP_S3_KEY)
        payload = json.loads(obj["Body"].read())
        return payload.get("cached_at", 0.0), payload.get("date"), payload.get("data")
    except Exception as e:
        logger.warning("SharePoint S3 cache read failed: %s", e)
        return 0.0, None, None


def _sp_s3_save(cached_at: float, date_str: str, data: dict) -> None:
    try:
        import boto3
        boto3.client("s3", region_name="eu-west-1").put_object(
            Bucket=_SP_S3_BUCKET, Key=_SP_S3_KEY,
            Body=json.dumps({"cached_at": cached_at, "date": date_str, "data": data}).encode(),
            ContentType="application/json",
        )
    except Exception as e:
        logger.warning("SharePoint S3 cache write failed: %s", e)


def _get_sharepoint_data() -> dict:
    """Memory cache → S3 cache → SharePoint fetch → stale S3 fallback."""
    global _kpi_sp_cache, _kpi_sp_cache_at
    if _kpi_sp_cache.get("status") == "ok" and _sp_cache_is_fresh(_kpi_sp_cache_at):
        logger.info("KPI SharePoint: serving from memory cache")
        return _kpi_sp_cache

    cached_at, cache_date, cached = _sp_s3_load()
    if cached and cached.get("status") == "ok" and _sp_cache_is_fresh(cached_at):
        logger.info("KPI SharePoint: serving from S3 cache (fetched %s)", cache_date)
        _kpi_sp_cache = cached
        _kpi_sp_cache_at = cached_at
        return cached

    logger.info("KPI SharePoint: cache stale or missing, fetching from SharePoint")
    try:
        result = _build_kpi_sharepoint_data()
    except Exception as e:
        logger.error("KPI SharePoint fetch failed: %s", e)
        result = {"status": "error", "message": str(e)}

    if result.get("status") == "ok":
        now = time.time()
        _kpi_sp_cache = result
        _kpi_sp_cache_at = now
        _sp_s3_save(now, datetime.date.today().isoformat(), result)
        return result

    # SharePoint failed — fall back to the most recent S3 payload if we have one
    if cached and cached.get("status") == "ok":
        logger.warning("KPI SharePoint: fetch failed, serving stale S3 cache from %s", cache_date)
        stale = dict(cached)
        stale["stale"] = True
        stale["stale_date"] = cache_date
        return stale
    return result


def warm_sharepoint_cache() -> None:
    """Refresh the SharePoint KPI cache if stale. Called by the /home cache warmer."""
    _get_sharepoint_data()
    logger.info("Home cache warm (SharePoint KPI) complete")


def _build_kpi_sharepoint_data() -> dict:
    """Fetch Table1 and keep every number column by date. Result is cached in
    _kpi_sp_cache; /kpi/custom-data turns it into chart series per request, so
    the chart periods can change without refetching the spreadsheet."""
    client = SharePointClient()
    rows = client.get_table_rows(
        table_name="Table1",
        sharepoint_url="https://carsmkt.sharepoint.com/teams/marketing/Reports/Daily%20Spend%20and%20Enquiries.xlsx",
    )

    if not rows:
        return {"status": "error", "message": "No rows returned from SharePoint"}

    columns = list(rows[0].keys())
    date_col = next((c for c in columns if c.lower() == "date"), None)
    if not date_col:
        return {"status": "error", "message": f"Could not find a Date column. Found: {columns}"}

    def _parse_date(val) -> str | None:
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

    def _number(val):
        if isinstance(val, bool):
            return None
        if isinstance(val, (int, float)):
            return float(val)
        try:
            return float(str(val).strip())
        except (TypeError, ValueError):
            return None

    # Far enough back for the longest periods the settings allow plus their
    # N-1 a year earlier: 24 months + 12, or 104 weeks + a year of weeks.
    today = datetime.date.today()
    cutoff = today.replace(day=1)
    for _ in range(38):
        cutoff = (cutoff - datetime.timedelta(days=1)).replace(day=1)
    cutoff_str = cutoff.isoformat()

    dated = [(d, row) for d, row in ((_parse_date(row.get(date_col)), row) for row in rows)
             if d and d >= cutoff_str]

    # A number column is one where every filled cell is a number and at least
    # one cell is filled. Text columns (notes, labels) are left out.
    number_columns = []
    for col in columns:
        if col == date_col:
            continue
        filled = [row.get(col) for _, row in dated if row.get(col) not in (None, "")]
        if filled and all(_number(v) is not None for v in filled):
            number_columns.append(col)

    by_date: dict[str, dict] = {}
    for d, row in dated:
        values = {col: _number(row.get(col)) for col in number_columns if row.get(col) not in (None, "")}
        if values:
            by_date[d] = values

    return {
        "status": "ok",
        "fetched": today.isoformat(),
        "columns": number_columns,
        "by_date": by_date,
    }


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
