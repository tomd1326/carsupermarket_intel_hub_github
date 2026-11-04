"""
Batch processor — wraps the AT/CAP clients with DB caching and supplier tracking.

Flow per file:
1. Parse CSV rows via columns_mapper -> VehicleRow list
2. Batch DB reads: cached valuations + known vehicles (2 queries)
3. Parallel AT+CAP API calls for cache-miss VRMs only
4. Batch DB writes: vehicles, valuations, listings, status, removals
5. Return enriched rows for the existing output pipeline (unchanged)

All DB steps are wrapped in try/except — any failure falls back to making
full API calls so the evaluator always produces a result.
"""

import datetime
import logging
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from clients.autotrader_client import AutoTraderClient
from clients.cap_client import CAPClient
from clients.columns_mapper import BaseColumnsMapper
from utils.db_utils import get_vehicle_database_connection
from utils.vehicle_store import _to_float, _to_int, round_mileage_to_nearest_thousand
from utils.vehicle_store import (
    fetch_cached_valuations,
    fetch_known_vehicles,
    fetch_price_history,
    fetch_buyer_figures,
    ensure_vehicles_exist,
    upsert_vehicles,
    insert_valuations,
    insert_listings,
    upsert_supplier_status,
    mark_removals,
)

logger = logging.getLogger(__name__)


def _parse_rows(input_df: pd.DataFrame, columns_mapper: BaseColumnsMapper) -> list:
    rows = []
    for _, row in input_df.iterrows():
        try:
            vehicle = columns_mapper.map_row(row.to_dict())
            if vehicle is not None:
                rows.append(vehicle)
        except Exception:
            pass
    return rows


def _fetch_cache(vehicle_rows, valuation_cache_hours: int = None) -> tuple[dict, dict]:
    """
    Returns (cached_valuations, known_vehicles). On any DB error returns empty dicts
    so all vehicles fall through to API calls.
    """
    try:
        with get_vehicle_database_connection() as conn:
            vrm_mileage_pairs = [(v.vrm, v.mileage) for v in vehicle_rows]
            vrms = [v.vrm for v in vehicle_rows]
            cached = fetch_cached_valuations(conn, vrm_mileage_pairs, ttl_hours=valuation_cache_hours)
            known = fetch_known_vehicles(conn, vrms)
            return cached, known
    except Exception:
        logger.error('DB cache read failed, treating all vehicles as cache misses: %s', traceback.format_exc())
        return {}, {}


