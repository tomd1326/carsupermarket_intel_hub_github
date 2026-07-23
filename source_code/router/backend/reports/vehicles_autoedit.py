"""
"Vehicles - AutoEdit" report engine.

Reconstructs the retired Nexus Point Apex "Vehicles - AutoEdit" report
(default 37-column view) from Snowflake base tables plus incremental Nexus
Point Data API requests, so the output is up to the minute regardless of the
3-hourly dbt / hourly ingestion schedules.

How it stays fast: the Snowflake raw tables mirror the API JSON, so the bulk
of the data comes from one warehouse round-trip; only rows whose updated_at
is newer than each table's high-water mark are fetched from the API.

Column provenance (reverse-engineered against a real AutoEdit export):
  - CONFIRMED: Purchase (Net), VATQualifying, Price, SIV (= purchase + costs),
    IsCommercial, ExtrasSpec (= description), Optional Extras (+ Cost),
    SpecialOffer (= interest-free HP finance product)
  - EMPTY (not available via the Data API): Tags, Notes, Standard Equipment,
    Classified Features, Recon
  - BEST-EFFORT (unverified vs the retired report): Sold, Costs, Writedown

Every Nexus-derived column (including Optional Extras, parsed in Python from
the vehicle row's vehicle_added_specification) is computed from the merged
Snowflake+API state, so it reflects the API as of the run. Only Costs and
Writedown (AutoView, not in the Data API) lag at the compute layer's cadence.

Ported from carsupermarket_nexus_point_ingestion/test.py.
"""
import datetime
import io
import json
import logging

from openpyxl import Workbook

from reports.nexus_source import fetch_updates_since, max_updated_at, parse_datetime
from utils.file_handler import set_default_font
from utils.snowflake_client import SnowflakeClient

# Fixed row height (points) for every data row, so long free-text columns
# (e.g. ExtrasSpec) never trigger auto row-expand/wrap in Excel/Sheets.
_ROW_HEIGHT = 15

logger = logging.getLogger(__name__)

# Exact header order/labels from the real AutoEdit export
OUTPUT_COLUMNS = [
    'Company', 'Supplier', 'DaysInStock', 'StockID', 'Registration', 'Make',
    'Model', 'Variant', 'NoDoors', 'BodyStyle', 'ExtrasSpec', 'Colour',
    'Mileage', 'SIV', 'VATQualifying', 'Price', 'Sold', 'Location',
    'PhotoCount', 'CapID', 'DateFirstRegistered', 'Status', 'AttentionGrabber',
    'IsCommercial', 'SpecialOffer', 'Tags', 'Notes', 'Standard Equipment',
    'Classified Features', 'Costs', 'Purchase (Net)', 'Optional Extras',
    'Optional Extras Cost', 'Recon', 'Writedown', 'Date First Advertised',
    'Date Last Service',
]

VEHICLE_BASE_QUERY = """
SELECT
    id, cap_id, cap_database, stock_id, vrm, location_id,
    vehicle_makes_name, vehicle_ranges_name, vehicle_body_types_name,
    status, description, discount_price, derivative, date_first_advertised,
    date_first_registered, colour, doors, updated_at, category, vat_scheme,
    purchase_price_ex_vat, price_ex_vat, date_stock_in, department_id,
    supplier_id, date_ordered, reservation_flags, photo_count,
    finance_products, vehicle_added_specification, attention_grabber,
    odometer_value, next_service_due
FROM SOURCE_VEHICLES.VEHICLES
WHERE status NOT IN ('SOLD', 'RETURNED')
"""

# Watermark must span the FULL table: vehicles excluded by the status filter
# would otherwise drag the watermark back and bloat the API delta.
VEHICLE_WATERMARK_QUERY = "SELECT MAX(updated_at) AS updated_at FROM SOURCE_VEHICLES.VEHICLES"

LOCATIONS_BASE_QUERY = "SELECT id, name, updated_at FROM SOURCE_VEHICLES.VEHICLES_LOCATIONS"

