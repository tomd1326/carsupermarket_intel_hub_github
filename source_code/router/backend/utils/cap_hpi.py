"""cap hpi monthly editorials: reading the report PDFs.

Pure functions only (no S3, no database), like utils/smmt.py. routers/external_data.py
does the fetching, storing and the Settings page.

cap hpi publishes four editorials a month on https://www.cap-hpi.com/tag/cars/, each with
one PDF. The report named for month M comes out about 3 working days before the end of
month M-1 (Car Market Overview November 2026 was "correct at the time of writing, 26th
October 2026").

Feeds:
- cap_market_overview: "Car Market Overview <Month YYYY>". Read here: the sector and
  sub-sector average value movements, and Notable Movers at 1-yr 20k and 3-yr 60k.
- cap_future_overview: "Future Car Market Overview Used Car / SMR / New Cars <Month>".
  Stored with their text only (Tom, 2026-11-06).

The "Monthly percentage movements in Live valuations (3-years, 60k miles)" page is a chart
whose labels cannot be matched to months, so the monthly series is built instead from each
report's "Overall Avg Book Movement" at 3 YR/60K, which is the chart's newest point.
"""
import io
import re

from pypdf import PdfReader

from utils import smmt

FEEDS = ('cap_market_overview', 'cap_future_overview')
AGE_BANDS = ('1yr_10k', '3yr_60k', '5yr_80k', '10yr_100k')
MOVER_BANDS = ('1yr_20k', '3yr_60k')
OVERALL = 'Overall Avg Book Movement'

# Future report types, from the editorial slug or title.
FUTURE_TYPES = (('used-car', 'used_car'), ('smr', 'smr'), ('new-car', 'new_cars'))

_PCT = re.compile(r'^\(?-?\d+(?:\.\d+)?%\)?$')
_GBP = re.compile(r'^\(?-?[\d,]+\)?$')


def series(slug_or_title: str):
    """('cap_market_overview', None) or ('cap_future_overview', 'used_car' | 'smr' | 'new_cars'),
    or None for any other editorial."""
    low = re.sub(r'[\s_]+', '-', slug_or_title.lower())
    if 'market-overview' not in low:
        return None
    if 'future' in low:
        kind = next((k for word, k in FUTURE_TYPES if word in low), None)
        return ('cap_future_overview', kind)
    return ('cap_market_overview', None)


def report_month(title: str):
    """'Car Market Overview November 2026' / 'October 26' / a PDF name -> 'YYYY-MM'."""
    return smmt.month_from_text(title or '')


def _value(token: str):
    """'(1.3%)' -> -1.3, '0.1%' -> 0.1, '(1,200)' -> -1200, '(250' -> -250."""
    t = token.strip()
    neg = t.startswith('(') or t.startswith('-')
    t = t.strip('()-%').replace(',', '')
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def pdf_pages(data: bytes) -> list:
    return [(p.extract_text() or '') for p in PdfReader(io.BytesIO(data)).pages]


def _lines(text: str) -> list:
    return [re.sub(r'\s+', ' ', l).strip() for l in text.splitlines() if l.strip()]


def _trailing(line: str, pattern, most: int):
    """Split a row into (label, [values]) taking up to `most` value tokens off the end."""
    tokens = line.replace('(', ' (').split()
    # Re-join tokens split above: '(1.2%)' stays whole, '(250' stays whole.
    values = []
    while tokens and len(values) < most and pattern.match(tokens[-1]):
        values.insert(0, tokens.pop())
    return ' '.join(tokens).strip(), values


def parse_value_movements(pages: list) -> list:
    """[{table: 'sector'|'sub_sector', segment, age_band, pct, partial}] from the
    "average value movements" page. A row with fewer than four values (MPV Small) has
    them laid in from the right, as the PDF shows them, and is marked partial."""
    page = next((p for p in pages if 'average value movements' in p), None)
    if page is None:
        return []
    out, table = [], None
    for line in _lines(page):
        if line.upper().startswith('1 YR/10K'):
            table = 'sector' if table is None else 'sub_sector'
            continue
        if table is None:
            continue
        label, values = _trailing(line, _PCT, 4)
        if not label or not values or label.startswith('('):
            continue
        bands = AGE_BANDS[len(AGE_BANDS) - len(values):]
        for band, token in zip(bands, values):
            out.append({'table': table, 'segment': label, 'age_band': band, 'pct': _value(token),
                        'partial': len(values) < 4})
    return out


def parse_notable_movers(pages: list) -> list:
    """[{band: '1yr_20k'|'3yr_60k', model, min_gbp, max_gbp, avg_gbp}]. The first table under
    a MIN £ MAX £ AVG £ heading is 1-yr 20k, the second 3-yr 60k (the order of the headings)."""
    page = next((p for p in pages if 'Notable Movers' in p), None)
    if page is None:
        return []
    out, index = [], -1
    for line in _lines(page):
        if line.upper().startswith('MIN £'):
            index += 1
            continue
        if not 0 <= index < len(MOVER_BANDS):
            continue
        model, values = _trailing(line, _GBP, 3)
        if len(values) != 3 or not model:
            continue
        mn, mx, avg = (_value(v) for v in values)
        out.append({'band': MOVER_BANDS[index], 'model': model, 'min_gbp': mn, 'max_gbp': mx, 'avg_gbp': avg})
    return out


def overall_3yr_60k(movements: list):
    return next((m['pct'] for m in movements
                 if m['table'] == 'sector' and m['segment'] == OVERALL and m['age_band'] == '3yr_60k'), None)


def summary(pages: list, limit: int = 1200) -> str:
    """The opening text of a report, for the future home page news."""
    lines = [l for p in pages[:3] for l in _lines(p) if len(l) > 40 and 'By cap hpi' not in l]
    return '\n'.join(lines)[:limit]


def classify(feed: str, filename: str, data: bytes, title: str = '') -> dict:
    """Everything read from one report PDF, in the same shape as smmt.classify plus
    'movements', 'movers', 'overall_3yr_60k', 'report_type'."""
    out = {'kind': 'other', 'month': report_month(title) or report_month(filename),
           'movements': [], 'movers': [], 'overall_3yr_60k': None, 'headline': '', 'report_type': None}
    s = series(title or filename)
    out['report_type'] = s[1] if s else None
    if not filename.lower().endswith('.pdf'):
        return out
    pages = pdf_pages(data)
    out['headline'] = summary(pages)
    if feed == 'cap_market_overview':
        out['movements'] = parse_value_movements(pages)
        out['movers'] = parse_notable_movers(pages)
        out['overall_3yr_60k'] = overall_3yr_60k(out['movements'])
        out['kind'] = 'cap_overview' if out['movements'] else 'other'
    else:
        out['kind'] = 'cap_future'
    return out
