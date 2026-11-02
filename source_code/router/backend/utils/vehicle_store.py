import datetime
import logging
import re

import psycopg2
import psycopg2.extras

from utils.ddl_once import once_per_database

logger = logging.getLogger(__name__)

VALUATION_TTL_HOURS = 36

# Rows per round trip for the bulk writes below. execute_values defaults to
# 100, which on a 5,000 vehicle supplier file meant roughly 250 round trips
# across the five writes — about 63 seconds at the ~250ms it costs to reach
# Aurora from a desk. These rows are small (no raw API responses are stored),
# so a far larger page is comfortably within the statement size limit.
WRITE_PAGE_SIZE = 1000


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
        # The chosen spec is a fact about the car, so it lives on the vehicle
        # and is reused by every later valuation of that VRM. Autotrader
        # resolves real VIN spec for some vehicles and not others — when it is
        # available it seeds this column automatically (see at_vin_spec_features
        # below); otherwise these are picked by hand.
        cur.execute(
            "ALTER TABLE vehicles "
            "ADD COLUMN IF NOT EXISTS at_selected_features TEXT, "
            "ADD COLUMN IF NOT EXISTS at_selected_features_at TIMESTAMPTZ, "
            "ADD COLUMN IF NOT EXISTS at_selected_features_by VARCHAR(100)"
        )
        # What Autotrader itself reports as factory-fitted (factoryFitted: true
        # on the VIN), kept separately from at_selected_features. The two can
        # diverge on purpose — a VIN spec item can be un-ticked if it looks
        # wrong, or extras added on top that VIN spec does not know about — so
        # this column is what lets the page keep showing "this came from VIN
        # spec" even after the buyer has turned it off.
        cur.execute(
            "ALTER TABLE vehicles "
            "ADD COLUMN IF NOT EXISTS at_vin_spec_features TEXT, "
            "ADD COLUMN IF NOT EXISTS at_vin_spec_checked_at TIMESTAMPTZ"
        )
        cur.execute(
            "ALTER TABLE vehicle_valuations "
            "ADD COLUMN IF NOT EXISTS source VARCHAR(20) NOT NULL DEFAULT 'evaluator', "
            "ADD COLUMN IF NOT EXISTS checked_by VARCHAR(100), "
            # `mileage` remains the rounded cache key. Keep the supplier's or
            # user's actual odometer reading separately for check history.
            "ADD COLUMN IF NOT EXISTS reported_mileage INTEGER"
        )
        # Three different retail figures, all kept so they can be compared:
        #   at_retail_valuation_average - average spec for the derivative. The
        #       inline figure from /vehicles, and the baseline the Autotrader
        #       Portal measures its "Spec: £N above/below average" against.
        #   at_retail_valuation_base    - no extras at all (/valuations with an
        #       empty features list). Lower than average on most cars.
        #   at_retail_valuation_spec    - this car's chosen extras.
        # Verified 2026-08-21 on DA66VGN: 11294 average, 11107 none, 11647 spec,
        # reproducing the Portal's "-187" and "+353" exactly.
        cur.execute(
            "ALTER TABLE vehicle_valuations "
            "ADD COLUMN IF NOT EXISTS at_retail_valuation_average INTEGER, "
            "ADD COLUMN IF NOT EXISTS at_retail_valuation_base INTEGER, "
            "ADD COLUMN IF NOT EXISTS at_retail_valuation_spec INTEGER, "
            "ADD COLUMN IF NOT EXISTS at_spec_features TEXT, "
            "ADD COLUMN IF NOT EXISTS at_spec_updated_at TIMESTAMPTZ, "
            "ADD COLUMN IF NOT EXISTS at_spec_updated_by VARCHAR(100)"
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
        # db/08_cap_images.sql is the canonical schema for this one.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cap_images (
                cap_id              INTEGER         NOT NULL,
                registration_year   SMALLINT        NOT NULL,
                image               BYTEA,
                content_type        VARCHAR(50),
                width               SMALLINT,
                height              SMALLINT,
                not_available       BOOLEAN         NOT NULL DEFAULT FALSE,
                fetched_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
                PRIMARY KEY (cap_id, registration_year)
            )
            """
        )
        # db/09_spec_checks.sql is the canonical schema for this one.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS vehicle_spec_checks (
                vrm                 VARCHAR(10)     PRIMARY KEY,
                status_code         SMALLINT        NOT NULL,
                status_message      TEXT,
                base_price          NUMERIC(10, 2),
                options_price       NUMERIC(10, 2),
                total_price         NUMERIC(10, 2),
                options_rating      NUMERIC(3, 1),
                options             JSONB,
                option_count        SMALLINT,
                optional_count      SMALLINT,
                fetched_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
                fetched_by          VARCHAR(100)
            )
            """
        )
    conn.commit()


