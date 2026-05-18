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
import traceback
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from list_processor.autotrader_client import AutoTraderClient
from list_processor.cap_client import CAPClient
from list_processor.columns_mapper import BaseColumnsMapper
from utils.db_utils import get_vehicle_database_connection
from utils.vehicle_store import _to_float, _to_int
from utils.vehicle_store import (
    fetch_cached_valuations,
    fetch_known_vehicles,
    fetch_price_history,
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


def _fetch_cache(vehicle_rows) -> tuple[dict, dict]:
    """
    Returns (cached_valuations, known_vehicles). On any DB error returns empty dicts
    so all vehicles fall through to API calls.
    """
    try:
        with get_vehicle_database_connection() as conn:
            vrm_mileage_pairs = [(v.vrm, v.mileage) for v in vehicle_rows]
            vrms = [v.vrm for v in vehicle_rows]
            cached = fetch_cached_valuations(conn, vrm_mileage_pairs)
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

        logger.info(
            'DB write complete for supplier %s: %d vehicles, %d valuations, %d listings',
            supplier_name, len(vehicles_to_upsert), len(valuations_to_insert), len(listings_to_insert),
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
) -> pd.DataFrame:
    """
    Drop-in replacement for the parallel process_df calls in router.py.
    Returns the same concatenated processed_df the router expects.
    """
    vehicle_rows = _parse_rows(input_df, columns_mapper)

    # Step 1: batch DB reads
    cached_valuations, known_vehicles = _fetch_cache(vehicle_rows)

    cache_hits = {(v.vrm, v.mileage) for v in vehicle_rows if (v.vrm, v.mileage) in cached_valuations}
    cache_misses = [v for v in vehicle_rows if (v.vrm, v.mileage) not in cached_valuations]

    logger.info('Supplier %s: %d total, %d from cache, %d API requests', supplier_name, len(vehicle_rows), len(cache_hits), len(cache_misses))

    # Step 2: build a partial-input df for cache misses only, run AT+CAP in parallel
    api_results: dict[str, dict] = {}
    _at_veh = _at_val = _at_veh_err = _at_val_err = 0
    _cap_vrm = _cap_monthly = _cap_live = _cap_retries = _cap_vrm_err = _cap_live_err = 0
    _at_errors: list = []
    _cap_errors: list = []

    if cache_misses:
        miss_vrms = {v.vrm for v in cache_misses}
        # VRMs with a CAP ID but no derivable registration date (e.g. Irish plates) are
        # not AT-searchable — exclude them from the AT call to avoid wasted error requests.
        at_skip_vrms = {v.vrm for v in cache_misses if v.capid and not v.registration_date}
        miss_df = input_df[input_df.apply(
            lambda row: columns_mapper.map_row(row.to_dict()) is not None and
                        columns_mapper.map_row(row.to_dict()).vrm in miss_vrms,
            axis=1
        )]
        at_miss_df = miss_df[miss_df.apply(
            lambda row: columns_mapper.map_row(row.to_dict()) is not None and
                        columns_mapper.map_row(row.to_dict()).vrm not in at_skip_vrms,
            axis=1
        )]

        with ThreadPoolExecutor(max_workers=2) as executor:
            at_future = executor.submit(at_client.process_file, at_miss_df, columns_mapper, at_workers, at_rate_limit)
            cap_future = executor.submit(cap_client.process_file, miss_df, columns_mapper, cap_workers, cap_rate_limit)
            at_df, _at_veh, _at_val, _at_veh_err, _at_val_err, _at_errors = at_future.result()
            cap_df, _cap_vrm, _cap_monthly, _cap_live, _cap_retries, _cap_vrm_err, _cap_live_err, _cap_errors = cap_future.result()

        if at_client._abort:
            raise RuntimeError(f"AT API aborted after {at_client._rate_limit_hits} rate limit (429) responses — reduce AT workers or rate limit")
        if cap_client._abort:
            raise RuntimeError(f"CAP API aborted after {cap_client._connection_errors} connection/timeout errors — reduce CAP workers or rate limit")

        # Build AT results keyed by VRM (at_miss_df may be smaller than miss_df)
        at_by_vrm: dict[str, pd.Series] = {}
        at_miss_df_reset = at_miss_df.reset_index(drop=True)
        at_df_reset = at_df.reset_index(drop=True)
        for i, at_row in at_miss_df_reset.iterrows():
            try:
                v = columns_mapper.map_row(at_row.to_dict())
                if v and i < len(at_df_reset):
                    at_by_vrm[v.vrm] = at_df_reset.iloc[i]
            except Exception:
                pass

        # Merge AT+CAP results into api_results keyed by vrm
        miss_df_reset = miss_df.reset_index(drop=True)
        cap_df_reset = cap_df.reset_index(drop=True)

        for i, row in miss_df_reset.iterrows():
            try:
                vehicle = columns_mapper.map_row(row.to_dict())
                if vehicle is None:
                    continue
                at_row = at_by_vrm.get(vehicle.vrm, pd.Series(dtype=object))
                cap_row = cap_df_reset.iloc[i] if i < len(cap_df_reset) else pd.Series(dtype=object)

                description = at_row.get('at_description', '')
                has_at_data = bool(description and description != 'N/A')

                api_results[vehicle.vrm] = {
                    'has_at_data': has_at_data,
                    'at_description': description if has_at_data else None,
                    'at_factory_fitted_features': at_row.get('at_factory_fitted_feature_names') or None,
                    'at_first_registration_date': at_row.get('at_at_first_registration_date') or None,
                    'at_derivative_id': at_row.get('at_derivative_id') or None,
                    'at_retail_valuation': _to_int(at_row.get('at_valuations_retail_valuation')),
                    'at_rating': _to_float(at_row.get('at_rating')),
                    'at_days_to_sell': _to_float(at_row.get('at_original_days_to_sell')),
                    'at_market_condition': _to_float(at_row.get('at_market_condition')),
                    'cap_capid': _to_int(cap_row.get('cap_capid')),
                    'cap_capcode': cap_row.get('cap_capcode') or None,
                    'cap_manufacturer': cap_row.get('cap_manufacturer') or None,
                    'cap_model': cap_row.get('cap_model') or None,
                    'cap_derivative': cap_row.get('cap_derivative') or None,
                    'cap_registration_date': cap_row.get('cap_registration_date') or None,
                    'cap_live_clean': _to_int(cap_row.get('cap_live_clean')),
                    'cap_live_retail': _to_int(cap_row.get('cap_live_retail')),
                    'cap_monthly_clean': _to_int(cap_row.get('cap_monthly_clean')),
                    'cap_monthly_retail': _to_int(cap_row.get('cap_monthly_retail')),
                    'cap_rounded_mileage': _to_int(cap_row.get('cap_rounded_mileage')),
                }
            except Exception:
                pass

    # Step 3: fill cache hits from DB (valuations + vehicle identity)
    for vrow in vehicle_rows:
        key = (vrow.vrm, vrow.mileage)
        if key in cached_valuations and vrow.vrm not in api_results:
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
    try:
        with get_vehicle_database_connection() as conn:
            price_history = fetch_price_history(conn, supplier_id, [v.vrm for v in vehicle_rows])
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
            'Price Change Date':  changed_at.date() if changed_at is not None else None,
        })

    price_df = pd.DataFrame(price_rows)

    # Exclude AT Valuations 400s — AT data quality issue (missing firstRegistrationDate), not speed/rate related
    api_error_log = [e for e in _at_errors + _cap_errors if not (e.get('api') == 'AT Valuations' and e.get('status_code') == 400)]
    return pd.concat([input_df.reset_index(drop=True), at_result, cap_result, price_df], axis=1), reductions, api_error_log