ORG_UNITS_BASE_QUERY = "SELECT id, name, updated_at FROM SOURCE_COMPANY.ORGANISATION_UNITS"

SUPPLIERS_BASE_QUERY = "SELECT id, name, updated_at FROM SOURCE_PURCHASES_B2B.CONTACTS_SUPPLIERS"

# Costs/Writedown are AutoView-sourced (not in the Data API); best-effort
# from the 3-hourly compute layer
ENRICHMENT_QUERY = """
SELECT vehicle_id, vehicle_costs, write_down_amount
FROM COMPUTE_VEHICLES.VEHICLES
WHERE vehicle_status NOT IN ('SOLD', 'RETURNED')
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _query_dicts(snowflake_client, sql):
    rows, cols = snowflake_client.execute(sql)
    cols = [c.lower() for c in cols]
    return [dict(zip(cols, row)) for row in rows]


def parse_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    dt = parse_datetime(value)
    return dt.date() if dt else None


def to_number(value):
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def merge_delta(base_rows, delta_rows):
    merged = {row['id']: row for row in base_rows if row.get('id') is not None}
    for row in delta_rows:
        if row.get('id') is not None:
            merged[row['id']] = row
    return merged


def get_active_options(vehicle_added_specification):
    """Active options from the vehicle's added-specification JSON; mirrors
    the vehicles_options__c LATERAL FLATTEN (active = 1, deduped by
    short_description). Works on both the Snowflake VARIANT (JSON string)
    and the API's native list."""
    spec = vehicle_added_specification
    if spec is None:
        return []
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except (ValueError, TypeError):
            return []
    if not isinstance(spec, list):
        return []
    options = []
    seen = set()
    for item in spec:
        if not isinstance(item, dict):
            continue
        if to_number(item.get('active')) != 1:
            continue
        description = item.get('short_description')
        if description in seen:
            continue
        seen.add(description)
        options.append({'option_description': description, 'option_price': item.get('price')})
    return options


def get_hp_value(finance_products):
    """First finance product with key 'HP'; mirrors vehicles__c's LATERAL FLATTEN."""
    if finance_products is None:
        return None
    if isinstance(finance_products, str):
        try:
            finance_products = json.loads(finance_products)
        except (ValueError, TypeError):
            return None
    if isinstance(finance_products, dict):
        finance_products = [finance_products]
    if not isinstance(finance_products, list):
        return None
    for item in finance_products:
        if isinstance(item, dict) and item.get('key') == 'HP':
            value = item.get('value')
            if value is None or str(value) == 'null':
                return None
            return to_number(value)
    return None


def fmt_date(d):
    return d.strftime('%d/%m/%Y') if d else ''


# ---------------------------------------------------------------------------
# Report row
# ---------------------------------------------------------------------------

