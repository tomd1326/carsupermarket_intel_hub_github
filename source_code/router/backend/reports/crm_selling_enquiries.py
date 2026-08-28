"""
"CRM - Selling - Enquiries" report engine.

Replicates the Apex "CRM - Selling - Enquiries (v2)" report from
COMPUTE_SALES_B2C.ENQUIRIES, which carries every column the report needs -
the enquiry's own site, the vehicle's site, the contact's address lines and
unmodified phone numbers were all added to that model for this report.

Unlike Vehicles - AutoEdit there is no live API top-up. Enquiries are reported
on completed days rather than as a live worklist, so the dbt model's own
schedule is close enough; adding a Nexus delta fetch would mean re-deriving the
model's joins in Python for a handful of rows.

Verified against the Apex export for 2026-08-09: 580 rows both sides, all 19
site values matching. The only differences were rows edited in Apex after the
export was taken, which a rebuilt table reflects and a frozen export does not.

Date range comes from the report parameters (date_from / date_to, inclusive,
on the enquiry's created date). Both default to yesterday - the most recent
complete day.

Attribution UTM columns are NOT a faithful replication: neither Apex version
parses them correctly (v1 finds 16 of 256 tagged rows for 2026-08-09 and leaves
campaign/content/term empty on every row; v2 prints "(unknown)" everywhere).
The real values are parsed here from the raw JSON, with "(unknown)" kept only
as the no-tagging default so the column reads like v2's.
"""
import datetime
import io
import json
import logging

from openpyxl import Workbook

from utils.file_handler import set_default_font
from utils.snowflake_client import SnowflakeClient

# Fixed row height (points) so the free-text Message column never triggers
# auto row-expand in Excel/Sheets. Matches the AutoEdit report.
_ROW_HEIGHT = 15

logger = logging.getLogger(__name__)

# (snowflake_column, output_header) in the order the Apex report shows them.
# Site Services and Site Manufacturer Name are Apex columns that are empty on
# every row ('[]' and blank respectively); they are emitted as literals below
# so the column set matches, and so are not listed here.
OUTPUT_COLUMNS = [
    ('ENQUIRY_ID', 'ID'),
    ('ENQUIRY_TYPE', 'Type'),
    ('ENQUIRY_SOURCE', 'Source'),
    ('FIRST_NAME', 'First Name'),
    ('LAST_NAME', 'Last Name'),
    ('EMAIL', 'Contact Email'),
    ('CONTACT_TELEPHONE', 'Contact Telephone'),
    ('CONTACT_MOBILE', 'Contact Mobile'),
    ('ADDRESS_LINE1', 'Address Line 1'),
    ('ADDRESS_LINE2', 'Address Line 2'),
    ('ADDRESS_LINE3', 'Address Line 3'),
    ('ADDRESS_LINE4', 'Address Line 4'),
    ('ADDRESS_LINE5', 'Address Line 5'),
    ('POSTCODE', 'Postcode'),
    ('ADVISOR_NAME', 'Advisor'),
    ('ASSIGNEE', 'Assignee'),
    ('ENQUIRY_SITE', 'Site'),
    ('ENQUIRY_VRM', 'Vehicle VRM'),
    ('ENQUIRY_STOCK_ID', 'Vehicle Stock ID'),
    ('MESSAGE', 'Message'),
    ('ENQUIRY_CREATED_AT', 'Date of Contact'),
    ('ATTRIBUTION_HISTORY', 'Attribution Raw'),
    ('ENQUIRY_STATUS', 'Status'),
    ('ADVERT_URL', 'Advert URL'),
    ('ENQUIRY_VEHICLE_SITE', 'Vehicle Site'),
]

# Permission columns, emitted as TRUE/FALSE like the Apex export.
_PERMISSION_COLUMNS = [
    ('CONTACT_PREFS_SMS', 'SMS Permission'),
    ('CONTACT_PREFS_EMAIL', 'Email Permission'),
    ('CONTACT_PREFS_TELEPHONE', 'Telephone Permission'),
    ('CONTACT_PREFS_POST', 'Postal Permission'),
]

