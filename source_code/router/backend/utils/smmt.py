"""SMMT publications: reading the raw files and working out which months are covered.

Pure functions only (no S3, no database) so they can be tested on the real files.
routers/external_data.py does the fetching, storing and the Settings page.

Feeds (the S3 folder under external_data/smmt/):
- new_car_registrations: the monthly zip (Cars_MM_YYYY.xls/.xlsx by marque, plus a news-release
  .docx), Tom's old news releases (.docx), and his tracker (__TRACKER New car registrations.xlsx,
  monthly private / fleet / total from 2011).
- used_car_sales: the quarterly zip (YYYY used car data.xlsx: used car sales by month since 2014,
  plus a news-release .docx).
- outlook: the quarterly forecast .docx (a year x fuel type table, in thousands).

Each monthly marque sheet holds the month this year and last year, and the year to date this
year and last year. So a file also gives last year's month, and a month with no file of its own
can often be worked out from two year-to-date figures (see fill_from_ytd).
"""
import calendar
import datetime
import io
import re
import zipfile
from xml.etree import ElementTree

from python_calamine import CalamineWorkbook

FEEDS = ('new_car_registrations', 'used_car_sales', 'outlook')

# How a month's figure was obtained.
HOW_OWN = 'own_file'           # the month's own SMMT file
HOW_LAST_YEAR = 'last_year'    # the last-year column of the same month's file a year later
HOW_TRACKER = 'tracker'        # Tom's tracker spreadsheet
HOW_YTD = 'worked_out'         # year to date minus the other months
# Which figure wins when several give the same month: any SMMT file beats Tom's tracker, and the
# tracker beats a worked-out figure. Between SMMT files the most recent file wins, because SMMT
# restates earlier months in later files (see _put). Higher tier wins.
HOW_TIER = {HOW_OWN: 3, HOW_LAST_YEAR: 3, HOW_TRACKER: 2, HOW_YTD: 1}
# Best first, for showing one colour per month.
HOW_RANK = {HOW_LAST_YEAR: 1, HOW_OWN: 2, HOW_TRACKER: 3, HOW_YTD: 4}

TOTAL = 'Total'
_TOTAL_LABELS = {'total', 'grand total', 'total market'}
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_MONTHS['sept'] = 9

_W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


# ---------------------------------------------------------------- small helpers