def fetch_selected_features(conn, vrm: str) -> list:
    """The spec chosen for this VRM, as a list of Autotrader feature names."""
    with conn.cursor() as cur:
        cur.execute("SELECT at_selected_features FROM vehicles WHERE vrm = %s", (vrm,))
        row = cur.fetchone()
    if not row or not row[0]:
        return []
    return [n for n in (part.strip() for part in row[0].split('|')) if n]


def save_selected_features(conn, vrm: str, features: list, chosen_by: str) -> None:
    """
    Store the spec for a VRM, replacing whatever was chosen before.

    Names are joined with a pipe rather than a comma because Autotrader feature
    names contain commas and hyphens of their own (e.g. "Tow Bar - Manually
    Folding - Includes 13-Pin Socket").
    """
    joined = '|'.join(n.strip() for n in features if n and n.strip()) or None
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE vehicles
            SET at_selected_features    = %s,
                at_selected_features_at = NOW(),
                at_selected_features_by = %s
            WHERE vrm = %s
            """,
            (joined, chosen_by, vrm),
        )
    conn.commit()


def _split_features(joined) -> list:
    if not joined:
        return []
    return [n for n in (part.strip() for part in joined.split('|')) if n]


def fetch_vin_spec_features(conn, vrm: str) -> list:
    """What Autotrader itself reports as factory-fitted for this VIN, if any."""
    with conn.cursor() as cur:
        cur.execute("SELECT at_vin_spec_features FROM vehicles WHERE vrm = %s", (vrm,))
        row = cur.fetchone()
    return _split_features(row[0]) if row else []


def save_vin_spec_features(conn, vrm: str, features: list) -> None:
    """
    Record what Autotrader reports as factory-fitted for this VIN.

    Kept separately from at_selected_features so the page can always show which
    extras came from Autotrader's own VIN data, even after a buyer un-ticks one
    they believe is wrong or adds extras on top that VIN spec does not cover.
    A no-op when the vehicle has no VIN spec (features is empty) — nothing to
    record, and this must never overwrite a previous real result with nothing.
    """
    if not features:
        return
    joined = '|'.join(n.strip() for n in features if n and n.strip()) or None
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE vehicles
            SET at_vin_spec_features    = %s,
                at_vin_spec_checked_at  = NOW()
            WHERE vrm = %s
            """,
            (joined, vrm),
        )
    conn.commit()


def has_confirmed_spec(conn, vrm: str) -> bool:
    """
    Whether a spec has ever been set for this VRM — by VIN-spec seeding or a
    manual Apply — as opposed to simply never having been looked at yet.

    Used to tell "no extras, because nobody has checked" (should value at the
    derivative's average, same as the Portal shows before a spec is entered)
    apart from "no extras, confirmed" (should value at the no-extras base).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT at_selected_features_at FROM vehicles WHERE vrm = %s", (vrm,)
        )
        row = cur.fetchone()
    return bool(row and row[0] is not None)


def ensure_vin_spec_seeded(conn, vrm: str, vin_features: list, chosen_by: str) -> None:
    """
    First-check seeding: if Autotrader has VIN spec and nothing has ever been
    manually chosen for this VRM, use the VIN spec as the starting selection.

    Only fires once per vehicle — as soon as at_selected_features_at is set
    (whether by this seeding or a manual Apply), it is left alone. A buyer who
    un-ticks a VIN-spec item is making a deliberate choice; this must never
    silently re-tick it on a later check.
    """
    if not vin_features:
        return
    with conn.cursor() as cur:
        cur.execute(
            "SELECT at_selected_features_at FROM vehicles WHERE vrm = %s", (vrm,)
        )
        row = cur.fetchone()
    if row and row[0] is not None:
        return
    save_selected_features(conn, vrm, vin_features, chosen_by)


def save_spec_valuation(conn, valuation_id: int, average_valuation, base_valuation,
                        spec_valuation, features: list, updated_by: str) -> None:
    """
    Put the three retail figures on an existing valuation row.

    Updates in place and deliberately leaves at_retail_valuation alone — that
    stays as whatever the original check recorded, so nothing rewrites what was
    quoted at the time.
    """
    joined = '|'.join(n.strip() for n in features if n and n.strip()) or None
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE vehicle_valuations
            SET at_retail_valuation_average = %s,
                at_retail_valuation_base    = %s,
                at_retail_valuation_spec    = %s,
                at_spec_features            = %s,
                at_spec_updated_at          = NOW(),
                at_spec_updated_by          = %s
            WHERE id = %s
            """,
            (_to_int(average_valuation), _to_int(base_valuation),
             _to_int(spec_valuation), joined, updated_by, valuation_id),
        )
    conn.commit()