# Parsed out of ATTRIBUTION_HISTORY in Python (see _parse_attribution).
_UTM_COLUMNS = [
    ('utm_source', 'Attribution UTM Source'),
    ('utm_medium', 'Attribution UTM Medium'),
    ('utm_campaign', 'Attribution UTM Campaign'),
    ('utm_content', 'Attribution UTM Content'),
    ('utm_term', 'Attribution UTM Term'),
]

_UNKNOWN = '(unknown)'

# Apex column order, including the ones filled with constants.
HEADERS = [
    'ID', 'Type', 'Source', 'First Name', 'Last Name', 'Contact Email',
    'Contact Telephone', 'Contact Mobile', 'Address Line 1', 'Address Line 2',
    'Address Line 3', 'Address Line 4', 'Address Line 5', 'Postcode',
    'Advisor', 'Assignee', 'Site', 'Site Services', 'Site Manufacturer Name',
    'Vehicle VRM', 'Vehicle Stock ID', 'Message', 'Date of Contact',
    'Attribution Raw', 'Attribution Channel', 'Attribution UTM Source',
    'Attribution UTM Medium', 'Attribution UTM Campaign',
    'Attribution UTM Content', 'Attribution UTM Term', 'SMS Permission',
    'Email Permission', 'Telephone Permission', 'Postal Permission',
    'Status', 'Advert URL', 'Vehicle Site', 'Vehicle Company',
]

ENQUIRIES_QUERY = """
SELECT
    ENQUIRY_ID, ENQUIRY_TYPE, ENQUIRY_SOURCE, FIRST_NAME, LAST_NAME, EMAIL,
    CONTACT_TELEPHONE, CONTACT_MOBILE,
    ADDRESS_LINE1, ADDRESS_LINE2, ADDRESS_LINE3, ADDRESS_LINE4, ADDRESS_LINE5,
    POSTCODE, ADVISOR_NAME, ASSIGNEE, ENQUIRY_SITE,
    ENQUIRY_VRM, ENQUIRY_STOCK_ID, MESSAGE, ENQUIRY_CREATED_AT, ATTRIBUTION_HISTORY,
    CONTACT_PREFS_SMS, CONTACT_PREFS_EMAIL, CONTACT_PREFS_TELEPHONE,
    CONTACT_PREFS_POST, ENQUIRY_STATUS, ADVERT_URL, ENQUIRY_VEHICLE_SITE
FROM PROD_CARSUPERMARKET_DB.COMPUTE_SALES_B2C.ENQUIRIES
WHERE ENQUIRY_CREATED_AT >= %s
  AND ENQUIRY_CREATED_AT < DATEADD(DAY, 1, %s)
ORDER BY ENQUIRY_ID DESC
"""

# The single COMPANY-type organisation unit ("CarSupermarket.com"), shown in
# the report's Vehicle Company column for every row that has a vehicle.
COMPANY_QUERY = """
SELECT MAX(NAME) AS COMPANY_NAME
FROM PROD_CARSUPERMARKET_DB.SOURCE_COMPANY.ORGANISATION_UNITS
WHERE TYPE = 'COMPANY' AND DELETED_AT IS NULL
"""


def _yesterday() -> datetime.date:
    return datetime.date.today() - datetime.timedelta(days=1)


def _coerce_date(value, fallback: datetime.date) -> datetime.date:
    """Accept a date, a YYYY-MM-DD string, or nothing."""
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.date.fromisoformat(value.strip()[:10])
        except ValueError:
            logger.warning('Unparseable report date %r - using %s', value, fallback)
    return fallback


def _scalar(value):
    """Flatten a UTM value to something Excel can hold.

    Bing traffic stores these as arrays rather than strings - usually the same
    value twice (["bing", "bing"]), but sometimes an id and a human label
    (["1269936924057497", "Sheffield > Exact"]). The first element is taken
    either way: it is the id, which is what Google rows put in the same field.
    """
    while isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if isinstance(value, dict):
        return None
    return value