def _num(v):
    """A sheet cell as a number, or None for blanks and text."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip().replace(',', '')
    try:
        return float(s)
    except ValueError:
        return None


def _int(v):
    n = _num(v)
    return None if n is None else int(round(n))


def _sheets(data: bytes) -> dict:
    """{sheet name: rows} for an .xls or .xlsx file."""
    wb = CalamineWorkbook.from_filelike(io.BytesIO(data))
    return {name: wb.get_sheet_by_name(name).to_python() for name in wb.sheet_names}


def month_key(year: int, month: int) -> str:
    return f'{year:04d}-{month:02d}'


def _shift(key: str, months: int) -> str:
    y, m = int(key[:4]), int(key[5:7])
    n = y * 12 + (m - 1) + months
    return month_key(n // 12, n % 12 + 1)


def month_from_text(text: str):
    """'September 2023', 'Sep-25', 'April 23', '2023_09' -> '2023-09', else None."""
    t = text.lower()
    m = re.search(r'(20\d\d)[_-](\d\d)(?!\d)', t)
    if m and 1 <= int(m.group(2)) <= 12:
        return month_key(int(m.group(1)), int(m.group(2)))
    for name in sorted(_MONTHS, key=len, reverse=True):
        m = re.search(rf'(?<![a-z]){name}(?![a-z])[\s_-]*(?:fy[\s_-]*)?(20\d\d|\d\d)(?!\d)', t)
        if m:
            y = int(m.group(1))
            return month_key(y + 2000 if y < 100 else y, _MONTHS[name])
    return None


def quarter_from_text(text: str):
    """'Used-Cars-Q2-2026', 'Q4 and FY 2023' -> '2026-Q2', else None."""
    m = re.search(r'q([1-4])\D{0,12}?(20\d\d)', text.lower())
    return f'{m.group(2)}-Q{m.group(1)}' if m else None


# ---------------------------------------------------------------- final-figures note

_NOTE_RE = re.compile(r'(?:subject to change|preliminary)[^.]*\.\s*(Full and final figures published[^.<]*)', re.I)
_DATE_RE = re.compile(r'(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)(?:\s+(20\d\d))?')


def page_note(html: str):
    """The 'Full and final figures published Monday 5 October, 9am' line, or None."""
    m = _NOTE_RE.search(re.sub(r'<[^>]+>', ' ', html or ''))
    return re.sub(r'\s+', ' ', m.group(1)).strip() if m else None


def final_date(note: str, today: datetime.date):
    """The date in a final-figures note. It has no year, so take the one nearest today."""
    m = _DATE_RE.search(note or '')
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    month, day = _MONTHS[m.group(2).lower()], int(m.group(1))
    years = [int(m.group(3))] if m.group(3) else [today.year - 1, today.year, today.year + 1]
    dates = []
    for y in years:
        try:
            dates.append(datetime.date(y, month, day))
        except ValueError:
            pass
    return min(dates, key=lambda d: abs((d - today).days)) if dates else None


def page_links(html: str, base_url: str = '', extensions=('zip', 'docx', 'xlsx', 'xls')) -> list:
    """Every file link on a page, in page order, once each. Links starting '/' (cap hpi
    writes some that way) are made full using base_url."""
    seen, out = set(), []
    ext = '|'.join(extensions)
    origin = re.match(r'https?://[^/]+', base_url or '')
    found = re.findall(rf'(?:https?://[^"\'\s<>]+?|(?<=["\'])/[^"\'\s<>]+?)\.(?:{ext})\b', html or '', re.I)
    for url in found:
        if url.startswith('/'):
            if not origin:
                continue
            url = origin.group(0) + url
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


# ---------------------------------------------------------------- docx

def docx_paragraphs(data: bytes) -> list:
    """The non-empty paragraphs of a .docx, outside tables, in order."""
    root = ElementTree.fromstring(zipfile.ZipFile(io.BytesIO(data)).read('word/document.xml'))
    body = root.find(f'{_W}body')
    out = []
    for p in (body if body is not None else []):
        if p.tag != f'{_W}p':
            continue
        text = ''.join(t.text or '' for t in p.iter(f'{_W}t')).replace('﻿', '').strip()
        if text:
            out.append(text)
    return out


def docx_tables(data: bytes) -> list:
    """Each table of a .docx as a list of rows of cell text."""
    root = ElementTree.fromstring(zipfile.ZipFile(io.BytesIO(data)).read('word/document.xml'))
    tables = []
    for tbl in root.iter(f'{_W}tbl'):
        rows = []
        for tr in tbl.iter(f'{_W}tr'):
            rows.append([' '.join(''.join(t.text or '' for t in p.iter(f'{_W}t')).strip()
                                  for p in tc.iter(f'{_W}p')).strip() for tc in tr.iter(f'{_W}tc')])
        tables.append(rows)
    return tables


def headline(data: bytes, limit: int = 1200) -> str:
    """The opening of a news release: title and the lead lines, for the future home page news."""
    out, size = [], 0
    for p in docx_paragraphs(data):
        if p.lower().startswith(('hi-res', 'ends', 'notes to editors')):
            continue
        out.append(p)
        size += len(p)
        if size >= limit or len(out) >= 6:
            break
    return '\n'.join(out)[:limit]


# ---------------------------------------------------------------- spreadsheets

_SHEET_RE = re.compile(r'(?<![a-z])cars?_(\d\d)_(20\d\d|\d\d)(?!\d)')


def sheet_month(filename: str):
    """'Cars_10_2026.xlsx' or 'Car_03_25.xls' (the marque sheet) -> '2026-10', else None."""
    m = _SHEET_RE.search(filename.lower())
    if not m or not 1 <= int(m.group(1)) <= 12:
        return None
    y = int(m.group(2))
    return month_key(y + 2000 if y < 100 else y, int(m.group(1)))

def parse_marque_sheet(data: bytes, filename: str) -> dict:
    """A Cars_MM_YYYY sheet -> {'month', 'rows': [{marque, month, last_year, ytd, ytd_last_year, share}]}.

    Columns: marque, month this year, share, month last year, share, % change,
    year to date this year, share, year to date last year, share, % change.
    The total row is returned as marque 'Total'."""
    month = sheet_month(filename)
    for rows in _sheets(data).values():
        start = next((i for i, r in enumerate(rows) if r and str(r[0]).strip().upper() == 'MARQUE'), None)
        if start is None:
            continue
        header = rows[start]
        year = _int(header[1]) if len(header) > 1 else None
        if month is None or (year and int(month[:4]) != year):
            continue
        out = []
        for r in rows[start + 1:]:
            r = list(r) + [None] * (11 - len(r))
            name = str(r[0] or '').strip()
            if not name:
                continue
            is_total = name.lower() in _TOTAL_LABELS
            out.append({'marque': TOTAL if is_total else name,
                        'month': _int(r[1]) or 0, 'share': _num(r[2]),
                        'last_year': _int(r[3]) or 0,
                        'ytd': _int(r[6]) or 0, 'ytd_last_year': _int(r[8]) or 0})
            if is_total:
                break
        if out:
            return {'month': month, 'rows': out}
    raise ValueError(f'{filename}: no MARQUE table found')


def parse_tracker(data: bytes) -> list:
    """Tom's tracker: [{month, private, fleet, total}] by month."""
    out = []
    for rows in _sheets(data).values():
        for r in rows:
            if not r or not isinstance(r[0], (datetime.date, datetime.datetime)):
                continue
            r = list(r) + [None] * 4
            out.append({'month': month_key(r[0].year, r[0].month),
                        'private': _int(r[1]), 'fleet': _int(r[2]), 'total': _int(r[3])})
        if out:
            break
    return out


