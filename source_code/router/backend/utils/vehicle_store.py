import datetime
import logging
import re

import psycopg2.extras

logger = logging.getLogger(__name__)

VALUATION_TTL_HOURS = 24


def _to_int(value):
    if value is None:
        return None
    try:
        if isinstance(value, float) and value != value:  # NaN
            return None
        return int(float(str(value).replace(',', '').strip()))
    except (ValueError, TypeError):
        return None


def _to_float(value):
    if value is None:
        return None
    try:
        if isinstance(value, float) and value != value:  # NaN
            return None
        cleaned = re.sub(r'[^\d.\-]', '', str(value))
        return float(cleaned) if cleaned else None
    except (ValueError, TypeError):
        return None


def fetch_cached_valuations(conn, vrm_mileage_pairs: list[tuple[str, int]]) -> dict[tuple[str, int], dict]:
    """
    Batch fetch the most recent valuation for each (vrm, mileage) pair.
    Returns a dict keyed by (vrm, mileage) for pairs with a valuation under 24 hours old.
    """
    if not vrm_mileage_pairs:
        return {}

    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=VALUATION_TTL_HOURS)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (vrm, mileage)
                vrm, mileage, fetched_at,
                at_retail_valuation, at_rating, at_days_to_sell, at_market_condition,
                cap_live_clean, cap_live_retail, cap_monthly_clean, cap_monthly_retail, cap_rounded_mileage
            FROM vehicle_valuations
            WHERE (vrm, mileage) IN %s
              AND fetched_at >= %s
            ORDER BY vrm, mileage, fetched_at DESC
            """,
            (tuple(vrm_mileage_pairs), cutoff)
        )
        rows = cur.fetchall()

    return {(r['vrm'], r['mileage']): dict(r) for r in rows}


def fetch_known_vehicles(conn, vrms: list[str]) -> dict[str, dict]:
    """
    Batch fetch vehicle identity rows for the given VRMs.
    Returns a dict keyed by vrm.
    """
    if not vrms:
        return {}

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT vrm, at_description, at_factory_fitted_features, at_first_registration_date,
                   at_derivative_id, cap_capid, cap_capcode, cap_manufacturer, cap_model, cap_derivative,
                   cap_registration_date
            FROM vehicles WHERE vrm IN %s
            """,
            (tuple(vrms),)
        )
        rows = cur.fetchall()

    return {r['vrm']: dict(r) for r in rows}


def upsert_vehicles(conn, vehicles: list[dict]) -> None:
    """
    Bulk upsert vehicle identity rows.
    at_derivative_id and cap_capid are never overwritten once set.
    """
    if not vehicles:
        return

    rows = [
        (
            v['vrm'],
            v.get('at_description'),
            v.get('at_factory_fitted_features'),
            v.get('at_first_registration_date'),
            v.get('at_derivative_id'),
            _to_int(v.get('cap_capid')),
            v.get('cap_capcode'),
            v.get('cap_manufacturer'),
            v.get('cap_model'),
            v.get('cap_derivative'),
            v.get('cap_registration_date'),
        )
        for v in vehicles
    ]

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO vehicles
                (vrm, at_description, at_factory_fitted_features, at_first_registration_date,
                 at_derivative_id, cap_capid, cap_capcode, cap_manufacturer, cap_model, cap_derivative,
                 cap_registration_date)
            VALUES %s
            ON CONFLICT (vrm) DO UPDATE SET
                at_description              = EXCLUDED.at_description,
                at_factory_fitted_features  = EXCLUDED.at_factory_fitted_features,
                at_first_registration_date  = COALESCE(vehicles.at_first_registration_date, EXCLUDED.at_first_registration_date),
                at_derivative_id            = COALESCE(vehicles.at_derivative_id, EXCLUDED.at_derivative_id),
                cap_capid                   = COALESCE(vehicles.cap_capid, EXCLUDED.cap_capid),
                cap_capcode                 = EXCLUDED.cap_capcode,
                cap_manufacturer            = EXCLUDED.cap_manufacturer,
                cap_model                   = EXCLUDED.cap_model,
                cap_derivative              = EXCLUDED.cap_derivative,
                cap_registration_date       = COALESCE(vehicles.cap_registration_date, EXCLUDED.cap_registration_date)
            """,
            rows,
        )


def insert_valuations(conn, valuations: list[dict]) -> None:
    """
    Bulk insert new valuation rows. Always appends — never updates existing rows.
    """
    if not valuations:
        return

    rows = [
        (
            v['vrm'],
            _to_int(v.get('mileage')),
            v.get('fetched_at', datetime.datetime.utcnow()),
            _to_int(v.get('at_retail_valuation')),
            _to_float(v.get('at_rating')),
            _to_float(v.get('at_days_to_sell')),
            _to_float(v.get('at_market_condition')),
            _to_int(v.get('cap_live_clean')),
            _to_int(v.get('cap_live_retail')),
            _to_int(v.get('cap_monthly_clean')),
            _to_int(v.get('cap_monthly_retail')),
            _to_int(v.get('cap_rounded_mileage')),
        )
        for v in valuations
    ]

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO vehicle_valuations
                (vrm, mileage, fetched_at,
                 at_retail_valuation, at_rating, at_days_to_sell, at_market_condition,
                 cap_live_clean, cap_live_retail, cap_monthly_clean, cap_monthly_retail, cap_rounded_mileage)
            VALUES %s
            """,
            rows,
        )


