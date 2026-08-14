import datetime
import logging
import re

import psycopg2.extras

logger = logging.getLogger(__name__)

VALUATION_TTL_HOURS = 36


def round_mileage_to_nearest_thousand(mileage: int) -> int:
    """CAP valuations are bucketed per 1000 miles, so the valuation cache is
    keyed on this rounded figure rather than the exact odometer reading —
    two files quoting the same VRM a few miles apart (e.g. Purchase Pro vs
    1link exports of the same VWFS vehicle) reuse the same cached valuation.
    """
    if mileage < 1000:
        return 1000
    return round(mileage / 1000) * 1000


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


def ensure_manual_check_columns(conn) -> None:
    """Add the columns manual (Assess Vehicle) checks need.

    Mirrors _ensure_subsidy_rate_tables: db/07_manual_checks.sql is the
    canonical schema, but adding on demand means a fresh environment works
    without a manual migration step. All three are IF NOT EXISTS, so this is
    safe to call repeatedly and is a no-op once applied.
    """
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS at_vin VARCHAR(20)")
        cur.execute(
            "ALTER TABLE vehicle_valuations "
            "ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'evaluator', "
            "ADD COLUMN IF NOT EXISTS checked_by VARCHAR(100)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_vehicle_valuations_vrm_fetched "
            "ON vehicle_valuations (vrm, fetched_at DESC)"
        )
        # The vrm-leading indexes cannot serve a date-range scan across all
        # VRMs, which is what the recent-checks list does.
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_vehicle_valuations_fetched "
            "ON vehicle_valuations (fetched_at DESC)"
        )
    conn.commit()