def _write_to_db(supplier_id: int, supplier_name: str, vehicle_rows, api_results: dict, is_snapshot: bool):
    """
    Batch write all DB records. Any failure is logged and swallowed — never raises.
    """
    now = datetime.datetime.utcnow()
    try:
        vehicles_to_upsert = []
        valuations_to_insert = []
        listings_to_insert = []
        statuses_to_upsert = []

        for vrow in vehicle_rows:
            result = api_results.get(vrow.vrm)
            if result is None:
                continue

            vrm_in_vehicles = bool(result.get('has_at_data'))
            from_cache = bool(result.get('from_cache'))

            if vrm_in_vehicles and not from_cache:
                vehicles_to_upsert.append({
                    'vrm': vrow.vrm,
                    'at_description': result.get('at_description'),
                    'at_factory_fitted_features': result.get('at_factory_fitted_features'),
                    'at_first_registration_date': result.get('at_first_registration_date'),
                    'at_colour': result.get('at_colour'),
                    'at_derivative_id': result.get('at_derivative_id'),
                    'cap_capid': result.get('cap_capid'),
                    'cap_capcode': result.get('cap_capcode'),
                    'cap_manufacturer': result.get('cap_manufacturer'),
                    'cap_model': result.get('cap_model'),
                    'cap_derivative': result.get('cap_derivative'),
                    'cap_registration_date': result.get('cap_registration_date'),
                })

            if vrm_in_vehicles and not from_cache and (result.get('at_retail_valuation') is not None or result.get('cap_live_clean') is not None):
                valuations_to_insert.append({
                    'vrm': vrow.vrm,
                    'mileage': vrow.mileage,
                    'fetched_at': now,
                    'at_retail_valuation': result.get('at_retail_valuation'),
                    'at_rating': result.get('at_rating'),
                    'at_days_to_sell': result.get('at_days_to_sell'),
                    'at_market_condition': result.get('at_market_condition'),
                    'cap_live_clean': result.get('cap_live_clean'),
                    'cap_live_retail': result.get('cap_live_retail'),
                    'cap_monthly_clean': result.get('cap_monthly_clean'),
                    'cap_monthly_retail': result.get('cap_monthly_retail'),
                    'cap_rounded_mileage': result.get('cap_rounded_mileage'),
                })

            # Track price for every VRM present in the supplier file, regardless of AT/CAP data
            listings_to_insert.append({
                'vrm': vrow.vrm,
                'supplier_id': supplier_id,
                'imported_at': now,
                'mileage': vrow.mileage,
                'asking_price': vrow.asking_price,
            })

            statuses_to_upsert.append({
                'vrm': vrow.vrm,
                'supplier_id': supplier_id,
                'asking_price': vrow.asking_price,
            })

        write_started = time.time()
        with get_vehicle_database_connection() as conn:
            if vehicles_to_upsert:
                upsert_vehicles(conn, vehicles_to_upsert)

            # Ensure all tracked VRMs exist in vehicles before FK-constrained inserts.
            # Covers VRMs where AT/CAP failed but the supplier price still needs tracking.
            all_tracked_vrms = [s['vrm'] for s in statuses_to_upsert]
            ensure_vehicles_exist(conn, all_tracked_vrms)
            conn.commit()

            if valuations_to_insert:
                insert_valuations(conn, valuations_to_insert)
            if listings_to_insert:
                insert_listings(conn, listings_to_insert)
            if statuses_to_upsert:
                upsert_supplier_status(conn, statuses_to_upsert)
            if is_snapshot:
                active_vrms = [v.vrm for v in vehicle_rows]
                mark_removals(conn, supplier_id, active_vrms)
            conn.commit()

        # Timed so a slow write shows up as a number rather than a hunch. The
        # row total against the elapsed time is what says whether the rows per
        # round trip (WRITE_PAGE_SIZE in vehicle_store) is set sensibly.
        total_written = (len(vehicles_to_upsert) + len(valuations_to_insert)
                         + len(listings_to_insert) + len(statuses_to_upsert))
        logger.info(
            'DB write complete for supplier %s in %.1fs: %d vehicles, %d valuations, %d listings, %d rows total',
            supplier_name, time.time() - write_started, len(vehicles_to_upsert),
            len(valuations_to_insert), len(listings_to_insert), total_written,
        )
    except Exception:
        logger.error('DB write failed for supplier %s — results still returned: %s', supplier_name, traceback.format_exc())