def build_row(v, locations, org_units, suppliers, enrichment, today):
    vehicle_id = v.get('id')
    enr = enrichment.get(vehicle_id, {})
    vat_scheme = v.get('vat_scheme')
    vat_qualifying = vat_scheme in ('Qualifying', 'Commercial')

    purchase_price_ex_vat = to_number(v.get('purchase_price_ex_vat')) or 0

    discount_price = to_number(v.get('discount_price'))
    price_ex_vat = to_number(v.get('price_ex_vat'))

    if discount_price is not None and discount_price > 0:
        price = discount_price
    elif vat_qualifying and price_ex_vat is not None and price_ex_vat > 0:
        price = round(price_ex_vat * 1.2, 2)
    elif price_ex_vat is not None and price_ex_vat > 0:
        price = price_ex_vat
    else:
        price = None

    vehicle_costs = to_number(enr.get('vehicle_costs')) or 0
    write_down_amount = to_number(enr.get('write_down_amount')) or 0

    # SIV = purchase_price_ex_vat + Costs, confirmed against a real export
    siv = round(purchase_price_ex_vat + vehicle_costs, 2)

    hp_value = get_hp_value(v.get('finance_products'))
    interest_free = hp_value == 0  # confirmed: SpecialOffer = interest_free

    location_name = locations.get(v.get('location_id'), {}).get('name')
    vehicle_location = location_name.upper() if location_name else None

    org_name = org_units.get(v.get('department_id'), {}).get('name')
    site_name = org_name.replace('CarSupermarket.com ', '') if org_name else org_name

    supplier_name = suppliers.get(v.get('supplier_id'), {}).get('name')

    date_stock_in = parse_date(v.get('date_stock_in'))
    days_in_stock = (today - date_stock_in).days if date_stock_in else None

    # Best-effort, unverified: the retired AutoEdit Sold logic didn't match a
    # static rule derived from status/date_ordered/reservation_flags.
    sold = bool(v.get('date_ordered') is not None and v.get('reservation_flags'))

    is_commercial = ('van' in (v.get('category') or '').lower()
                     or (v.get('cap_database') or '').lower() == 'lcv')

    opts = get_active_options(v.get('vehicle_added_specification'))
    optional_extras = ' || '.join(o['option_description'] for o in opts if o.get('option_description'))
    optional_extras_cost = round(sum(to_number(o.get('option_price')) or 0 for o in opts), 2) if opts else 0

    cap_id = None
    try:
        cap_id = int(float(v.get('cap_id')))
    except (TypeError, ValueError):
        pass
    if cap_id == 0:
        cap_id = None

    photo_count = v.get('photo_count')
    if photo_count in (None, 'null', ''):
        photo_count = 0
    else:
        try:
            photo_count = int(str(photo_count).strip('"'))
        except (TypeError, ValueError):
            photo_count = 0

    return {
        'Company': site_name,
        'Supplier': supplier_name,
        'DaysInStock': days_in_stock,
        'StockID': v.get('stock_id'),
        'Registration': v.get('vrm'),
        'Make': (v.get('vehicle_makes_name') or '').strip() or None,
        'Model': (v.get('vehicle_ranges_name') or '').strip() or None,
        'Variant': (v.get('derivative') or '').strip() or None,
        'NoDoors': v.get('doors'),
        'BodyStyle': v.get('vehicle_body_types_name'),
        'ExtrasSpec': v.get('description'),
        'Colour': v.get('colour'),
        'Mileage': int(to_number(v.get('odometer_value')) or 0),
        'SIV': siv,
        'VATQualifying': vat_qualifying,
        'Price': price,
        'Sold': sold,
        'Location': vehicle_location,
        'PhotoCount': photo_count,
        'CapID': cap_id,
        'DateFirstRegistered': fmt_date(parse_date(v.get('date_first_registered'))),
        'Status': v.get('status'),
        'AttentionGrabber': v.get('attention_grabber'),
        'IsCommercial': is_commercial,
        'SpecialOffer': interest_free,
        'Tags': '',                # not available via Data API
        'Notes': '',               # not available via Data API
        'Standard Equipment': '',  # not available via Data API
        'Classified Features': '', # not available via Data API
        'Costs': vehicle_costs,
        'Purchase (Net)': purchase_price_ex_vat,
        'Optional Extras': optional_extras,
        'Optional Extras Cost': optional_extras_cost,
        'Recon': '',               # AutoView-sourced, not in Data API
        'Writedown': write_down_amount,
        'Date First Advertised': fmt_date(parse_date(v.get('date_first_advertised'))),
        'Date Last Service': fmt_date(parse_date(v.get('next_service_due'))),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_report(progress_cb=None):
    """Build the report and return the xlsx content as bytes.

    progress_cb(pct: int, stage: str) is called at each stage.
    """
    # Stage percentages are proportional to measured wall-clock time of a
    # representative run, so the bar advances smoothly rather than jumping.
    last_pct = [-1]

    def progress(pct, stage):
        pct = int(pct)
        if pct <= last_pct[0]:
            return  # skip duplicate/backward writes (each one is an S3 put)
        last_pct[0] = pct
        logger.info('AutoEdit report: %d%% - %s', pct, stage)
        if progress_cb:
            progress_cb(pct, stage)

    progress(2, 'Connecting to Snowflake')
    with SnowflakeClient() as snowflake_client:
        progress(10, 'Loading vehicles from Snowflake')
        vehicles_base = _query_dicts(snowflake_client, VEHICLE_BASE_QUERY)
        progress(22, 'Loading lookups from Snowflake')
        locations_base = _query_dicts(snowflake_client, LOCATIONS_BASE_QUERY)
        org_units_base = _query_dicts(snowflake_client, ORG_UNITS_BASE_QUERY)
        suppliers_base = _query_dicts(snowflake_client, SUPPLIERS_BASE_QUERY)
        progress(27, 'Loading costs from Snowflake')
        enrichment_rows = _query_dicts(snowflake_client, ENRICHMENT_QUERY)
        vehicle_watermark_rows = _query_dicts(snowflake_client, VEHICLE_WATERMARK_QUERY)

    logger.info(
        'Snowflake base loaded: %d vehicles, %d locations, %d org units, %d suppliers, '
        '%d enrichment rows',
        len(vehicles_base), len(locations_base), len(org_units_base),
        len(suppliers_base), len(enrichment_rows)
    )

    sources = [
        ('vehicles', 'vehicles/', vehicles_base, max_updated_at(vehicle_watermark_rows), None, 32, 68),
        ('locations', 'vehicles/locations', locations_base, max_updated_at(locations_base), None, 68, 73),
        ('org_units', 'organisation-units/', org_units_base, max_updated_at(org_units_base), None, 73, 78),
        ('suppliers', 'contacts/', suppliers_base, max_updated_at(suppliers_base),
         {'contact_type': 'Supplier'}, 78, 83),
    ]
    merged = {}
    for name, route, base_rows, sf_watermark, extra_params, start_pct, end_pct in sources:
        stage = f'Fetching live updates: {route}'
        progress(start_pct, stage)

        def page_progress(done, total, _start=start_pct, _end=end_pct, _stage=stage):
            progress(_start + (_end - _start) * done / total, _stage)

        # Shared per-endpoint delta cache + API top-up (reports/nexus_source):
        # returns everything changed since the Snowflake watermark, ordered so
        # live API state wins on id conflict over cached rows over base rows.
        fresh_rows = fetch_updates_since(route, sf_watermark, extra_params, page_progress)
        merged[name] = merge_delta(base_rows, fresh_rows)

    progress(84, 'Building report')
    enrichment = {row['vehicle_id']: row for row in enrichment_rows}

    today = datetime.date.today()
    rows = []
    for v in merged['vehicles'].values():
        if v.get('status') in ('SOLD', 'RETURNED'):
            continue
        rows.append(build_row(v, merged['locations'], merged['org_units'],
                              merged['suppliers'], enrichment, today))
    rows.sort(key=lambda r: -(r['DaysInStock'] or 0))

    progress(88, 'Writing report')
    wb = Workbook()
    set_default_font(wb)
    ws = wb.active
    ws.title = 'Vehicles - AutoEdit'
    ws.append(OUTPUT_COLUMNS)

    # An explicit row height pins every row regardless of embedded newlines
    # in the free-text columns; per-cell wrap_text styling is unnecessary and
    # was the slowest part of the whole run (~30s for 108k cells).
    total_rows = len(rows) or 1
    for i, row in enumerate(rows):
        ws.append([('' if row.get(col) is None else row.get(col)) for col in OUTPUT_COLUMNS])
        ws.row_dimensions[ws.max_row].height = _ROW_HEIGHT
        if i % 500 == 0:
            progress(88 + 5 * i / total_rows, 'Writing report')

    progress(94, 'Writing report')
    buf = io.BytesIO()
    wb.save(buf)

    logger.info('AutoEdit report built: %d rows', len(rows))
    return buf.getvalue()