def parse_used_cars(data: bytes) -> list:
    """Used car data sheet (Year, Jan..Dec, Total) -> [{month, units}]."""
    out = []
    for rows in _sheets(data).values():
        for r in rows:
            year = _int(r[0]) if r else None
            if not year or not 2000 <= year <= 2100:
                continue
            for i in range(1, 13):
                units = _int(r[i]) if i < len(r) else None
                if units:
                    out.append({'month': month_key(year, i), 'units': units})
        if out:
            break
    return out


def parse_outlook(data: bytes) -> list:
    """Outlook .docx table -> [{year, forecast, measure, thousands}]."""
    for table in docx_tables(data):
        if len(table) < 2:
            continue
        header = table[0]
        out = []
        for row in table[1:]:
            if not row:
                continue
            m = re.match(r'(20\d\d)\s*(\(f\))?', row[0])
            if not m:
                continue
            for name, cell in zip(header[1:], row[1:]):
                value = _num(cell)
                if value is not None and name:
                    out.append({'year': int(m.group(1)), 'forecast': bool(m.group(2)),
                                'measure': re.sub(r'\s+', ' ', name).strip(), 'thousands': value})
        if out:
            return out
    return []


def outlook_published(filename: str, paragraphs: list):
    """'... OUTLOOK-Q4-26-05-11-2026.docx' -> '2026-11-05' (the date in the name), else None."""
    m = re.search(r'(\d{2})-(\d{2})-(20\d\d)', filename)
    if m:
        try:
            return datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            pass
    for p in paragraphs[:3]:
        k = month_from_text(p)
        if k:
            return k + '-01'
    return None


# ---------------------------------------------------------------- reading one stored file