def _parse_attribution(raw):
    """Pull the UTM tags off the last tagged touchpoint.

    attribution_history is a JSON array of touchpoints, oldest first, each one
    optionally carrying UTM tags. It sometimes arrives double-nested
    ([[{...}]]), so the outer array is unwrapped when present. The LAST
    touchpoint carrying a utm_source is the one that produced the enquiry -
    earlier ones are prior visits, later untagged ones are direct returns.
    """
    blank = {key: _UNKNOWN for key, _ in _UTM_COLUMNS}
    if not raw:
        return blank
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return blank
    if isinstance(parsed, list) and parsed and isinstance(parsed[0], list):
        parsed = parsed[0]
    if not isinstance(parsed, list):
        return blank

    latest = None
    for touchpoint in parsed:
        if isinstance(touchpoint, dict) and _scalar(touchpoint.get('utm_source')):
            latest = touchpoint
    if latest is None:
        return blank
    return {
        key: (_scalar(latest.get(key)) or _UNKNOWN) for key, _ in _UTM_COLUMNS
    }


def _cell(value):
    """Excel-safe scalar. Timestamps keep their time, dates stay dates.

    openpyxl raises on anything it cannot store in a cell, which fails the whole
    run for one odd row. Lists and dicts are serialised back to JSON rather than
    dropped, so nothing is silently lost.
    """
    if value is None:
        return ''
    if isinstance(value, datetime.datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value)
    if isinstance(value, (str, int, float, bool, datetime.date, datetime.time)):
        return value
    return str(value)


def run_report(progress_cb=None, params=None):
    """Build the report and return the xlsx content as bytes.

    progress_cb(pct: int, stage: str) is called at each stage.
    params may carry 'date_from' and 'date_to' (YYYY-MM-DD, inclusive); both
    default to yesterday.
    """
    last_pct = [-1]

    def progress(pct, stage):
        pct = int(pct)
        if pct <= last_pct[0]:
            return  # skip duplicate/backward writes (each one is an S3 put)
        last_pct[0] = pct
        logger.info('CRM Enquiries report: %d%% - %s', pct, stage)
        if progress_cb:
            progress_cb(pct, stage)

    params = params or {}
    default_day = _yesterday()
    date_from = _coerce_date(params.get('date_from'), default_day)
    date_to = _coerce_date(params.get('date_to'), default_day)
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    logger.info('CRM Enquiries report: %s to %s inclusive', date_from, date_to)

    progress(3, 'Connecting to Snowflake')
    with SnowflakeClient() as snowflake_client:
        progress(10, f'Loading enquiries for {date_from} to {date_to}')
        rows, cols = snowflake_client.execute(
            ENQUIRIES_QUERY, [str(date_from), str(date_to)]
        )
        progress(55, 'Loading company')
        company_rows, _ = snowflake_client.execute(COMPANY_QUERY)

    company_name = company_rows[0][0] if company_rows else None
    idx = {c.upper(): i for i, c in enumerate(cols)}
    logger.info('CRM Enquiries: %d rows loaded', len(rows))

    progress(60, 'Building report')

    def value(row, column):
        position = idx.get(column)
        return row[position] if position is not None else None

    built = []
    for row in rows:
        record = {header: _cell(value(row, col)) for col, header in OUTPUT_COLUMNS}
        for col, header in _PERMISSION_COLUMNS:
            record[header] = bool(value(row, col))
        record.update({
            header: _parse_attribution(value(row, 'ATTRIBUTION_HISTORY'))[key]
            for key, header in _UTM_COLUMNS
        })
        # Constant columns: Apex shows these but they are empty on every row.
        record['Site Services'] = '[]'
        record['Site Manufacturer Name'] = ''
        # Apex leaves Attribution Channel as (unknown) on the v2 report.
        record['Attribution Channel'] = _UNKNOWN
        # Company only applies where the enquiry has a vehicle.
        record['Vehicle Company'] = company_name if record['Vehicle Site'] else ''
        built.append(record)

    progress(75, 'Writing report')
    workbook = Workbook()
    set_default_font(workbook)
    sheet = workbook.active
    sheet.title = 'CRM - Selling - Enquiries'
    sheet.append(HEADERS)

    total_rows = len(built) or 1
    for i, record in enumerate(built):
        sheet.append([record.get(header, '') for header in HEADERS])
        sheet.row_dimensions[sheet.max_row].height = _ROW_HEIGHT
        if i % 500 == 0:
            progress(75 + 20 * i / total_rows, 'Writing report')

    progress(96, 'Writing report')
    buffer = io.BytesIO()
    workbook.save(buffer)

    logger.info('CRM Enquiries report built: %d rows', len(built))
    return buffer.getvalue()