def insert_listings(conn, listings: list[dict]) -> None:
    """
    Bulk insert supplier_vehicle_listings rows — one per vehicle per import run.
    """
    if not listings:
        return

    rows = [
        (
            l['vrm'],
            l['supplier_id'],
            l.get('imported_at', datetime.datetime.utcnow()),
            _to_int(l.get('mileage')),
            _to_float(l.get('asking_price')),
        )
        for l in listings
    ]

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO supplier_vehicle_listings (vrm, supplier_id, imported_at, mileage, asking_price)
            VALUES %s
            """,
            rows,
        )


def upsert_supplier_status(conn, statuses: list[dict]) -> None:
    """
    Bulk upsert supplier_vehicle_status. Detects price changes and preserves first_seen_at.
    """
    if not statuses:
        return

    now = datetime.datetime.utcnow()
    rows = [
        (
            s['vrm'],
            s['supplier_id'],
            now,
            now,
            _to_float(s.get('asking_price')),
        )
        for s in statuses
    ]

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO supplier_vehicle_status
                (vrm, supplier_id, first_seen_at, last_seen_at, current_price, previous_price, price_changed_at, removed_at)
            VALUES %s
            ON CONFLICT (vrm, supplier_id) DO UPDATE SET
                last_seen_at     = EXCLUDED.last_seen_at,
                removed_at       = NULL,
                previous_price   = supplier_vehicle_status.current_price,
                price_changed_at = CASE
                    WHEN supplier_vehicle_status.current_price IS DISTINCT FROM EXCLUDED.current_price
                    THEN EXCLUDED.last_seen_at
                    ELSE supplier_vehicle_status.price_changed_at
                END,
                current_price    = EXCLUDED.current_price
            """,
            rows,
            template="(%s, %s, %s, %s, %s, NULL, NULL, NULL)",
        )


def fetch_price_history(conn, supplier_id: int, vrms: list[str]) -> dict[str, dict]:
    """
    Fetch previous_price, price_change, and price_changed_at for each VRM.
    Only returns rows where a price change has been recorded (previous_price IS NOT NULL).
    Returns a dict keyed by vrm.
    """
    if not vrms:
        return {}

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT vrm, previous_price, current_price, price_changed_at
            FROM supplier_vehicle_status
            WHERE supplier_id = %s
              AND vrm = ANY(%s)
              AND previous_price IS NOT NULL
            """,
            (supplier_id, vrms)
        )
        rows = cur.fetchall()

    return {r['vrm']: dict(r) for r in rows}


def mark_removals(conn, supplier_id: int, active_vrms: list[str]) -> None:
    """
    For snapshot feeds — mark any VRM for this supplier not in active_vrms as removed.
    """
    now = datetime.datetime.utcnow()
    with conn.cursor() as cur:
        if active_vrms:
            cur.execute(
                """
                UPDATE supplier_vehicle_status
                SET removed_at = %s
                WHERE supplier_id = %s
                  AND removed_at IS NULL
                  AND vrm != ALL(%s)
                """,
                (now, supplier_id, active_vrms),
            )
        else:
            cur.execute(
                """
                UPDATE supplier_vehicle_status
                SET removed_at = %s
                WHERE supplier_id = %s
                  AND removed_at IS NULL
                """,
                (now, supplier_id),
            )