def classify(feed: str, filename: str, data: bytes) -> dict:
    """Everything that can be read from one raw file.

    Returns {'kind', 'marques': [...], 'tracker': [...], 'used': [...], 'outlook': [...],
    'headline': str, 'month': str|None, 'published': str|None}. Unknown files return
    kind 'other' and nothing else, so storing a file never fails on parsing."""
    out = {'kind': 'other', 'marques': None, 'tracker': [], 'used': [], 'outlook': [],
           'headline': '', 'month': None, 'quarter': None, 'published': None}
    low = filename.lower()
    if low.endswith('.zip'):
        z = zipfile.ZipFile(io.BytesIO(data))
        for name in z.namelist():
            inner, base = z.read(name), name.rsplit('/', 1)[-1].lower()
            if sheet_month(base) and base.endswith(('.xls', '.xlsx')):
                out['marques'] = parse_marque_sheet(inner, base)
                out['month'] = out['marques']['month']
                out['kind'] = 'marque_sheet'
            elif 'used car data' in base and base.endswith(('.xlsx', '.xls')):
                out['used'] = parse_used_cars(inner)
                out['kind'] = 'used_sheet'
            elif base.endswith('.docx') and not out['headline']:
                out['headline'] = headline(inner)
        return out
    if low.endswith(('.xlsx', '.xls')):
        if 'tracker' in low:
            out['tracker'], out['kind'] = parse_tracker(data), 'tracker'
        elif 'used car data' in low:
            out['used'], out['kind'] = parse_used_cars(data), 'used_sheet'
        elif sheet_month(low):
            out['marques'] = parse_marque_sheet(data, low)
            out['month'], out['kind'] = out['marques']['month'], 'marque_sheet'
        return out
    if low.endswith('.docx'):
        paragraphs = docx_paragraphs(data)
        if feed == 'outlook':
            out['outlook'] = parse_outlook(data)
            out['published'] = outlook_published(filename, paragraphs)
            out['kind'] = 'outlook'
            out['headline'] = '\n'.join(p for p in paragraphs[:12] if len(p) > 30)[:1200]
        else:
            out['headline'], out['kind'] = headline(data), 'news_release'
            lead = ' '.join(paragraphs[:3])
            found = re.search(r'data for ([A-Za-z]+(?: and [A-Za-z]+)?\s+(?:FY\s+)?20\d\d)', lead)
            found = re.sub(r'\s+and\s+(?:FY|HY)\b', '', found.group(1), flags=re.I) if found else None
            out['month'] = (month_from_text(found) if found else None) or month_from_text(filename)
            out['quarter'] = quarter_from_text(lead) or quarter_from_text(filename)
    return out


# ---------------------------------------------------------------- combining files

def _put(table: dict, key, value, how: str, file_id, as_of: str = ''):
    """Keep the winning figure for a key: higher HOW_TIER first, then the most recent file
    (as_of = the month the file reports on). A file given later wins a full tie."""
    old = table.get(key)
    if old is None or (HOW_TIER[how], as_of) >= (HOW_TIER[old['how']], old.get('as_of', '')):
        table[key] = {'units': value, 'how': how, 'file_id': file_id, 'as_of': as_of}


def combine_registrations(files: list) -> dict:
    """files: [{'file_id', 'marques': parse_marque_sheet(...)|None, 'tracker': [...]}], oldest first.

    The most recent SMMT file wins for every month it covers, so a figure SMMT restated a
    year later replaces the first one. Year-to-date figures follow the same rule (files are
    given oldest first, so a later file's YTD overwrites an earlier one).

    Returns {'marques': {(month, marque): {units, how, file_id}},
             'totals': {month: {units, how, file_id, private, fleet}}}.
    Marque 'Total' rows feed 'totals' as well."""
    marques, ytd, names = {}, {}, {}
    totals, split = {}, {}
    for f in files:
        sheet = f.get('marques')
        if sheet:
            month = sheet['month']
            last = _shift(month, -12)
            for r in sheet['rows']:
                key = r['marque'].upper()
                _put(marques, (month, key), r['month'], HOW_OWN, f['file_id'], month)
                _put(marques, (last, key), r['last_year'], HOW_LAST_YEAR, f['file_id'], month)
                ytd[(month, key)] = r['ytd']
                ytd[(last, key)] = r['ytd_last_year']
                names[key] = r['marque']
        for t in f.get('tracker') or []:
            if t['total']:
                _put(totals, t['month'], t['total'], HOW_TRACKER, f['file_id'], t['month'])
                split[t['month']] = (t['private'], t['fleet'])
    fill_from_ytd(marques, ytd)
    out_marques = {}
    for (month, key), v in marques.items():
        if key == TOTAL.upper():
            _put(totals, month, v['units'], v['how'], v['file_id'], v.get('as_of', ''))
        else:
            out_marques[(month, names.get(key, key))] = v
    for month, v in totals.items():
        v['private'], v['fleet'] = split.get(month, (None, None))
    return {'marques': out_marques, 'totals': totals}


