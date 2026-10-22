"""Assess Vehicle: single VRM + mileage lookup across CAP, Autotrader and buying requirements (admin only)."""
import logging
import threading
import requests
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse

from utils.db_utils import (
    get_database_connection, get_settings, get_vehicle_database_connection,
)
from utils.vehicle_store import (
    ensure_manual_check_columns, fetch_latest_valuation, fetch_check_history,
    fetch_recent_checks, save_manual_check, copy_check_for_mileage, fetch_cap_image, save_cap_image,
    fetch_selected_features, save_selected_features, save_spec_valuation,
    fetch_latest_valuation_id, fetch_spec_check, save_spec_check,
    fetch_vin_spec_features, save_vin_spec_features, ensure_vin_spec_seeded,
    has_confirmed_spec,
)

from .core import (BASE_URL, templates, _track, _uk_now,
                   require_page, require_page_api)
from .buying_requirements import (
    _get_sf_rows, _get_dbs_purchases_cached, _get_cb_purchases_cached,
    _get_dbs_purchases, _get_cb_purchases, _get_cb_extra,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# The list is every vehicle checked in this window, from supplier list runs and
# manual lookups alike — read straight from the shared store rather than kept
# per user, so it shows the same data the evaluator produced.
_RECENT_DAYS = 30
_RECENT_PAGE_SIZE = 10

# Data older than this is still shown, but flagged as stale with an Update
# button. Matches the 24-hour rule the vehicle store was designed around.
_STALE_AFTER_HOURS = 24

# Set once per process — the columns only need adding on the first request.
_columns_checked = False


def _plain(v):
    """Snowflake and Postgres both return Decimal, which json.dumps rejects when
    the result is written to the history store. Convert to a plain float."""
    from decimal import Decimal
    return float(v) if isinstance(v, Decimal) else v


def _ensure_columns(conn) -> None:
    global _columns_checked
    if _columns_checked:
        return
    ensure_manual_check_columns(conn)
    _columns_checked = True


def _age_hours(fetched_at) -> float | None:
    if fetched_at is None:
        return None
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=_dt.timezone.utc)
    return (now - fetched_at).total_seconds() / 3600.0


def _valued_today(fetched_at) -> bool:
    """
    True when the stored valuation was fetched on today's UK calendar day.

    CAP and Autotrader refresh their valuations overnight (around 2am), so a
    second call on the same day returns the same figures. The Update button is
    disabled while this is true.
    """
    if fetched_at is None:
        return False
    import datetime as _dt
    from zoneinfo import ZoneInfo
    uk = ZoneInfo('Europe/London')
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=_dt.timezone.utc)
    return fetched_at.astimezone(uk).date() == _dt.datetime.now(uk).date()


def _registration_year(date: str | None) -> int:
    """
    Year from a dd/mm/yyyy registration date, for the image storage key.

    0 stands for "no date given" so those rows still have a key — that request
    gets CAP's last model year, which is a different picture from a dated one
    and should not overwrite it.
    """
    if not date:
        return 0
    try:
        return int(str(date).split('/')[-1])
    except (ValueError, IndexError):
        return 0


def _stored_lookup(vrm: str, mileage: int) -> dict | None:
    """
    Rebuild a CAP/Autotrader result from the shared vehicle store.

    Returns None when there is nothing stored for this VRM at this mileage, so
    the caller falls through to the APIs. This is what makes a manual check and
    a supplier list check the same data: whichever ran last populated these
    tables, and both pages read them back.
    """
    from list_processor.cap_client import CAPClient
    rounded = CAPClient.round_to_nearest_thousand(mileage)
    try:
        with get_vehicle_database_connection() as conn:
            _ensure_columns(conn)
            val = fetch_latest_valuation(conn, vrm, mileage)
            if not val:
                logger.info(f"Assess vehicle STORE MISS: no vehicle_valuations row for vrm={vrm} mileage={mileage} rounded={rounded}")
                return None
            logger.info(
                f"Assess vehicle STORE HIT: vrm={vrm} rounded={rounded} "
                f"source={val.get('source')} fetched_at={val.get('fetched_at')} "
                f"at_retail={val.get('at_retail_valuation')} cap_clean={val.get('cap_live_clean')}"
            )
            known = _fetch_vehicle_row(conn, vrm)
            vin = known.get('at_vin')
            # Same connection — the popup always shows this, so fetching it here
            # saves a second round trip on the fast path.
            checks = _format_checks(fetch_check_history(conn, vrm))
            selected_features = fetch_selected_features(conn, vrm)
            vin_spec_features = fetch_vin_spec_features(conn, vrm)
    except Exception as e:
        logger.warning(f"Assess vehicle: could not read stored data for {vrm}: {e}")
        return None

    cap = {
        'cap_id':         known.get('cap_capid'),
        'cap_code':       known.get('cap_capcode'),
        'make':           known.get('cap_manufacturer'),
        'model':          known.get('cap_model'),
        'derivative':     known.get('cap_derivative'),
        'registered':     known.get('cap_registration_date'),
        'live_clean':     _plain(val.get('cap_live_clean')),
        'live_retail':    _plain(val.get('cap_live_retail')),
        'monthly_clean':  _plain(val.get('cap_monthly_clean')),
        'monthly_retail': _plain(val.get('cap_monthly_retail')),
        'rounded_mileage': _plain(val.get('cap_rounded_mileage')),
        'vin':            vin,
        'note':           None,
    }

    # The store keeps one description, not separate make/model/derivative, so
    # show it whole rather than inventing a split that might be wrong.
    at = {
        'found':            val.get('at_retail_valuation') is not None,
        'description':      known.get('at_description'),
        'make':             '',
        'model':            '',
        'derivative':       known.get('at_description') or '',
        'vin':              vin,
        'first_registered': str(known['at_first_registration_date']) if known.get('at_first_registration_date') else '',
        'derivative_id':    known.get('at_derivative_id'),
        'factory_fitted_features': known.get('at_factory_fitted_features'),
        'retail_valuation': _plain(val.get('at_retail_valuation')),
        'retail_rating':    _plain(val.get('at_rating')),
        'days_to_sell':     _plain(val.get('at_days_to_sell')),
        'market_condition': _plain(val.get('at_market_condition')),
        # Three figures kept side by side: average spec for the derivative, no
        # spec at all, and this car's chosen spec. The Portal compares against
        # the average, so that is what the page shows the difference from.
        'average_valuation': _plain(val.get('at_retail_valuation_average')),
        'base_valuation':    _plain(val.get('at_retail_valuation_base')),
        'spec_valuation':    _plain(val.get('at_retail_valuation_spec')),
        'spec_features':     [n for n in (val.get('at_spec_features') or '').split('|') if n],
        'spec_updated_by':   val.get('at_spec_updated_by'),
        # Current selection, and which of those (if any) came from Autotrader's
        # own VIN data rather than being picked by hand. The card highlights the
        # overlap so a VIN-spec item stays visibly flagged even if un-ticked —
        # it just moves from "fitted" to "available" but keeps its VIN badge.
        'selected_features':  selected_features,
        'vin_spec_features':  vin_spec_features,
    }

    age = _age_hours(val.get('fetched_at'))
    return {
        'cap': cap,
        'at': at,
        'checks': checks,
        'from_store': True,
        'stored_source': val.get('source') or 'evaluator',
        'stored_by': val.get('checked_by'),
        'fetched_at': val['fetched_at'].astimezone().strftime('%d/%m/%Y %H:%M') if val.get('fetched_at') else None,
        'age_hours': round(age, 1) if age is not None else None,
        'is_stale': age is not None and age > _STALE_AFTER_HOURS,
        'valued_today': _valued_today(val.get('fetched_at')),
    }