def process_file_with_cache(
    input_df: pd.DataFrame,
    columns_mapper: BaseColumnsMapper,
    supplier_id: int,
    supplier_name: str,
    is_snapshot: bool,
    at_client: AutoTraderClient,
    cap_client: CAPClient,
    cap_workers: int,
    cap_rate_limit: int,
    at_workers: int = 40,
    at_rate_limit: int = 60,
    prev_task_created=None,
    valuation_cache_hours: int = None,
) -> pd.DataFrame:
    """
    Drop-in replacement for the parallel process_df calls in router.py.
    Returns the same concatenated processed_df the router expects.
    """
    vehicle_rows = _parse_rows(input_df, columns_mapper)

    # Step 1: batch DB reads
    cached_valuations, known_vehicles = _fetch_cache(vehicle_rows, valuation_cache_hours=valuation_cache_hours)

    def _has_cap(cached: dict) -> bool:
        return cached.get('cap_live_clean') is not None or cached.get('cap_monthly_clean') is not None

    def _has_at(cached: dict) -> bool:
        return cached.get('at_retail_valuation') is not None

    def _cache_key(v):
        return (v.vrm, round_mileage_to_nearest_thousand(v.mileage))

    # Full cache hit = AT + CAP both present. Partial = one is missing and needs re-requesting.
    full_cache_hits = {_cache_key(v) for v in vehicle_rows
                       if _cache_key(v) in cached_valuations
                       and _has_at(cached_valuations[_cache_key(v)])
                       and _has_cap(cached_valuations[_cache_key(v)])}
    cap_only_misses = [v for v in vehicle_rows
                       if _cache_key(v) in cached_valuations
                       and _has_at(cached_valuations[_cache_key(v)])
                       and not _has_cap(cached_valuations[_cache_key(v)])]
    at_only_misses = [v for v in vehicle_rows
                      if _cache_key(v) in cached_valuations
                      and not _has_at(cached_valuations[_cache_key(v)])
                      and _has_cap(cached_valuations[_cache_key(v)])]
    full_misses = [v for v in vehicle_rows if _cache_key(v) not in cached_valuations]

    # Vehicles that need at least one API call
    cap_needed = {v.vrm for v in cap_only_misses + full_misses}
    at_needed = {v.vrm for v in at_only_misses + full_misses}

    logger.info(
        'Supplier %s: %d total, %d full cache hits, %d cap-only misses, %d at-only misses, %d full misses',
        supplier_name, len(vehicle_rows), len(full_cache_hits), len(cap_only_misses), len(at_only_misses), len(full_misses),
    )

    # Step 2: build partial-input dfs and run AT+CAP in parallel for only what's needed
    api_results: dict[str, dict] = {}
    _at_veh = _at_val = _at_veh_err = _at_val_err = 0
    _cap_vrm = _cap_monthly = _cap_live = _cap_retries = _cap_vrm_err = _cap_live_err = 0
    _at_errors: list = []
    _cap_errors: list = []

    api_needed_vrms = cap_needed | at_needed
    if api_needed_vrms:
        # VRMs with a CAP ID but no derivable registration date (e.g. Irish plates) are
        # not AT-searchable — exclude them from the AT call to avoid wasted error requests.
        at_skip_vrms = {v.vrm for v in cap_only_misses + full_misses if v.capid and not v.registration_date}

        def _make_df(vrm_set):
            return input_df[input_df.apply(
                lambda row: columns_mapper.map_row(row.to_dict()) is not None and
                            columns_mapper.map_row(row.to_dict()).vrm in vrm_set,
                axis=1
            )]

        cap_df_input = _make_df(cap_needed) if cap_needed else pd.DataFrame()
        at_df_input = _make_df(at_needed - at_skip_vrms) if (at_needed - at_skip_vrms) else pd.DataFrame()

        with ThreadPoolExecutor(max_workers=2) as executor:
            at_future = executor.submit(at_client.process_file, at_df_input, columns_mapper, at_workers, at_rate_limit) if not at_df_input.empty else None
            cap_future = executor.submit(cap_client.process_file, cap_df_input, columns_mapper, cap_workers, cap_rate_limit) if not cap_df_input.empty else None

            at_df = pd.DataFrame()
            if at_future:
                at_df, _at_veh, _at_val, _at_veh_err, _at_val_err, _at_errors = at_future.result()
            cap_df = pd.DataFrame()
            if cap_future:
                cap_df, _cap_vrm, _cap_monthly, _cap_live, _cap_retries, _cap_vrm_err, _cap_live_err, _cap_errors = cap_future.result()

        if at_client._abort:
            raise RuntimeError(f"AT API aborted after {at_client._rate_limit_hits} rate limit (429) responses — reduce AT workers or rate limit")
        if cap_client._abort:
            raise RuntimeError(f"CAP API aborted after {cap_client._connection_errors} connection/timeout errors — reduce CAP workers or rate limit")

        # Build AT results keyed by VRM
        at_by_vrm: dict[str, pd.Series] = {}
        if not at_df_input.empty and not at_df.empty:
            at_df_input_reset = at_df_input.reset_index(drop=True)
            at_df_reset = at_df.reset_index(drop=True)
            for i, at_row in at_df_input_reset.iterrows():
                try:
                    v = columns_mapper.map_row(at_row.to_dict())
                    if v and i < len(at_df_reset):
                        at_by_vrm[v.vrm] = at_df_reset.iloc[i]
                except Exception:
                    pass

        # Build CAP results keyed by VRM
        cap_by_vrm: dict[str, pd.Series] = {}
        if not cap_df_input.empty and not cap_df.empty:
            cap_df_input_reset = cap_df_input.reset_index(drop=True)
            cap_df_reset = cap_df.reset_index(drop=True)
            for i, cap_row in cap_df_input_reset.iterrows():
                try:
                    v = columns_mapper.map_row(cap_row.to_dict())
                    if v and i < len(cap_df_reset):
                        cap_by_vrm[v.vrm] = cap_df_reset.iloc[i]
                except Exception:
                    pass

        # Merge AT+CAP results into api_results, filling from cache where one side was not re-requested
        for vrow in cap_only_misses + at_only_misses + full_misses:
            if vrow.vrm in api_results:
                continue
            try:
                cached = cached_valuations.get(_cache_key(vrow), {})
                known = known_vehicles.get(vrow.vrm, {})
                at_row = at_by_vrm.get(vrow.vrm, pd.Series(dtype=object))
                cap_row = cap_by_vrm.get(vrow.vrm, pd.Series(dtype=object))

                # AT: use fresh API result if available, else fall back to cache
                if vrow.vrm in at_needed and vrow.vrm in at_by_vrm:
                    description = at_row.get('at_description', '')
                    has_at_data = bool(description and description != 'N/A')
                    at_retail_valuation = _to_int(at_row.get('at_valuations_retail_valuation'))
                    at_rating = _to_float(at_row.get('at_rating'))
                    at_days_to_sell = _to_float(at_row.get('at_original_days_to_sell'))
                    at_market_condition = _to_float(at_row.get('at_market_condition'))
                    at_factory_fitted_features = at_row.get('at_factory_fitted_feature_names') or None
                    at_first_registration_date = at_row.get('at_at_first_registration_date') or None
                    at_colour = at_row.get('at_at_colour') or None
                    at_derivative_id = at_row.get('at_derivative_id') or None
                    at_description = description if has_at_data else None
                else:
                    has_at_data = True
                    at_description = known.get('at_description')
                    at_factory_fitted_features = known.get('at_factory_fitted_features')
                    at_first_registration_date = known.get('at_first_registration_date')
                    at_colour = known.get('at_colour')
                    at_derivative_id = known.get('at_derivative_id')
                    at_retail_valuation = cached.get('at_retail_valuation')
                    at_rating = cached.get('at_rating')
                    at_days_to_sell = cached.get('at_days_to_sell')
                    at_market_condition = cached.get('at_market_condition')

                # CAP: use fresh API result if available, else fall back to cache
                if vrow.vrm in cap_needed and vrow.vrm in cap_by_vrm:
                    cap_capid = _to_int(cap_row.get('cap_capid'))
                    cap_capcode = cap_row.get('cap_capcode') or None
                    cap_manufacturer = cap_row.get('cap_manufacturer') or None
                    cap_model = cap_row.get('cap_model') or None
                    cap_derivative = cap_row.get('cap_derivative') or None
                    cap_registration_date = cap_row.get('cap_registration_date') or None
                    cap_live_clean = _to_int(cap_row.get('cap_live_clean'))
                    cap_live_retail = _to_int(cap_row.get('cap_live_retail'))
                    cap_monthly_clean = _to_int(cap_row.get('cap_monthly_clean'))
                    cap_monthly_retail = _to_int(cap_row.get('cap_monthly_retail'))
                    cap_rounded_mileage = _to_int(cap_row.get('cap_rounded_mileage'))
                else:
                    cap_capid = known.get('cap_capid')
                    cap_capcode = known.get('cap_capcode')
                    cap_manufacturer = known.get('cap_manufacturer')
                    cap_model = known.get('cap_model')
                    cap_derivative = known.get('cap_derivative')
                    cap_registration_date = known.get('cap_registration_date')
                    cap_live_clean = cached.get('cap_live_clean')
                    cap_live_retail = cached.get('cap_live_retail')
                    cap_monthly_clean = cached.get('cap_monthly_clean')
                    cap_monthly_retail = cached.get('cap_monthly_retail')
                    cap_rounded_mileage = cached.get('cap_rounded_mileage')

                api_results[vrow.vrm] = {
                    'has_at_data': has_at_data,
                    'at_description': at_description,
                    'at_factory_fitted_features': at_factory_fitted_features,
                    'at_first_registration_date': at_first_registration_date,
                    'at_colour': at_colour,
                    'at_derivative_id': at_derivative_id,
                    'at_retail_valuation': at_retail_valuation,
                    'at_rating': at_rating,
                    'at_days_to_sell': at_days_to_sell,
                    'at_market_condition': at_market_condition,
                    'cap_capid': cap_capid,
                    'cap_capcode': cap_capcode,
                    'cap_manufacturer': cap_manufacturer,
                    'cap_model': cap_model,
                    'cap_derivative': cap_derivative,
                    'cap_registration_date': cap_registration_date,
                    'cap_live_clean': cap_live_clean,
                    'cap_live_retail': cap_live_retail,
                    'cap_monthly_clean': cap_monthly_clean,
                    'cap_monthly_retail': cap_monthly_retail,
                    'cap_rounded_mileage': cap_rounded_mileage,
                }
            except Exception:
                pass

    # Step 3: fill full cache hits from DB (no API calls needed)
    for vrow in vehicle_rows:
        key = _cache_key(vrow)
        if key in full_cache_hits and vrow.vrm not in api_results:
            cached = cached_valuations[key]
            known = known_vehicles.get(vrow.vrm, {})
            api_results[vrow.vrm] = {
                'has_at_data': True,
                'from_cache': True,
                'at_description':           known.get('at_description'),
                'at_factory_fitted_features': known.get('at_factory_fitted_features'),
                'at_first_registration_date': known.get('at_first_registration_date'),
                'at_derivative_id':         known.get('at_derivative_id'),
                'cap_capid':                known.get('cap_capid'),
                'cap_capcode':              known.get('cap_capcode'),
                'cap_manufacturer':         known.get('cap_manufacturer'),
                'cap_model':                known.get('cap_model'),
                'cap_derivative':           known.get('cap_derivative'),
                'cap_registration_date':    known.get('cap_registration_date'),
                'at_retail_valuation':      cached.get('at_retail_valuation'),
                'at_rating':                cached.get('at_rating'),
                'at_days_to_sell':          cached.get('at_days_to_sell'),
                'at_market_condition':      cached.get('at_market_condition'),
                'cap_live_clean':           cached.get('cap_live_clean'),
                'cap_live_retail':          cached.get('cap_live_retail'),
                'cap_monthly_clean':        cached.get('cap_monthly_clean'),
                'cap_monthly_retail':       cached.get('cap_monthly_retail'),
                'cap_rounded_mileage':      cached.get('cap_rounded_mileage'),
            }

    logger.info('AT: %d vehicles requests (%d errors), %d valuations requests (%d errors)', _at_veh, _at_veh_err, _at_val, _at_val_err)
    cap_retry_str = f', {_cap_retries} retries' if _cap_retries else ''
    logger.info('CAP: %d VRM lookups (%d errors), %d CAP ID monthly, %d live valuations (%d errors)%s', _cap_vrm, _cap_vrm_err, _cap_monthly, _cap_live, _cap_live_err, cap_retry_str)

    # Step 4: DB write — swallowed on failure, never blocks result
    _write_to_db(supplier_id, supplier_name, vehicle_rows, api_results, is_snapshot)

    # Step 5: build output df directly from api_results — no further API calls needed.
    at_rows = []
    cap_rows = []
    for vrow in vehicle_rows:
        r = api_results.get(vrow.vrm, {})
        at_rows.append({
            'at_description':                   r.get('at_description'),
            'at_vrm':                           vrow.vrm,
            'at_mileage':                       vrow.mileage,
            'at_valuations_retail_valuation':   r.get('at_retail_valuation'),
            'at_rating':                        r.get('at_rating'),
            'at_original_days_to_sell':         r.get('at_days_to_sell'),
            'at_market_condition':              r.get('at_market_condition'),
            'at_factory_fitted_feature_names':  r.get('at_factory_fitted_features'),
            'at_at_first_registration_date':    r.get('at_first_registration_date'),
        })
        cap_rows.append({
            'cap_capid':            r.get('cap_capid'),
            'cap_capcode':          r.get('cap_capcode'),
            'cap_manufacturer':     r.get('cap_manufacturer'),
            'cap_model':            r.get('cap_model'),
            'cap_derivative':       r.get('cap_derivative'),
            'cap_registration_date': r.get('cap_registration_date'),
            'cap_rounded_mileage':  r.get('cap_rounded_mileage'),
            'cap_live_clean':       r.get('cap_live_clean'),
            'cap_live_retail':      r.get('cap_live_retail'),
            'cap_monthly_clean':    r.get('cap_monthly_clean'),
            'cap_monthly_retail':   r.get('cap_monthly_retail'),
            'cap_note':             None,
        })

    at_result = pd.DataFrame(at_rows)
    for col in ('at_rating', 'at_original_days_to_sell', 'at_market_condition'):
        if col in at_result.columns:
            at_result[col] = pd.to_numeric(at_result[col], errors='coerce')
    cap_result = pd.DataFrame(cap_rows)

    # Step 6: fetch price history and prepend as first 3 enhanced columns
    price_history = {}
    buyer_figures = {}
    try:
        with get_vehicle_database_connection() as conn:
            price_history = fetch_price_history(conn, supplier_id, [v.vrm for v in vehicle_rows])
            buyer_figures = fetch_buyer_figures(conn, [v.vrm for v in vehicle_rows])
    except Exception:
        logger.error('Price history fetch failed — columns will be blank: %s', traceback.format_exc())

    price_rows = []
    reductions = 0
    for vrow in vehicle_rows:
        ph = price_history.get(vrow.vrm, {})
        prev = ph.get('previous_price')
        curr = ph.get('current_price')
        changed_at = ph.get('price_changed_at')
        price_change = round(float(curr) - float(prev), 2) if curr is not None and prev is not None else None
        if price_change is not None and price_change < 0:
            if prev_task_created is None or (
                changed_at is not None and
                changed_at.replace(tzinfo=None) > prev_task_created.replace(tzinfo=None)
            ):
                reductions += 1
        price_rows.append({
            'Previous Price':     float(prev) if prev is not None else None,
            'Price Change':       price_change,
            'Price Change Date':  changed_at.replace(tzinfo=None) if changed_at is not None else None,
            # "Buyer" so these cannot clash with an RRP column on the supplier's own file.
            'Buyer RRP':                  buyer_figures.get(vrow.vrm, {}).get('rrp'),
            'Buyer Estimated Prep Cost':  buyer_figures.get(vrow.vrm, {}).get('est_prep_cost'),
        })

    price_df = pd.DataFrame(price_rows)

    # Exclude AT Valuations 400s — AT data quality issue (missing firstRegistrationDate), not speed/rate related
    api_error_log = [e for e in _at_errors + _cap_errors if not (e.get('api') == 'AT Valuations' and e.get('status_code') == 400)]
    return pd.concat([input_df.reset_index(drop=True), at_result, cap_result, price_df], axis=1), reductions, api_error_log, len(full_cache_hits)