def fill_from_ytd(marques: dict, ytd: dict):
    """Work out a missing month from year-to-date figures, in place.

    For a missing month k with a known YTD at a later month m of the same year, and a known
    YTD at an earlier month p (or the start of the year), month k = YTD(m) - YTD(p) - every
    other month between p and m. Only done when k is the only unknown month in that span."""
    keys = {key for (_, key) in marques} | {key for (_, key) in ytd}
    for key in keys:
        years = {int(m[:4]) for (m, k) in list(marques) + list(ytd) if k == key}
        for year in years:
            for k in range(1, 13):
                mk = month_key(year, k)
                if (mk, key) in marques:
                    continue
                later = next((m for m in range(k, 13) if (month_key(year, m), key) in ytd), None)
                if later is None:
                    continue
                earlier = next((p for p in range(k - 1, 0, -1) if (month_key(year, p), key) in ytd), 0)
                between = [month_key(year, i) for i in range(earlier + 1, later + 1) if i != k]
                if any((b, key) not in marques for b in between):
                    continue
                value = ytd[(month_key(year, later), key)]
                value -= ytd[(month_key(year, earlier), key)] if earlier else 0
                value -= sum(marques[(b, key)]['units'] for b in between)
                if value >= 0:
                    marques[(mk, key)] = {'units': value, 'how': HOW_YTD, 'file_id': None}


def coverage(months_found: dict, first: str, last: str) -> list:
    """Years of 12 cells for the month grid: [{'year', 'months': [{'month', 'how'|None}]}], newest first."""
    out = []
    for year in range(int(last[:4]), int(first[:4]) - 1, -1):
        cells = []
        for m in range(1, 13):
            key = month_key(year, m)
            if key < first or key > last:
                cells.append({'month': key, 'how': 'n/a'})
            else:
                v = months_found.get(key)
                cells.append({'month': key, 'how': v['how'] if v else None})
        out.append({'year': year, 'months': cells})
    return out


# ---------------------------------------------------------------- where raw files go

# Not split by dev / prod: the raw files are the same whichever site fetched them, and this
# folder is what a later Snowflake load would read. One folder per publisher under it.
EXTERNAL_ROOT = 'applications/pricing/uk-vehicle-list-evaluator/external_data'
S3_ROOT = EXTERNAL_ROOT + '/smmt'


def publisher_for(feed: str) -> str:
    """The S3 folder for a feed: 'cap_hpi' for cap hpi feeds, else 'smmt'."""
    return 'cap_hpi' if feed.startswith('cap_') else 'smmt'

DEFAULT_SKIP_WORDS = ['colour', 'motorparc', 'checklist', 'lcv', 'van-registrations', 'bus', 'coach', 'hgv']


def feed_for(filename: str, default_feed: str, skip_words) -> str | None:
    """Which feed a linked file belongs to, or None to leave it (colours, Motorparc...)."""
    low = filename.lower()
    # Before the skip words: outlook files are named CAR-LCV-MARKET-OUTLOOK.
    if 'outlook' in low:
        return 'outlook'
    if any(w and w.lower() in low for w in skip_words or []):
        return None
    if 'used' in low:
        return 'used_car_sales'
    return default_feed


def period_for(feed: str, info: dict, filename: str, published: str | None) -> str:
    """The S3 period folder: YYYY-MM for registrations, YYYY-Qn for used cars, YYYY-MM-DD for outlooks."""
    if feed == 'outlook':
        return info.get('published') or (published or 'undated')[:10]
    if info.get('kind') == 'tracker':
        return 'history'
    if feed == 'used_car_sales':
        q = info.get('quarter') or quarter_from_text(filename)
        if q:
            return q
        if info.get('used'):
            last = max(u['month'] for u in info['used'])
            return f"{last[:4]}-Q{(int(last[5:7]) - 1) // 3 + 1}"
        return 'undated'
    month = info.get('month') or month_from_text(filename)
    if not month and published:
        # A registrations post comes out in the first days of the month after the data.
        month = _shift(published[:7], -1)
    return month or 'undated'


def s3_key(feed: str, period: str, filename: str) -> str:
    safe = re.sub(r'[^A-Za-z0-9._ ()-]+', '_', filename).strip() or 'file'
    return f'{EXTERNAL_ROOT}/{publisher_for(feed)}/{feed}/{period}/{safe}'


def latest_complete_month(today: datetime.date) -> str:
    """SMMT publishes a month in the first week of the next, so last month is the newest expected."""
    return _shift(month_key(today.year, today.month), -1)