def fetch_latest_valuation_id(conn, vrm: str, mileage: int):
    """
    Row id of the most recent valuation for this VRM and mileage, or None.

    Rounds the mileage the same way fetch_latest_valuation does, so both find
    the same row from a raw odometer reading.
    """
    rounded = round_mileage_to_nearest_thousand(_to_int(mileage) or 0)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM vehicle_valuations
            WHERE vrm = %s AND mileage = %s
            ORDER BY fetched_at DESC
            LIMIT 1
            """,
            (vrm, rounded),
        )
        row = cur.fetchone()
    return row[0] if row else None


# CAP has no image for plenty of vehicles. Once recorded, that answer is reused
# for this long before asking again, so a missing picture costs one call a month
# rather than one per popup open.
MISSING_IMAGE_RETRY_DAYS = 30


def fetch_cap_image(conn, cap_id: int, registration_year: int) -> dict | None:
    """
    Stored CAP image for a CAP id + registration year.

    Returns None when nothing is stored, or when a "no image" answer has gone
    stale and is worth asking about again. A stored not_available row inside the
    retry window comes back as a row with image=None, so the caller can tell
    "CAP says there is no picture" apart from "never asked".
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT image, content_type, not_available, fetched_at
            FROM cap_images
            WHERE cap_id = %s AND registration_year = %s
            """,
            (cap_id, registration_year),
        )
        row = cur.fetchone()

    if not row:
        return None

    if row['not_available']:
        age = datetime.datetime.now(datetime.timezone.utc) - row['fetched_at']
        if age.days >= MISSING_IMAGE_RETRY_DAYS:
            return None

    return dict(row)


def save_cap_image(conn, cap_id: int, registration_year: int, image: bytes,
                   content_type: str, width: int, height: int) -> None:
    """Store an image, or record that CAP has none. Pass image=None for the latter."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO cap_images
                (cap_id, registration_year, image, content_type, width, height,
                 not_available, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (cap_id, registration_year) DO UPDATE SET
                image         = EXCLUDED.image,
                content_type  = EXCLUDED.content_type,
                width         = EXCLUDED.width,
                height        = EXCLUDED.height,
                not_available = EXCLUDED.not_available,
                fetched_at    = NOW()
            """,
            (cap_id, registration_year,
             psycopg2.Binary(image) if image else None,
             content_type, width, height, image is None),
        )
    conn.commit()


def fetch_spec_check(conn, vrm: str) -> dict | None:
    """
    Stored HPI Spec Check for this VRM, or None if it has never been run.

    No age check on purpose. Factory options were fitted when the car was built
    and never change, so a stored result is never out of date — and every fresh
    call is billable. A 103 "no data" answer is stored and returned like any
    other, so a vehicle HPI has nothing for is not paid for twice.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT vrm, status_code, status_message, base_price, options_price,
                   total_price, options_rating, options, option_count,
                   optional_count, fetched_at, fetched_by
            FROM vehicle_spec_checks
            WHERE vrm = %s
            """,
            (vrm,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def save_spec_check(conn, vrm: str, result: dict, fetched_by: str) -> None:
    """
    Store a Spec Check result, replacing any earlier one for this VRM.

    Called for a 103 "not available" answer as well as a successful one — that
    is a real answer from HPI and storing it is what stops the same vehicle
    being re-requested and re-billed.
    """
    import json

    options = result.get('options') or []
    optional = [o for o in options if o.get('option_kind') == 'Optional']
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO vehicle_spec_checks
                (vrm, status_code, status_message, base_price, options_price,
                 total_price, options_rating, options, option_count,
                 optional_count, fetched_at, fetched_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
            ON CONFLICT (vrm) DO UPDATE SET
                status_code    = EXCLUDED.status_code,
                status_message = EXCLUDED.status_message,
                base_price     = EXCLUDED.base_price,
                options_price  = EXCLUDED.options_price,
                total_price    = EXCLUDED.total_price,
                options_rating = EXCLUDED.options_rating,
                options        = EXCLUDED.options,
                option_count   = EXCLUDED.option_count,
                optional_count = EXCLUDED.optional_count,
                fetched_at     = NOW(),
                fetched_by     = EXCLUDED.fetched_by
            """,
            (
                vrm,
                result.get('status_code'),
                result.get('status_message'),
                result.get('base_price'),
                result.get('options_price'),
                result.get('total_price'),
                result.get('options_rating'),
                json.dumps(options) if options else None,
                len(options),
                len(optional),
                fetched_by,
            ),
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
            SELECT id, vrm, mileage, fetched_at, source, checked_by,
                   at_retail_valuation, at_rating, at_days_to_sell, at_market_condition,
                   at_retail_valuation_average, at_retail_valuation_base,
                   at_retail_valuation_spec, at_spec_features, at_spec_updated_at,
                   at_spec_updated_by,
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
            SELECT v.id, v.fetched_at, COALESCE(v.reported_mileage, v.mileage) AS mileage,
                   v.source, v.checked_by,
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


def fetch_recent_checks(conn, days: int = 30, limit: int = 10,
                        offset: int = 0) -> list[dict]:
    """
    One row per VRM for every vehicle checked in the last `days`, from supplier
    list runs and manual lookups alike, newest first.

    DISTINCT ON keeps only the most recent check per VRM, so a vehicle that
    appears on a supplier list every night shows up once rather than 30 times.
    check_count is the number of checks in the window, so the list can show how
    often a vehicle has been seen. total_count is repeated on each returned row
    so the caller can build pagination without running a second query.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            WITH recent AS (
                SELECT DISTINCT ON (vrm)
                       vrm, mileage, reported_mileage, fetched_at, source, checked_by,
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
                   (SELECT COUNT(*) FROM recent) AS total_count,
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
            LIMIT %s OFFSET %s
            """,
            (days, days, limit, offset),
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
            page_size=WRITE_PAGE_SIZE,
        )