def fetch_latest_valuation(conn, vrm: str, mileage: int) -> dict | None:
    """
    Most recent valuation for this VRM at this mileage, whatever its age and
    whichever check produced it. The Assess Vehicle page uses this to show
    existing data immediately, with its age, rather than always re-requesting.
    """
    rounded = round_mileage_to_nearest_thousand(_to_int(mileage) or 0)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT vrm, mileage, fetched_at, source, checked_by,
                   at_retail_valuation, at_rating, at_days_to_sell, at_market_condition,
                   cap_live_clean, cap_live_retail, cap_monthly_clean, cap_monthly_retail,
                   cap_rounded_mileage
            FROM vehicle_valuations
            WHERE vrm = %s AND mileage = %s
            ORDER BY fetched_at DESC
            LIMIT 1
            """,
            (vrm, rounded),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def fetch_check_history(conn, vrm: str, limit: int = 25) -> list[dict]:
    """
    Every check for this VRM, newest first, from supplier list runs and manual
    lookups alike. Supplier names come from any listing recorded at the same
    time, so an evaluator check can say which list it came from.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT v.fetched_at, v.mileage, v.source, v.checked_by,
                   v.at_retail_valuation, v.at_rating,
                   v.cap_live_clean, v.cap_live_retail,
                   v.cap_monthly_clean, v.cap_monthly_retail,
                   (
                       SELECT s.supplier_name
                       FROM supplier_vehicle_listings l
                       JOIN suppliers s ON s.supplier_id = l.supplier_id
                       WHERE l.vrm = v.vrm
                         AND l.imported_at BETWEEN v.fetched_at - INTERVAL '10 minutes'
                                              AND v.fetched_at + INTERVAL '10 minutes'
                       ORDER BY ABS(EXTRACT(EPOCH FROM (l.imported_at - v.fetched_at)))
                       LIMIT 1
                   ) AS supplier_name
            FROM vehicle_valuations v
            WHERE v.vrm = %s
            ORDER BY v.fetched_at DESC
            LIMIT %s
            """,
            (vrm, limit),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def fetch_recent_checks(conn, days: int = 30, limit: int = 500) -> list[dict]:
    """
    One row per VRM for every vehicle checked in the last `days`, from supplier
    list runs and manual lookups alike, newest first.

    DISTINCT ON keeps only the most recent check per VRM, so a vehicle that
    appears on a supplier list every night shows up once rather than 30 times.
    check_count is the number of checks in the window, so the list can show how
    often a vehicle has been seen.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            WITH recent AS (
                SELECT DISTINCT ON (vrm)
                       vrm, mileage, fetched_at, source, checked_by,
                       at_retail_valuation, at_rating, at_days_to_sell,
                       cap_live_clean, cap_live_retail,
                       cap_monthly_clean, cap_monthly_retail
                FROM vehicle_valuations
                WHERE fetched_at >= NOW() - MAKE_INTERVAL(days => %s)
                ORDER BY vrm, fetched_at DESC
            ),
            counts AS (
                SELECT vrm, COUNT(*) AS check_count
                FROM vehicle_valuations
                WHERE fetched_at >= NOW() - MAKE_INTERVAL(days => %s)
                GROUP BY vrm
            )
            SELECT r.*, c.check_count,
                   v.at_description, v.at_vin, v.cap_capid,
                   v.cap_manufacturer, v.cap_model, v.cap_derivative,
                   (
                       SELECT s.supplier_name
                       FROM supplier_vehicle_listings l
                       JOIN suppliers s ON s.supplier_id = l.supplier_id
                       WHERE l.vrm = r.vrm
                         AND l.imported_at BETWEEN r.fetched_at - INTERVAL '10 minutes'
                                              AND r.fetched_at + INTERVAL '10 minutes'
                       ORDER BY ABS(EXTRACT(EPOCH FROM (l.imported_at - r.fetched_at)))
                       LIMIT 1
                   ) AS supplier_name
            FROM recent r
            JOIN counts c ON c.vrm = r.vrm
            LEFT JOIN vehicles v ON v.vrm = r.vrm
            ORDER BY r.fetched_at DESC
            LIMIT %s
            """,
            (days, days, limit),
        )
        return [dict(r) for r in cur.fetchall()]


def fetch_cached_valuations(conn, vrm_mileage_pairs: list[tuple[str, int]], ttl_hours: int = None) -> dict[tuple[str, int], dict]:
    """
    Batch fetch the most recent valuation for each (vrm, mileage) pair.
    Mileage is rounded to the nearest 1000 before lookup (see
    round_mileage_to_nearest_thousand), matching how it is stored.
    Returns a dict keyed by (vrm, rounded_mileage) for pairs with a
    valuation within ttl_hours old.
    """
    if not vrm_mileage_pairs:
        return {}

    rounded_pairs = {(vrm, round_mileage_to_nearest_thousand(mileage)) for vrm, mileage in vrm_mileage_pairs}
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=ttl_hours if ttl_hours is not None else VALUATION_TTL_HOURS)

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
            (tuple(rounded_pairs), cutoff)
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


def ensure_vehicles_exist(conn, vrms: list[str]) -> None:
    """
    Insert a minimal stub row for any VRM not yet in the vehicles table.
    Required so FK constraints on supplier_vehicle_listings/status don't fail
    for VRMs where AT returned no data.
    """
    if not vrms:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO vehicles (vrm) VALUES %s ON CONFLICT (vrm) DO NOTHING",
            [(vrm,) for vrm in vrms],
        )


def ensure_vehicles_exist(conn, vrms: list[str]) -> None:
    """
    Insert a minimal stub row for any VRM not yet in the vehicles table.
    Required so FK constraints on supplier_vehicle_listings/status don't fail
    for VRMs where AT returned no data.
    """
    if not vrms:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO vehicles (vrm) VALUES %s ON CONFLICT (vrm) DO NOTHING",
            [(vrm,) for vrm in vrms],
        )


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
    Mileage is rounded to the nearest 1000 to match fetch_cached_valuations.
    """
    if not valuations:
        return

    rows = [
        (
            v['vrm'],
            round_mileage_to_nearest_thousand(_to_int(v.get('mileage'))),
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


def save_manual_check(conn, vrm: str, mileage: int, checked_by: str, cap: dict, at: dict) -> None:
    """
    Write one Assess Vehicle lookup into the shared vehicle store.

    Same tables the supplier evaluator writes, so a manual check warms its cache
    and vice versa — the only difference is source='manual' and checked_by. The
    vehicles upsert follows the evaluator's rules: identity fields that never
    change (derivative id, CAP ID, first registration) are kept once set.
    """
    at_found = bool(at.get('found'))
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO vehicles
                (vrm, at_description, at_factory_fitted_features, at_first_registration_date,
                 at_derivative_id, at_vin, cap_capid, cap_capcode, cap_manufacturer,
                 cap_model, cap_derivative, cap_registration_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (vrm) DO UPDATE SET
                at_description             = COALESCE(EXCLUDED.at_description, vehicles.at_description),
                at_factory_fitted_features = COALESCE(EXCLUDED.at_factory_fitted_features, vehicles.at_factory_fitted_features),
                at_first_registration_date = COALESCE(vehicles.at_first_registration_date, EXCLUDED.at_first_registration_date),
                at_derivative_id           = COALESCE(vehicles.at_derivative_id, EXCLUDED.at_derivative_id),
                at_vin                     = COALESCE(vehicles.at_vin, EXCLUDED.at_vin),
                cap_capid                  = COALESCE(vehicles.cap_capid, EXCLUDED.cap_capid),
                cap_capcode                = COALESCE(EXCLUDED.cap_capcode, vehicles.cap_capcode),
                cap_manufacturer           = COALESCE(EXCLUDED.cap_manufacturer, vehicles.cap_manufacturer),
                cap_model                  = COALESCE(EXCLUDED.cap_model, vehicles.cap_model),
                cap_derivative             = COALESCE(EXCLUDED.cap_derivative, vehicles.cap_derivative),
                cap_registration_date      = COALESCE(vehicles.cap_registration_date, EXCLUDED.cap_registration_date)
            """,
            (
                vrm,
                at.get('description') if at_found else None,
                at.get('factory_fitted_features') if at_found else None,
                _parse_at_date(at.get('first_registered')) if at_found else None,
                at.get('derivative_id') if at_found else None,
                (at.get('vin') or None) if at_found else None,
                _to_int(cap.get('cap_id')),
                cap.get('cap_code'),
                cap.get('make'),
                cap.get('model'),
                cap.get('derivative'),
                cap.get('registered'),
            ),
        )

        # Only record a valuation when at least one side returned figures —
        # matching the evaluator, which skips empty rows so they never become
        # a cache hit that suppresses a real lookup.
        will_write = at.get('retail_valuation') is not None or cap.get('live_clean') is not None
        logger.info(
            f"Assess vehicle SAVE: vrm={vrm} mileage={mileage} will_write_valuation={will_write} "
            f"at_retail={at.get('retail_valuation')} at_rating={at.get('retail_rating')} "
            f"cap_live_clean={cap.get('live_clean')} cap_monthly_clean={cap.get('monthly_clean')}"
        )
        if will_write:
            cur.execute(
                """
                INSERT INTO vehicle_valuations
                    (vrm, mileage, fetched_at, source, checked_by,
                     at_retail_valuation, at_rating, at_days_to_sell, at_market_condition,
                     cap_live_clean, cap_live_retail, cap_monthly_clean, cap_monthly_retail,
                     cap_rounded_mileage)
                VALUES (%s, %s, NOW(), 'manual', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    vrm,
                    round_mileage_to_nearest_thousand(_to_int(mileage) or 0),
                    checked_by,
                    _to_int(at.get('retail_valuation')),
                    _to_float(at.get('retail_rating')),
                    _to_float(at.get('days_to_sell')),
                    _to_float(at.get('market_condition')),
                    _to_int(cap.get('live_clean')),
                    _to_int(cap.get('live_retail')),
                    _to_int(cap.get('monthly_clean')),
                    _to_int(cap.get('monthly_retail')),
                    _to_int(cap.get('rounded_mileage')),
                ),
            )
    conn.commit()


def _parse_at_date(value):
    """AT returns firstRegistrationDate as 'YYYY-MM-DD'; the column is a DATE."""
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


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