def _fetch_vehicle_row(conn, vrm: str) -> dict:
    """Identity columns plus the VIN in one query — fetch_known_vehicles predates
    the VIN column and would need a second round trip for it."""
    import psycopg2.extras
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT at_description, at_factory_fitted_features, at_first_registration_date,
                   at_derivative_id, at_vin, cap_capid, cap_capcode, cap_manufacturer,
                   cap_model, cap_derivative, cap_registration_date
            FROM vehicles WHERE vrm = %s
            """,
            (vrm,),
        )
        row = cur.fetchone()
    return dict(row) if row else {}


def _format_checks(rows: list) -> list:
    """Per-VRM check rows for the popup — supplier list runs and manual lookups."""
    out = []
    for r in rows:
        fetched = r.get('fetched_at')
        out.append({
            'id':        _plain(r.get('id')),
            'when':      fetched.astimezone().strftime('%d/%m/%Y %H:%M') if fetched else None,
            'age_hours': round(_age_hours(fetched), 1) if fetched else None,
            'source':    r.get('source') or 'evaluator',
            'by':        r.get('checked_by'),
            'supplier':  r.get('supplier_name'),
            'mileage':   _plain(r.get('mileage')),
            'at_retail': _plain(r.get('at_retail_valuation')),
            'at_rating': _plain(r.get('at_rating')),
            'cap_clean': _plain(r.get('cap_live_clean')),
            'cap_retail': _plain(r.get('cap_live_retail')),
        })
    return out


def _recent_list(days: int = _RECENT_DAYS, page: int = 1,
                 page_size: int = _RECENT_PAGE_SIZE) -> tuple[list, int]:
    """
    Every vehicle with API data in the last `days`, one row per VRM, newest
    first. Read from the shared store, so supplier list vehicles appear here
    exactly as manual checks do.
    """
    try:
        with get_vehicle_database_connection() as conn:
            _ensure_columns(conn)
            rows = fetch_recent_checks(
                conn, days=days, limit=page_size,
                offset=(page - 1) * page_size,
            )
    except Exception as e:
        logger.warning(f"Assess vehicle: could not load recent checks: {e}")
        return [], 0

    total = int(rows[0].get('total_count') or 0) if rows else 0
    out = []
    for r in rows:
        fetched = r.get('fetched_at')
        source = r.get('source') or 'evaluator'
        description = (r.get('at_description')
                       or ' '.join(x for x in (r.get('cap_manufacturer'), r.get('cap_model'),
                                               r.get('cap_derivative')) if x)
                       or '')
        out.append({
            'vrm':          r['vrm'],
            'mileage':      _plain(r.get('reported_mileage') or r.get('mileage')),
            'description':  description,
            'cap_id':       _plain(r.get('cap_capid')),
            'vin':          r.get('at_vin'),
            'looked_up':    fetched.astimezone().strftime('%d/%m/%Y %H:%M') if fetched else None,
            'age_hours':    round(_age_hours(fetched), 1) if fetched else None,
            'is_stale':     (_age_hours(fetched) or 0) > _STALE_AFTER_HOURS if fetched else False,
            'source':       source,
            'checked_by':   r.get('checked_by'),
            'supplier':     r.get('supplier_name'),
            'check_count':  _plain(r.get('check_count')),
            'cap_clean':    _plain(r.get('cap_live_clean')),
            'cap_retail':   _plain(r.get('cap_live_retail')),
            'at_retail':    _plain(r.get('at_retail_valuation')),
            'at_rating':    _plain(r.get('at_rating')),
        })
    return out, total


@router.get("/assess-vehicle", response_class=HTMLResponse)
def assess_vehicle_page(request: Request, user: str = Depends(require_page('assess_vehicle'))):
    _track(request, 'page_view', '/assess-vehicle')
    _warm_for_lookup()
    return templates.TemplateResponse(
        "assess_vehicle.html",
        {"request": request, "user": user, "BASE_URL": BASE_URL},
    )


@router.get("/assess-vehicle/vehicle/{vrm}", response_class=HTMLResponse)
def assess_vehicle_detail_page(request: Request, vrm: str, mileage: int,
                               user: str = Depends(require_page('assess_vehicle'))):
    """
    Full page for one vehicle, replacing what used to be a popup.

    A plain GET with the VRM and mileage in the URL, so the page can be
    bookmarked, refreshed and shared. It renders a shell and fetches its own
    data from /assess-vehicle/lookup/{vrm}, which reads the store rather than
    calling the APIs.
    """
    key = vrm.strip().upper().replace(' ', '')
    _track(request, 'page_view', '/assess-vehicle/vehicle', {'vrm': key})
    return templates.TemplateResponse(
        "assess_vehicle_detail.html",
        {"request": request, "user": user, "BASE_URL": BASE_URL,
         "vrm": key, "mileage": mileage},
    )


def _warm_for_lookup() -> None:
    """
    Get the slow one-off work out of the way while the page is still rendering:
    the CAP/Autotrader credentials from AWS, an Autotrader access token, the
    vehicle DB pool, and the two SharePoint sheets. Fire-and-forget — a failure
    here just means the first lookup pays the cost, as it did before.

    The SharePoint fetches are by far the slowest part (~30s cold, against ~2s
    for CAP and Autotrader together), so starting them while the user is still
    typing a VRM is what keeps the presence/purchases block from arriving long
    after everything else. _prewarm_sharepoint returns immediately when the
    caches are already fresh.
    """
    def _warm():
        def _apis():
            try:
                from list_processor.cap_client import CAPClient
                from list_processor.autotrader_client import AutoTraderClient
                CAPClient()
                AutoTraderClient().get_access_token(requests.Session())
            except Exception as e:
                logger.warning(f"Assess vehicle warm (credentials) failed: {e}")

        def _db():
            try:
                with get_vehicle_database_connection() as conn:
                    _ensure_columns(conn)
            except Exception as e:
                logger.warning(f"Assess vehicle warm (vehicle DB) failed: {e}")

        def _sharepoint():
            try:
                from .buying_requirements import _prewarm_sharepoint
                # Cars Bought is read via _get_cb_extra here rather than the
                # band-matched _get_cb_purchases (see _vehicle_presence), but
                # both come off the same cached sheet fetch, so warming it
                # covers this page too.
                _prewarm_sharepoint(enable_cars_bought=True)
            except Exception as e:
                logger.warning(f"Assess vehicle warm (SharePoint) failed: {e}")

        with ThreadPoolExecutor(max_workers=3) as ex:
            ex.submit(_apis)
            ex.submit(_db)
            ex.submit(_sharepoint)

    threading.Thread(target=_warm, name="assess-vehicle-warm", daemon=True).start()


@router.get("/assess-vehicle/lookup/{vrm}")
def assess_vehicle_cached(vrm: str, mileage: int, user: str = Depends(require_page_api('assess_vehicle'))):
    """
    Rebuild a popup for a vehicle already in the store — no API calls.

    Used when opening a row in the recent list, whether that vehicle was last
    checked by a supplier list run or a manual lookup.
    """
    key = vrm.strip().upper().replace(' ', '')
    stored = _stored_lookup(key, mileage)
    if not stored:
        raise HTTPException(status_code=404, detail="No stored data for this vehicle")

    cap, at = stored['cap'], stored['at']
    return {'result': _build_result(
        key, mileage, cap, at, stored['checks'],
        {
            'from_store': True,
            'source':     stored['stored_source'],
            'checked_by': stored['stored_by'],
            'fetched_at': stored['fetched_at'],
            'age_hours':  stored['age_hours'],
            'is_stale':   stored['is_stale'],
            'valued_today': stored['valued_today'],
        },
        user,
    )}


def _cap_lookup(vrm: str, mileage: int) -> dict:
    """CAP VRM valuation plus the live valuation for the same CAP ID."""
    from list_processor.cap_client import CAPClient
    from list_processor.structs import VehicleRow

    cap = CAPClient()
    vehicle = VehicleRow(vrm=vrm, mileage=mileage)
    data = cap.vrm_valuation(vehicle)

    def _num(v):
        try:
            return float(str(v).replace(',', '').strip())
        except (TypeError, ValueError):
            return None

    try:
        cap_id = int(data['capid']) if data.get('capid') and data['capid'] != 'Not Found' else None
    except (TypeError, ValueError):
        cap_id = None

    out = {
        'cap_id':         cap_id,
        'cap_code':       data.get('capcode'),
        'make':           data.get('capman'),
        'model':          data.get('capmod'),
        'derivative':     data.get('capder'),
        'registered':     data.get('registered_date'),
        'monthly_clean':  _num(data.get('clean')),
        'monthly_retail': _num(data.get('retail')),
        'live_clean':     None,
        'live_retail':    None,
        # The evaluator stores the mileage CAP actually valued at (rounded to the
        # nearest 1000), so record it here too — the shared store keys on it.
        'rounded_mileage': CAPClient.round_to_nearest_thousand(mileage),
        'note':           data.get('note'),
    }

    # Live values need a CAP ID and registration date, which only come back for cars.
    if cap_id and data.get('database') == 'CAR' and data.get('registered_date'):
        reg_date = cap.convert_date_format(data['registered_date'])
        if reg_date:
            vehicle.capid = cap_id
            vehicle.registration_date = reg_date
            try:
                live_retail, live_clean = cap.live_valuation(vehicle)
                out['live_retail'] = cap.round_to_nearest_25(live_retail) if live_retail is not None else None
                out['live_clean'] = cap.round_to_nearest_25(live_clean) if live_clean is not None else None
            except Exception as e:
                logger.warning(f"Assess vehicle: CAP live valuation failed for {vrm}: {e}")

    return out


def _at_lookup(vrm: str, mileage: int, spec_confirmed: bool = False) -> dict:
    """
    Autotrader vehicle details, retail valuation and retail rating.

    `spec_confirmed` distinguishes "nobody has set a spec for this vehicle yet"
    from "checked and there genuinely are no extras". Autotrader's /valuations
    with an empty feature list returns the BASE price (no extras at all), which
    the Portal itself shows as below the derivative's average — verified on
    DA66VGN: average 11294, base 11107. Until a spec has actually been
    confirmed (VIN spec found, or a manual Apply — see has_confirmed_spec),
    there are no known extras, not zero extras, so the average is the closer
    match to what the Portal displays. Only once confirmed empty should the
    lower base price be shown.
    """
    from list_processor.autotrader_client import (
        AutoTraderClient, _unwrap_vehicles_result, _valuation_features_payload,
    )

    at = AutoTraderClient()
    session = requests.Session()
    headers = {'Authorization': f'Bearer {at.get_access_token(session)}'}

    vehicles_data = at.send_vehicles_post_request(session, headers, vrm, mileage)
    if not vehicles_data:
        return {'found': False}

    result = _unwrap_vehicles_result(vehicles_data)
    info = result.get('vehicle', {})
    metrics = result.get('vehicleMetrics', {}).get('retail', {})
    all_features = result.get('features', [])

    derivative_id = info.get('derivativeId', '')
    first_reg = info.get('firstRegistrationDate', '')

    # VIN-detected extras only — for display/storage, not the valuation call.
    fitted_features = [
        {"name": f['name'].strip()}
        for f in all_features
        if f.get('factoryFitted') is True and f.get('name')
    ]

    average_valuation = result.get('valuations', {}).get('retail', {}).get('amountGBP')

    retail_valuation = None
    if fitted_features or spec_confirmed:
        # Either there is known spec to price with, or the absence of spec has
        # already been confirmed deliberate — either way /valuations gives the
        # right figure to show. The payload must be every Standard feature plus
        # the fitted Optional ones — /valuations treats `features` as the full
        # spec of the car, not extras layered on a base price (see
        # _valuation_features_payload); sending only fitted_features here
        # silently returns the same figure as no spec at all.
        if derivative_id and first_reg and mileage:
            valuations = at.send_valuations_post_request(
                session, headers, vrm, derivative_id, first_reg, mileage,
                _valuation_features_payload(all_features)
            )
            if valuations:
                retail_valuation = valuations.get('valuations', {}).get('retail', {}).get('amountGBP')

    # Fall back to the average (matches what the Portal shows before any spec
    # is entered) when there's no confirmed spec, or the feature-adjusted call
    # could not be made.
    if retail_valuation is None:
        retail_valuation = average_valuation

    make = info.get('make', '')
    model = info.get('model', '')
    derivative = info.get('derivative', '')

    return {
        'found':            True,
        'make':             make,
        'model':            model,
        'derivative':       derivative,
        'vin':              info.get('vin', ''),
        'first_registered': first_reg,
        'retail_valuation': retail_valuation,
        'retail_rating':    metrics.get('rating', {}).get('value'),
        'days_to_sell':     metrics.get('daysToSell', {}).get('value'),
        'market_condition': metrics.get('marketCondition', {}).get('value'),
        # Stored by the evaluator on the vehicles table. Kept in the same shape
        # it writes so a manual check fills those columns identically.
        'description':      AutoTraderClient.concatenate_description(make, model, derivative),
        'derivative_id':    derivative_id or None,
        'factory_fitted_features': ', '.join(f['name'] for f in fitted_features) or None,
        # Same list, kept as plain names rather than the display string above —
        # this is what seeds the spec selection and the VIN-spec record on a
        # vehicle's first check (see ensure_vin_spec_seeded/save_vin_spec_features).
        'vin_spec_feature_names': [f['name'] for f in fitted_features],
    }


# Price band boundaries, matching _rrp_to_price_band in buying_requirements.
def _band_for_value(v) -> str | None:
    from .buying_requirements import _rrp_to_price_band
    if v is None:
        return None
    return _rrp_to_price_band(v)


def _buying_requirements(cap_id: int, price_band: str | None, number_to_buy: int) -> dict:
    """
    Stock, sales and buying figures for a CAP ID.

    Ideal Stock is the same calculation the Buying Requirements table uses: the
    CAP's share of 90-day sales applied to (total current stock + number to buy).
    That needs the totals across every row, so the whole table is aggregated here
    rather than just the one CAP ID.
    """
    rows = _get_sf_rows() or []
    if not rows:
        return {'found': False}

    # rows: [0:cap_id, 3:price_band, 4:stock, 5:sales90, 6:sales7,
    #        17:order_acknowledged_date, 35:is_advertised]
    import datetime
    today = datetime.datetime.now(datetime.timezone.utc)

    total_stock = 0
    total_sales90 = 0
    # Per (cap_id, band) so Ideal Stock is computed on the same grouping the
    # Buying Requirements table uses.
    this_stock = this_on_sale = this_sales90 = this_sales30 = this_sales7 = 0
    this_key_sales90 = 0

    bands_seen = {}

    for r in rows:
        stock = r[4] or 0
        sales90 = r[5] or 0
        total_stock += stock
        total_sales90 += sales90

        if r[0] != cap_id:
            continue

        band = r[3]
        b = bands_seen.setdefault(band, {'stock': 0, 'sales90': 0})
        b['stock'] += stock
        b['sales90'] += sales90

        # When no band is given, count every band for this CAP ID.
        if price_band is not None and band != price_band:
            continue

        this_stock += stock
        this_sales90 += sales90
        this_sales7 += (r[6] or 0)
        this_key_sales90 += sales90
        if len(r) > 35 and r[35] == 1:
            this_on_sale += 1

        # 30-day sales need the acknowledged date, as in the table's aggregation.
        if sales90 == 1 and r[17]:
            try:
                ack = datetime.datetime.fromisoformat(str(r[17]).replace('Z', '+00:00'))
                if ack.tzinfo is None:
                    ack = ack.replace(tzinfo=datetime.timezone.utc)
                if (today - ack).days <= 30:
                    this_sales30 += 1
            except (TypeError, ValueError):
                pass

    if not bands_seen:
        return {'found': False}

    ideal_total = total_stock + number_to_buy
    ideal = (ideal_total * this_key_sales90 / total_sales90) if (this_key_sales90 > 0 and total_sales90 > 0) else 0.0
    required = ideal - this_stock

    return {
        'found':        True,
        'stock':        int(this_stock),
        'stock_on_sale': int(this_on_sale),
        'sales90':      int(this_sales90),
        'sales30':      int(this_sales30),
        'sales7':       int(this_sales7),
        'ideal':        round(float(ideal), 1),
        'required':     round(float(required), 1),
        'bands':        sorted(bands_seen.keys()),
    }


def _vehicle_presence(vrm: str, cap_id: int | None) -> dict:
    """Where this VRM already appears: Apex (Snowflake), Daily Buyers Sheet, or Cars Bought."""
    presence = {'apex': None, 'dbs': None, 'cb': None}

    rows = _get_sf_rows() or []
    for r in rows:
        if len(r) > 18 and r[18] and str(r[18]).strip().upper().replace(' ', '') == vrm:
            presence['apex'] = {
                'vrm':            r[18],
                'cap_id':         _plain(r[0]),
                'price_band':     r[3],
                'supplier':       r[19] if len(r) > 19 else None,
                'vehicle_status': r[21] if len(r) > 21 else None,
                'location':       r[23] if len(r) > 23 else None,
                'mileage':        _plain(r[25]) if len(r) > 25 else None,
                'purchase_price': _plain(r[12]),
                'selling_price':  _plain(r[13]),
            }
            break

    def _find(cached, key):
        if not cached:
            return None
        _purchases, vehicles = cached
        for v in (vehicles or []):
            if str(v.get('vrm', '')).strip().upper().replace(' ', '') == vrm:
                return v
        return None

    # Cached-only reads (memory, then S3) come back with nothing whenever this
    # worker process has never visited the Buying Requirements page — only that
    # page prewarms these caches, and neither read here ever builds one. That
    # showed a genuinely-listed vehicle as "not found". Falling back to the
    # build-capable function fixes it: it still prefers memory, then S3, and
    # only pays for a live SharePoint + Snowflake fetch as a last resort.
    try:
        cached = _get_dbs_purchases_cached(quiet=True)
        if cached is None:
            _get_dbs_purchases()
            cached = _get_dbs_purchases_cached(quiet=True)
        presence['dbs'] = _find(cached, 'dbs')
    except Exception as e:
        logger.warning(f"Assess vehicle: Daily Buyers Sheet check failed for {vrm}: {e}")

    # Deliberately NOT _get_cb_purchases()/_get_cb_purchases_cached() here — that
    # path requires an exact (cap_id, band) match against Snowflake's own
    # buying_requirements bands, which the Buying Requirements page's counts
    # correctly depend on. But it silently drops a vehicle whose sheet-derived
    # band differs from Snowflake's band for the same CAP ID (confirmed on
    # YY22NLU: CAP ID 99465, sheet RRP bands it 20-25k, Snowflake's only row for
    # that CAP ID is 25-30k — genuinely on the sheet, dropped as "not in
    # Snowflake"). Assess Vehicle just needs "is this VRM on TO GO ON at all",
    # so it reads _get_cb_extra() instead — every VRM on the sheet before any
    # Apex dedup or band matching, which is what the row-level SPEC/MILEAGE/
    # BUYERS/SUPPLIER columns are built from anyway.
    try:
        extra = _get_cb_extra()
        row = extra.get(vrm)
        # This is the raw sheet row, before the band-matching that _get_cb_purchases
        # does — no cap_id/band/siv/rrp here, only what the sheet itself carries
        # for every row regardless of Snowflake. mileage/date come back as the
        # sheet's own text values.
        presence['cb'] = {
            'vrm':      vrm,
            'supplier': row.get('supplier'),
            'buyer':    row.get('buyer'),
            'mileage':  row.get('mileage'),
            'date':     row.get('date'),
        } if row else None
    except Exception as e:
        logger.warning(f"Assess vehicle: Cars Bought check failed for {vrm}: {e}")

    return presence


def _purchase_counts(cap_id: int, price_band: str | None) -> dict:
    """Daily Buyers Sheet and Cars Bought counts for this CAP ID (and band, if known)."""
    def _count(cached):
        if not cached:
            return None
        purchases, _vehicles = cached
        total = 0
        for k, v in (purchases or {}).items():
            if isinstance(k, tuple):
                k_cap, k_band = k
            else:
                cap_str, _, k_band = str(k).partition('|')
                try:
                    k_cap = int(cap_str)
                except (TypeError, ValueError):
                    k_cap = cap_str
            if k_cap != cap_id:
                continue
            if price_band is not None and k_band != price_band:
                continue
            total += v
        return total

    # Same fallback as _vehicle_presence: a cache-only read can be genuinely
    # empty if this worker has never visited the Buying Requirements page, so
    # fall back to the build-capable function rather than reporting zero.
    dbs = cb = None
    try:
        cached = _get_dbs_purchases_cached(quiet=True)
        if cached is None:
            _get_dbs_purchases()
            cached = _get_dbs_purchases_cached(quiet=True)
        dbs = _count(cached)
    except Exception as e:
        logger.warning(f"Assess vehicle: DBS purchase count failed: {e}")
    try:
        cached = _get_cb_purchases_cached(quiet=True)
        if cached is None:
            _get_cb_purchases()
            cached = _get_cb_purchases_cached(quiet=True)
        cb = _count(cached)
    except Exception as e:
        logger.warning(f"Assess vehicle: CB purchase count failed: {e}")
    return {'dbs': dbs, 'cb': cb}


def _build_result(vrm: str, mileage: int, cap: dict, at: dict,
                  checks: list, provenance: dict, user: str) -> dict:
    """
    The CAP/Autotrader half of a vehicle result, whether that came from the APIs
    or from the store.

    Deliberately does NOT include presence or buying requirements — both need
    the SharePoint sheets, which take ~30s on a cold cache against ~2s for CAP
    and Autotrader. The page fetches those separately from
    /assess-vehicle/market/{vrm} and fills them in when they arrive, so the
    valuation is on screen immediately rather than waiting on Excel.
    """
    # CAP's VRM lookup does not return a VIN, but Autotrader's /vehicles does.
    # Show it on the CAP block, which is where it was asked for.
    if not cap.get('vin') and at.get('vin'):
        cap['vin'] = at['vin']

    return {
        'vrm':        vrm,
        'mileage':    mileage,
        'looked_up':  provenance['fetched_at'],
        'provenance': provenance,
        'checks':     checks,
        'cap':        cap,
        'autotrader': at,
    }


def _build_market(vrm: str, cap_id, retail_valuation, user: str) -> dict:
    """
    The SharePoint/Snowflake half: where this vehicle already appears, and what
    the buying requirements say for its CAP ID.

    Split out of _build_result so it can be fetched on its own — see the note
    there. `retail_valuation` is only used to derive a price band when the
    vehicle is not already on Apex/DBS/Cars Bought.
    """
    try:
        with get_database_connection() as conn:
            settings = get_settings(conn, user=user)
        number_to_buy = int(settings.get('number_to_buy', 0) or 0)
    except Exception as e:
        logger.warning(f"Assess vehicle: could not read number_to_buy setting: {e}")
        number_to_buy = 0

    presence = _vehicle_presence(vrm, cap_id)

    # Prefer the band this exact vehicle already sits in; otherwise derive one
    # from the Autotrader retail valuation so the figures match a comparable band.
    price_band = None
    if presence['apex']:
        price_band = presence['apex'].get('price_band')
    elif presence['dbs']:
        price_band = presence['dbs'].get('band')
    elif presence['cb']:
        price_band = presence['cb'].get('band')
    elif retail_valuation:
        price_band = _band_for_value(retail_valuation)

    br = {'found': False}
    counts = {'dbs': None, 'cb': None}
    if cap_id:
        try:
            br = _buying_requirements(cap_id, price_band, number_to_buy)
        except Exception as e:
            logger.error(f"Assess vehicle: buying requirements lookup failed for {cap_id}: {e}")
            br = {'found': False, 'error': str(e)}
        try:
            counts = _purchase_counts(cap_id, price_band)
        except Exception as e:
            logger.warning(f"Assess vehicle: purchase counts failed for {cap_id}: {e}")

    if br.get('found'):
        dbs = counts.get('dbs') or 0
        cb = counts.get('cb') or 0
        br['dbs'] = counts.get('dbs')
        br['cb'] = counts.get('cb')
        br['remaining'] = round(float(br['required']) - dbs - cb, 1)
        br['price_band'] = price_band
        br['number_to_buy'] = number_to_buy

    return {'presence': presence, 'buying': br}


@router.get("/assess-vehicle/market/{vrm}")
def assess_vehicle_market(vrm: str, cap_id: int = None, retail_valuation: float = None,
                          user: str = Depends(require_page_api('assess_vehicle'))):
    """
    Presence and buying requirements for a vehicle — the SharePoint half.

    Called by the page after the valuation is already on screen, so the ~30s
    cold SharePoint fetch never delays the CAP/Autotrader figures. The sheets
    are warmed when the page opens (see _warm_for_lookup), so in normal use
    this returns from cache.
    """
    key = vrm.strip().upper().replace(' ', '')
    return _build_market(key, cap_id, retail_valuation, user)


@router.post("/assess-vehicle/lookup")
def assess_vehicle_lookup(request: Request, payload: dict, user: str = Depends(require_page_api('assess_vehicle'))):

    vrm = str(payload.get('vrm') or '').strip().upper().replace(' ', '')
    if not vrm:
        raise HTTPException(status_code=400, detail="VRM is required")
    try:
        mileage = int(float(str(payload.get('mileage') or 0)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Mileage must be a number")
    if mileage <= 0:
        raise HTTPException(status_code=400, detail="Mileage is required")

    force = bool(payload.get('force'))
    force_mileage = bool(payload.get('force_mileage'))
    _track(request, 'assess_vehicle', '/assess-vehicle', {'vrm': vrm, 'force': force})

    # Show what the store already holds rather than paying for the APIs again.
    # A supplier list run and a manual check write the same rows, so this hits
    # whether the VRM was last seen on a list or looked up here.
    stored = _stored_lookup(vrm, mileage)
    if force and stored and (force_mileage or not stored['valued_today']):
        # A forced update is only worth an API call once the overnight refresh
        # has run. Same day means the same figures come back.
        stored = None

    if stored:
        cap, at = stored['cap'], stored['at']
        checks = stored['checks']
        provenance = {
            'from_store':    True,
            'source':        stored['stored_source'],
            'checked_by':    stored['stored_by'],
            'fetched_at':    stored['fetched_at'],
            'age_hours':     stored['age_hours'],
            'is_stale':      stored['is_stale'],
            'valued_today':  stored['valued_today'],
        }
    else:
        try:
            with get_vehicle_database_connection() as conn:
                _ensure_columns(conn)
                spec_confirmed = has_confirmed_spec(conn, vrm)
        except Exception as e:
            logger.warning(f"Assess vehicle: could not read spec confirmation state for {vrm}: {e}")
            spec_confirmed = False

        # CAP and Autotrader are independent, so run them together.
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_cap = ex.submit(_cap_lookup, vrm, mileage)
            f_at = ex.submit(_at_lookup, vrm, mileage, spec_confirmed)
            try:
                cap = f_cap.result()
            except Exception as e:
                logger.error(f"Assess vehicle: CAP lookup failed for {vrm}: {e}")
                cap = {'error': str(e)}
            try:
                at = f_at.result()
            except Exception as e:
                logger.error(f"Assess vehicle: Autotrader lookup failed for {vrm}: {e}")
                at = {'error': str(e)}

        provenance = {
            'from_store': False,
            'source':     'manual',
            'checked_by': user,
            'fetched_at': _uk_now().strftime('%d/%m/%Y %H:%M'),
            'age_hours':  0.0,
            'is_stale':   False,
            'valued_today': True,
        }

        # Write into the shared store so the evaluator picks this up as a cache
        # hit, exactly as if the vehicle had come through a supplier list.
        checks = []
        if not cap.get('error') and not at.get('error'):
            try:
                with get_vehicle_database_connection() as conn:
                    _ensure_columns(conn)
                    save_manual_check(conn, vrm, mileage, user, cap, at)
                    # Autotrader's own VIN spec, if it has any, is recorded
                    # every time (it can only get more accurate) and used as
                    # the starting selection on a vehicle's first ever check.
                    # Never overwrites a manual choice made since.
                    vin_features = at.get('vin_spec_feature_names') or []
                    if vin_features:
                        save_vin_spec_features(conn, vrm, vin_features)
                        ensure_vin_spec_seeded(conn, vrm, vin_features, 'autotrader_vin')
                    at['selected_features'] = fetch_selected_features(conn, vrm)
                    at['vin_spec_features'] = fetch_vin_spec_features(conn, vrm)
                    # Read back in the same connection so the popup shows this
                    # check alongside any earlier ones.
                    checks = _format_checks(fetch_check_history(conn, vrm))
            except Exception as e:
                logger.error(f"Assess vehicle: could not save check for {vrm}: {e}")

    result = _build_result(vrm, mileage, cap, at, checks, provenance, user)
    return {'result': result, 'history': _recent_list()[0]}


@router.post("/assess-vehicle/reassess-mileage")
def assess_vehicle_reassess_mileage(payload: dict, user: str = Depends(require_page_api('assess_vehicle'))):
    """Save a same-bucket mileage correction, or request a fresh valuation."""
    vrm = str(payload.get('vrm') or '').strip().upper().replace(' ', '')
    try:
        previous_mileage = int(float(str(payload.get('previous_mileage') or 0)))
        mileage = int(float(str(payload.get('mileage') or 0)))
        check_id = int(payload['check_id']) if payload.get('check_id') else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Mileage must be a number")
    if not vrm or previous_mileage <= 0 or mileage <= 0:
        raise HTTPException(status_code=400, detail="VRM and mileage are required")

    from utils.vehicle_store import round_mileage_to_nearest_thousand
    refresh_required = (
        round_mileage_to_nearest_thousand(previous_mileage)
        != round_mileage_to_nearest_thousand(mileage)
    )
    if refresh_required:
        return {'refresh_required': True}

    with get_vehicle_database_connection() as conn:
        _ensure_columns(conn)
        if not copy_check_for_mileage(conn, vrm, previous_mileage, mileage, user, check_id):
            raise HTTPException(status_code=404, detail="The original valuation is no longer available")
    return {'refresh_required': False}


# One stored size, used for both the thumbnail and the full-size popup. The
# thumbnail is just this scaled down by the browser, which costs nothing at this
# file size (~42KB) and avoids fetching and storing the same picture twice.
_IMAGE_WIDTH = 600
_IMAGE_HEIGHT = 448


@router.get("/assess-vehicle/image/{cap_id}")
def assess_vehicle_image(cap_id: int, date: str = None,
                         user: str = Depends(require_page_api('assess_vehicle'))):
    """
    CAP model image for a CAP id, streamed through the app.

    Proxied rather than linked directly because CAP's image API needs the
    subscriber password on every request — the GET form takes an MD5 hash
    instead, but CAP's documented recipe for it does not work.

    `date` is the vehicle's registration date (dd/mm/yyyy) and picks the model
    year that was current then, so a facelifted model shows the right one.

    Always 600x448. Size is deliberately not a parameter: the stored image is
    keyed on CAP id and registration year only, so a second size would either
    overwrite the first or need its own row for no real benefit.

    404s when there is no image, which is what the <img> onerror in the page
    hides itself on. Cached for a day — CAP model images do not change.
    """

    from fastapi.responses import Response
    from list_processor.cap_client import CAPClient

    def _serve(content, content_type):
        return Response(
            content=bytes(content),
            media_type=content_type or 'image/jpeg',
            headers={'Cache-Control': 'public, max-age=86400'},
        )

    # Images are keyed on CAP id + registration year, not VRM: every car of the
    # same model shares one picture, and the year is only in the key because
    # CAP's image changes across a facelift.
    year = _registration_year(date)

    stored = None
    try:
        with get_vehicle_database_connection() as conn:
            _ensure_columns(conn)
            stored = fetch_cap_image(conn, cap_id, year)
    except Exception as e:
        logger.warning(f"Assess vehicle: could not read stored image for {cap_id}: {e}")

    if stored:
        if stored['image'] is None:
            # CAP already told us there is no picture for this one.
            raise HTTPException(status_code=404, detail="No image available")
        return _serve(stored['image'], stored['content_type'])

    try:
        content, content_type = CAPClient().vehicle_image(
            cap_id, registration_date=date, width=_IMAGE_WIDTH, height=_IMAGE_HEIGHT
        )
    except Exception as e:
        logger.warning(f"Assess vehicle: CAP image request failed for {cap_id}: {e}")
        raise HTTPException(status_code=404, detail="No image available")

    # Store the answer either way — a "no image" result is worth remembering so
    # the same vehicle does not hit CAP on every popup open.
    try:
        with get_vehicle_database_connection() as conn:
            save_cap_image(conn, cap_id, year, content, content_type,
                           _IMAGE_WIDTH, _IMAGE_HEIGHT)
    except Exception as e:
        logger.warning(f"Assess vehicle: could not store image for {cap_id}: {e}")

    if not content:
        raise HTTPException(status_code=404, detail="No image available")

    return _serve(content, content_type)


@router.get("/assess-vehicle/features/{vrm}")
def assess_vehicle_features(vrm: str, mileage: int, user: str = Depends(require_page_api('assess_vehicle'))):
    """
    The optional extras that can be fitted to this vehicle, plus whatever spec
    has already been chosen for it.

    Autotrader does resolve VIN spec for some vehicles (factoryFitted: true on
    genuinely fitted extras) but not others — it depends on whether the VIN is
    in Autotrader's data for that manufacturer, not on our account. When it is
    available and nothing has been manually saved yet, seed the selection from
    it rather than starting blank — the buyer would otherwise have to notice
    and re-tick extras Autotrader already knows about. A manual save always
    wins after that: this only fills a genuinely empty selection.
    """

    import requests
    from list_processor.autotrader_client import (
        AutoTraderClient, _unwrap_vehicles_result, optional_features,
    )

    key = vrm.strip().upper().replace(' ', '')
    at = AutoTraderClient()
    session = requests.Session()
    headers = {'Authorization': f'Bearer {at.get_access_token(session)}'}

    data = at.send_vehicles_post_request(session, headers, key, mileage)
    if not data:
        raise HTTPException(status_code=404, detail="Vehicle not found on Autotrader")

    result = _unwrap_vehicles_result(data)
    all_features = result.get('features', [])
    features = optional_features(all_features)
    vin_fitted = [
        f['name'].strip() for f in all_features
        if f.get('factoryFitted') is True and f.get('name')
    ]

    selected = []
    has_manual_save = False
    try:
        with get_vehicle_database_connection() as conn:
            _ensure_columns(conn)
            selected = fetch_selected_features(conn, key)
            has_manual_save = bool(selected)
    except Exception as e:
        logger.warning(f"Assess vehicle: could not read selected features for {key}: {e}")

    from_vin = False
    if not has_manual_save and vin_fitted:
        selected = vin_fitted
        from_vin = True

    return {
        'features': features, 'selected': selected, 'from_vin_spec': from_vin,
        # Every VIN-spec name, regardless of current selection — the picker
        # badges these so a buyer can tell "Autotrader confirmed this" apart
        # from "picked by hand", the same distinction the vehicle card shows.
        'vin_features': vin_fitted,
    }


@router.post("/assess-vehicle/features/{vrm}")
def assess_vehicle_save_features(vrm: str, payload: dict,
                                 user: str = Depends(require_page_api('assess_vehicle'))):
    """
    Save the chosen spec and re-value with it.

    The spec is stored on the vehicle, so it carries to every later valuation of
    this VRM. Both figures are written to the valuation row — the base with no
    extras and the spec-adjusted one — because the comparison between them is
    the point.

    This deliberately ignores the once-a-day valuation rule: the inputs changed,
    not just the date, so a fresh call is the only way to get a figure that
    reflects the new spec.
    """

    import requests
    from list_processor.autotrader_client import AutoTraderClient, _unwrap_vehicles_result

    key = vrm.strip().upper().replace(' ', '')
    try:
        mileage = int(float(str(payload.get('mileage') or 0)))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Mileage must be a number")
    if mileage <= 0:
        raise HTTPException(status_code=400, detail="Mileage is required")

    features = payload.get('features') or []
    if not isinstance(features, list):
        raise HTTPException(status_code=400, detail="features must be a list of names")

    at = AutoTraderClient()
    session = requests.Session()
    headers = {'Authorization': f'Bearer {at.get_access_token(session)}'}

    data = at.send_vehicles_post_request(session, headers, key, mileage)
    if not data:
        raise HTTPException(status_code=404, detail="Vehicle not found on Autotrader")
    result = _unwrap_vehicles_result(data)
    info = result.get('vehicle', {})
    derivative_id = info.get('derivativeId')
    first_reg = info.get('firstRegistrationDate')
    if not derivative_id or not first_reg:
        raise HTTPException(status_code=422,
                            detail="Autotrader has no derivative or registration date for this vehicle")

    # The inline figure is the average-spec valuation, which is what the Portal
    # measures its "above/below average" against.
    average = result.get('valuations', {}).get('retail', {}).get('amountGBP')

    average, base, with_spec = at.valuation_with_features(
        session, headers, key, derivative_id, first_reg, mileage, features,
        all_features=result.get('features', []),
        average_valuation=average,
    )

    try:
        with get_vehicle_database_connection() as conn:
            _ensure_columns(conn)
            save_selected_features(conn, key, features, user)
            valuation_id = fetch_latest_valuation_id(conn, key, mileage)
            if valuation_id:
                save_spec_valuation(conn, valuation_id, average, base, with_spec,
                                    features, user)
    except Exception as e:
        logger.error(f"Assess vehicle: could not save spec for {key}: {e}")
        raise HTTPException(status_code=500, detail="Could not save the spec")

    # Against the average, matching how the Portal states it.
    current = with_spec if with_spec is not None else base
    uplift = (current - average) if (current is not None and average is not None) else None
    return {
        'average_valuation': average,
        'base_valuation':    base,
        'spec_valuation':    with_spec,
        'uplift':            uplift,
        'features':          features,
    }


def _spec_check_payload(row: dict) -> dict:
    """Shape a stored spec_checks row for the page."""
    options = row.get('options') or []
    optional = [o for o in options if o.get('option_kind') == 'Optional']
    standard = [o for o in options if o.get('option_kind') != 'Optional']
    return {
        'found':          True,
        'available':      row.get('status_code') == 1,
        'blocked':        row.get('status_code') == 120,
        'status_code':    row.get('status_code'),
        'status_message': row.get('status_message'),
        'base_price':     _plain(row.get('base_price')),
        'options_price':  _plain(row.get('options_price')),
        'total_price':    _plain(row.get('total_price')),
        'options_rating': _plain(row.get('options_rating')),
        'optional':       optional,
        'standard':       standard,
        'fetched_at':     row['fetched_at'].strftime('%d/%m/%Y %H:%M') if row.get('fetched_at') else None,
        'fetched_by':     row.get('fetched_by'),
    }


@router.get("/assess-vehicle/spec-check/{vrm}")
def assess_vehicle_spec_check(vrm: str, user: str = Depends(require_page_api('assess_vehicle'))):
    """
    The stored HPI Spec Check for this VRM, if one has ever been run.

    Reads the store only — never calls HPI. Returns found=false when there is
    nothing stored, which is what puts the "Run Spec Check" button on the page.
    """

    key = vrm.strip().upper().replace(' ', '')
    try:
        with get_vehicle_database_connection() as conn:
            _ensure_columns(conn)
            row = fetch_spec_check(conn, key)
    except Exception as e:
        logger.warning(f"Assess vehicle: could not read spec check for {key}: {e}")
        return {'found': False}

    if not row:
        return {'found': False}
    return _spec_check_payload(row)


@router.post("/assess-vehicle/spec-check/{vrm}")
def assess_vehicle_run_spec_check(vrm: str, user: str = Depends(require_page_api('assess_vehicle'))):
    """
    Run an HPI Spec Check for this VRM. BILLABLE — one HPI check per call.

    Only ever reached from the button on the Assess Vehicle page, after the user
    confirms the cost. Never called from the supplier evaluator or any bulk path.

    A stored result short-circuits the call, so pressing the button twice cannot
    bill twice. Factory options never change, so there is no refresh case: if a
    result needs replacing, the row has to be deleted deliberately.
    """

    from list_processor.spec_check_client import SpecCheckClient, SpecCheckError

    key = vrm.strip().upper().replace(' ', '')

    with get_vehicle_database_connection() as conn:
        _ensure_columns(conn)
        existing = fetch_spec_check(conn, key)
        if existing:
            logger.info("[HPI] Spec Check for %s already stored — not re-requesting", key)
            payload = _spec_check_payload(existing)
            payload['from_store'] = True
            return payload

    try:
        result = SpecCheckClient().spec_check(key)
    except SpecCheckError as e:
        logger.error(f"Assess vehicle: spec check failed for {key}: {e}")
        raise HTTPException(status_code=502, detail=str(e))

    # Stored whatever the status — a 103 "no data for this vehicle" is a real
    # answer, and remembering it is what stops the same car being billed again.
    try:
        with get_vehicle_database_connection() as conn:
            save_spec_check(conn, key, result, user)
            row = fetch_spec_check(conn, key)
    except Exception as e:
        # The call has already been paid for, so return the data rather than
        # failing — but say loudly that it did not get stored.
        logger.error(f"Assess vehicle: spec check for {key} could not be stored: {e}")
        optional = [o for o in result['options'] if o.get('option_kind') == 'Optional']
        return {
            'found': True, 'available': result['available'],
            'blocked': result['blocked_for_make'],
            'status_code': result['status_code'], 'status_message': result['status_message'],
            'base_price': result['base_price'], 'options_price': result['options_price'],
            'total_price': result['total_price'], 'options_rating': result['options_rating'],
            'optional': optional,
            'standard': [o for o in result['options'] if o.get('option_kind') != 'Optional'],
            'fetched_at': None, 'fetched_by': user, 'store_failed': True,
        }

    return _spec_check_payload(row)


@router.get("/assess-vehicle/history")
def assess_vehicle_history(page: int = 1,
                           user: str = Depends(require_page_api('assess_vehicle'))):
    """Every vehicle checked in the window — supplier lists and manual checks."""
    # Keep the window sane — an arbitrary value would scan the whole table.
    page = max(1, int(page))
    history, total = _recent_list(days=_RECENT_DAYS, page=page)
    total_pages = max(1, (total + _RECENT_PAGE_SIZE - 1) // _RECENT_PAGE_SIZE)
    return {
        'history': history,
        'days': _RECENT_DAYS,
        'page': page,
        'page_size': _RECENT_PAGE_SIZE,
        'total': total,
        'total_pages': total_pages,
    }