BUYER_FIGURES = ('rrp', 'est_prep_cost')


@once_per_database
def _ensure_buyer_figures_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS vehicle_buyer_figures (
                vrm VARCHAR(20) PRIMARY KEY REFERENCES vehicles (vrm),
                rrp NUMERIC(12, 2),
                est_prep_cost NUMERIC(12, 2),
                updated_by VARCHAR(100),
                updated_at TIMESTAMP
            )
            """
        )
    conn.commit()


def fetch_buyer_figures(conn, vrms: list[str]) -> dict[str, dict]:
    """The buyer's current figures, for the VRMs that have any: {vrm: {rrp,
    est_prep_cost, updated_by, updated_at}}."""
    if not vrms:
        return {}
    _ensure_buyer_figures_table(conn)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT vrm, rrp, est_prep_cost, updated_by, updated_at FROM vehicle_buyer_figures WHERE vrm IN %s",
            (tuple(vrms),)
        )
        rows = cur.fetchall()
    conn.commit()
    return {r['vrm']: {'rrp': _to_float(r['rrp']), 'est_prep_cost': _to_float(r['est_prep_cost']),
                       'updated_by': r['updated_by'], 'updated_at': r['updated_at']} for r in rows}


def save_buyer_figures(conn, rows: list[dict], user: str) -> None:
    """Rows of {vrm, rrp and/or est_prep_cost}. Only the figures a row carries
    change, and None clears one. Called by every page buyers enter them on."""
    rows = [r for r in rows if any(f in r for f in BUYER_FIGURES)]
    if not rows:
        return
    _ensure_buyer_figures_table(conn)
    ensure_vehicles_exist(conn, [r['vrm'] for r in rows])
    now = datetime.datetime.utcnow()
    with conn.cursor() as cur:
        for figure in BUYER_FIGURES:
            values = [(r['vrm'], r[figure], user, now) for r in rows if figure in r]
            if values:
                psycopg2.extras.execute_values(
                    cur,
                    f"""
                    INSERT INTO vehicle_buyer_figures (vrm, {figure}, updated_by, updated_at)
                    VALUES %s
                    ON CONFLICT (vrm) DO UPDATE SET
                        {figure}   = EXCLUDED.{figure},
                        updated_by = EXCLUDED.updated_by,
                        updated_at = EXCLUDED.updated_at
                    """,
                    values,
                    page_size=WRITE_PAGE_SIZE,
                )
    conn.commit()


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
            page_size=WRITE_PAGE_SIZE,
        )


def insert_valuations(conn, valuations: list[dict]) -> None:
    """
    Bulk insert new valuation rows. Always appends — never updates existing rows.
    The cache key is rounded to the nearest 1000 to match
    fetch_cached_valuations; reported_mileage preserves the source value.
    """
    if not valuations:
        return

    rows = [
        (
            v['vrm'],
            round_mileage_to_nearest_thousand(_to_int(v.get('mileage'))),
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
                (vrm, mileage, reported_mileage, fetched_at,
                 at_retail_valuation, at_rating, at_days_to_sell, at_market_condition,
                 cap_live_clean, cap_live_retail, cap_monthly_clean, cap_monthly_retail, cap_rounded_mileage)
            VALUES %s
            """,
            rows,
            page_size=WRITE_PAGE_SIZE,
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
                    (vrm, mileage, reported_mileage, fetched_at, source, checked_by,
                     at_retail_valuation, at_rating, at_days_to_sell, at_market_condition,
                     cap_live_clean, cap_live_retail, cap_monthly_clean, cap_monthly_retail,
                     cap_rounded_mileage)
                VALUES (%s, %s, %s, NOW(), 'manual', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    vrm,
                    round_mileage_to_nearest_thousand(_to_int(mileage) or 0),
                    _to_int(mileage),
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


def copy_check_for_mileage(conn, vrm: str, previous_mileage: int,
                           mileage: int, checked_by: str,
                           check_id: int | None = None) -> bool:
    """Record an exact-mileage reassessment without revaluing its cache bucket."""
    previous_bucket = round_mileage_to_nearest_thousand(_to_int(previous_mileage) or 0)
    mileage_bucket = round_mileage_to_nearest_thousand(_to_int(mileage) or 0)
    if previous_bucket != mileage_bucket:
        return False

    source_filter = "id = %s AND vrm = %s" if check_id else "vrm = %s AND mileage = %s"
    source_params = (check_id, vrm) if check_id else (vrm, previous_bucket)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO vehicle_valuations
                (vrm, mileage, reported_mileage, fetched_at, source, checked_by,
                 at_retail_valuation, at_rating, at_days_to_sell, at_market_condition,
                 at_retail_valuation_average, at_retail_valuation_base,
                 at_retail_valuation_spec, at_spec_features, at_spec_updated_at,
                 at_spec_updated_by, cap_live_clean, cap_live_retail,
                 cap_monthly_clean, cap_monthly_retail, cap_rounded_mileage)
            SELECT vrm, %s, %s, NOW(), 'manual', %s,
                   at_retail_valuation, at_rating, at_days_to_sell, at_market_condition,
                   at_retail_valuation_average, at_retail_valuation_base,
                   at_retail_valuation_spec, at_spec_features, at_spec_updated_at,
                   at_spec_updated_by, cap_live_clean, cap_live_retail,
                   cap_monthly_clean, cap_monthly_retail, cap_rounded_mileage
            FROM vehicle_valuations
            WHERE """ + source_filter + """
            ORDER BY fetched_at DESC
            LIMIT 1
            RETURNING id
            """,
            (mileage_bucket, _to_int(mileage), checked_by, *source_params),
        )
        copied = cur.fetchone() is not None
    conn.commit()
    return copied


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
            page_size=WRITE_PAGE_SIZE,
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
            page_size=WRITE_PAGE_SIZE,
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
