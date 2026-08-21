"""Assess Vehicle: single VRM + mileage lookup across CAP, Autotrader and buying requirements (admin only)."""
import logging
import threading
import requests
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from utils.db_utils import (
    get_database_connection, get_settings, get_vehicle_database_connection,
)
from utils.vehicle_store import (
    ensure_manual_check_columns, fetch_latest_valuation, fetch_check_history,
    fetch_recent_checks, save_manual_check,
)

from .core import (BASE_URL, templates, _track, get_current_user, _uk_now)
from .buying_requirements import (
    _get_sf_rows, _get_dbs_purchases_cached, _get_cb_purchases_cached,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# The list is every vehicle checked in this window, from supplier list runs and
# manual lookups alike — read straight from the shared store rather than kept
# per user, so it shows the same data the evaluator produced.
_RECENT_DAYS = 30
_RECENT_LIMIT = 500

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


def _recent_list(days: int = _RECENT_DAYS) -> list:
    """
    Every vehicle with API data in the last `days`, one row per VRM, newest
    first. Read from the shared store, so supplier list vehicles appear here
    exactly as manual checks do.
    """
    try:
        with get_vehicle_database_connection() as conn:
            _ensure_columns(conn)
            rows = fetch_recent_checks(conn, days=days, limit=_RECENT_LIMIT)
    except Exception as e:
        logger.warning(f"Assess vehicle: could not load recent checks: {e}")
        return []

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
            'mileage':      _plain(r.get('mileage')),
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
    return out


@router.get("/assess-vehicle", response_class=HTMLResponse)
def assess_vehicle_page(request: Request, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        return RedirectResponse(url=BASE_URL + "/home", status_code=303)
    _track(request, 'page_view', '/assess-vehicle')
    _warm_for_lookup()
    return templates.TemplateResponse(
        "assess_vehicle.html",
        {"request": request, "user": user, "BASE_URL": BASE_URL},
    )


def _warm_for_lookup() -> None:
    """
    Get the slow one-off work out of the way while the page is still rendering:
    the CAP/Autotrader credentials from AWS, an Autotrader access token, and the
    vehicle DB pool. Fire-and-forget — a failure here just means the first
    lookup pays the cost, as it did before.
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

        with ThreadPoolExecutor(max_workers=2) as ex:
            ex.submit(_apis)
            ex.submit(_db)

    threading.Thread(target=_warm, name="assess-vehicle-warm", daemon=True).start()


@router.get("/assess-vehicle/lookup/{vrm}")
def assess_vehicle_cached(vrm: str, mileage: int, user: str = Depends(get_current_user)):
    """
    Rebuild a popup for a vehicle already in the store — no API calls.

    Used when opening a row in the recent list, whether that vehicle was last
    checked by a supplier list run or a manual lookup.
    """
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
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


def _at_lookup(vrm: str, mileage: int) -> dict:
    """Autotrader vehicle details, retail valuation and retail rating."""
    from list_processor.autotrader_client import AutoTraderClient, _unwrap_vehicles_result

    at = AutoTraderClient()
    session = requests.Session()
    headers = {'Authorization': f'Bearer {at.get_access_token(session)}'}

    vehicles_data = at.send_vehicles_post_request(session, headers, vrm, mileage)
    if not vehicles_data:
        return {'found': False}

    result = _unwrap_vehicles_result(vehicles_data)
    info = result.get('vehicle', {})
    metrics = result.get('vehicleMetrics', {}).get('retail', {})

    derivative_id = info.get('derivativeId', '')
    first_reg = info.get('firstRegistrationDate', '')

    features = [
        {"name": f['name'].strip()}
        for f in result.get('features', [])
        if f.get('factoryFitted') is True and f.get('name')
    ]

    retail_valuation = None
    if derivative_id and first_reg and mileage:
        valuations = at.send_valuations_post_request(
            session, headers, vrm, derivative_id, first_reg, mileage, features
        )
        if valuations:
            retail_valuation = valuations.get('valuations', {}).get('retail', {}).get('amountGBP')

    # Fall back to the valuation returned inline by /vehicles when the
    # feature-adjusted call could not be made.
    if retail_valuation is None:
        retail_valuation = result.get('valuations', {}).get('retail', {}).get('amountGBP')

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
        'factory_fitted_features': ', '.join(f['name'] for f in features) or None,
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

    try:
        presence['dbs'] = _find(_get_dbs_purchases_cached(quiet=True), 'dbs')
    except Exception as e:
        logger.warning(f"Assess vehicle: Daily Buyers Sheet check failed for {vrm}: {e}")
    try:
        presence['cb'] = _find(_get_cb_purchases_cached(quiet=True), 'cb')
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

    dbs = cb = None
    try:
        dbs = _count(_get_dbs_purchases_cached(quiet=True))
    except Exception as e:
        logger.warning(f"Assess vehicle: DBS purchase count failed: {e}")
    try:
        cb = _count(_get_cb_purchases_cached(quiet=True))
    except Exception as e:
        logger.warning(f"Assess vehicle: CB purchase count failed: {e}")
    return {'dbs': dbs, 'cb': cb}


def _build_result(vrm: str, mileage: int, cap: dict, at: dict,
                  checks: list, provenance: dict, user: str) -> dict:
    """
    Attach the Snowflake-derived parts — where the vehicle already appears and
    what the buying requirements say — to a CAP/Autotrader result, whether that
    came from the APIs or from the store.
    """
    cap_id = cap.get('cap_id')

    # CAP's VRM lookup does not return a VIN, but Autotrader's /vehicles does.
    # Show it on the CAP block, which is where it was asked for.
    if not cap.get('vin') and at.get('vin'):
        cap['vin'] = at['vin']

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
    elif at.get('retail_valuation'):
        price_band = _band_for_value(at['retail_valuation'])

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

    return {
        'vrm':        vrm,
        'mileage':    mileage,
        'looked_up':  provenance['fetched_at'],
        'provenance': provenance,
        'checks':     checks,
        'cap':        cap,
        'autotrader': at,
        'presence':   presence,
        'buying':     br,
    }


@router.post("/assess-vehicle/lookup")
def assess_vehicle_lookup(request: Request, payload: dict, user: str = Depends(get_current_user)):
    if user != 'tdrayson':
        raise HTTPException(status_code=403)

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
    _track(request, 'assess_vehicle', '/assess-vehicle', {'vrm': vrm, 'force': force})

    # Show what the store already holds rather than paying for the APIs again.
    # A supplier list run and a manual check write the same rows, so this hits
    # whether the VRM was last seen on a list or looked up here.
    stored = _stored_lookup(vrm, mileage)
    if force and stored and not stored['valued_today']:
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
        # CAP and Autotrader are independent, so run them together.
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_cap = ex.submit(_cap_lookup, vrm, mileage)
            f_at = ex.submit(_at_lookup, vrm, mileage)
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
                    # Read back in the same connection so the popup shows this
                    # check alongside any earlier ones.
                    checks = _format_checks(fetch_check_history(conn, vrm))
            except Exception as e:
                logger.error(f"Assess vehicle: could not save check for {vrm}: {e}")

    result = _build_result(vrm, mileage, cap, at, checks, provenance, user)
    return {'result': result, 'history': _recent_list()}


@router.get("/assess-vehicle/history")
def assess_vehicle_history(days: int = _RECENT_DAYS, user: str = Depends(get_current_user)):
    """Every vehicle checked in the window — supplier lists and manual checks."""
    if user != 'tdrayson':
        raise HTTPException(status_code=403)
    # Keep the window sane — an arbitrary value would scan the whole table.
    days = max(1, min(int(days), 365))
    return {'history': _recent_list(days=days), 'days': days}
