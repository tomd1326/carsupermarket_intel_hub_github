"""Automatic External Data check timing and the replaced-file rule (utils/external_checks.py),
and reading the cap hpi Car Market Overview text (utils/cap_hpi.py).

Run from source_code/router/backend:
    poetry run pytest tests/test_external_checks.py
"""

import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from utils import cap_hpi, external_checks as ec

D = datetime.date
NO_HOLIDAYS = set()


# ---------------------------------------------------------------- timing

def test_month_end_looks_for_next_months_report():
    # October 2026 ends on Friday 30th; the last 3 working days are 28, 29, 30 October.
    assert ec.due('month_end', 3, D(2026, 10, 27), NO_HOLIDAYS) == (False, None)
    assert ec.due('month_end', 3, D(2026, 10, 28), NO_HOLIDAYS) == (True, '2026-11')
    assert ec.due('month_end', 3, D(2026, 10, 30), NO_HOLIDAYS) == (True, '2026-11')


def test_month_end_keeps_looking_early_next_month():
    # November's report not found by the end of October: still looked for on 2-4 November.
    assert ec.due('month_end', 3, D(2026, 11, 2), NO_HOLIDAYS) == (True, '2026-11')
    assert ec.due('month_end', 3, D(2026, 11, 4), NO_HOLIDAYS) == (True, '2026-11')
    assert ec.due('month_end', 3, D(2026, 11, 5), NO_HOLIDAYS) == (False, None)


def test_month_end_window_skips_weekends_and_bank_holidays():
    # December 2026: Fri 25 and Mon 28 are bank holidays, so the last 3 working days are 29, 30 and 31.
    holidays = {D(2026, 12, 25), D(2026, 12, 28)}
    days = ec.working_days(2026, 12, holidays)
    assert days[-3:] == [D(2026, 12, 29), D(2026, 12, 30), D(2026, 12, 31)]
    assert ec.due('month_end', 3, D(2026, 12, 24), holidays)[0] is False
    assert ec.due('month_end', 3, D(2026, 12, 29), holidays) == (True, '2027-01')


def test_month_start_looks_for_last_month():
    assert ec.due('month_start', 5, D(2026, 11, 2), NO_HOLIDAYS) == (True, '2026-10')
    assert ec.due('month_start', 5, D(2026, 11, 9), NO_HOLIDAYS) == (False, '2026-10')


def test_daily_runs_once_a_day_and_off_never():
    assert ec.due('daily', 1, D(2026, 11, 6), NO_HOLIDAYS, None) == (True, None)
    assert ec.due('daily', 1, D(2026, 11, 6), NO_HOLIDAYS, D(2026, 11, 6)) == (False, None)
    assert ec.due('off', 3, D(2026, 10, 30), NO_HOLIDAYS) == (False, None)


def test_check_hours():
    assert ec.in_hours(datetime.datetime(2026, 11, 6, 8, 0), 8, 18)
    assert not ec.in_hours(datetime.datetime(2026, 11, 6, 18, 0), 8, 18)


# ---------------------------------------------------------------- replaced files

STORED = {'etag': '"abc"', 'last_modified': 'Thu, 05 Nov 2026 08:53:39 GMT', 'size': 100}


def test_same_validators_need_nothing():
    h = {'ETag': '"abc"', 'Last-Modified': 'Thu, 05 Nov 2026 08:53:39 GMT', 'Content-Length': '100'}
    assert ec.refresh_decision(STORED, 200, h) == 'same'


def test_any_changed_validator_downloads():
    assert ec.refresh_decision(STORED, 200, {'ETag': '"new"', 'Content-Length': '100'}) == 'download'
    assert ec.refresh_decision(STORED, 200, {'ETag': '"abc"', 'Last-Modified': 'Fri, 06 Nov 2026 09:00:00 GMT'}) == 'download'
    assert ec.refresh_decision(STORED, 200, {'ETag': '"abc"', 'Content-Length': '101'}) == 'download'


def test_no_head_or_no_validators_downloads():
    assert ec.refresh_decision(STORED, None, None) == 'download'
    assert ec.refresh_decision(STORED, 405, {}) == 'download'
    assert ec.refresh_decision(STORED, 200, {'Content-Type': 'application/zip'}) == 'download'


def test_first_validators_recorded_when_size_agrees():
    loaded_from_s3 = {'etag': None, 'last_modified': None, 'size': 100}
    assert ec.refresh_decision(loaded_from_s3, 200, {'ETag': '"abc"', 'Content-Length': '100'}) == 'record'
    assert ec.refresh_decision(loaded_from_s3, 200, {'ETag': '"abc"'}) == 'download'
    assert ec.refresh_decision(loaded_from_s3, 200, {'ETag': '"abc"', 'Content-Length': '99'}) == 'download'


# ---------------------------------------------------------------- cap hpi report text

# As pypdf reads the November 2026 Car Market Overview (pages 13 and 14), shortened.
VALUES_PAGE = """Car Market Overview
By cap hpi
Current used valuations November 2026 - average value movements
( ) Denotes negative percentages
 1 YR/10K 3 YR/60K 5 YR/80K 10 YR/100k
City Car (0.6%) (1.2%) (1.7%) (0.6%)
Luxury Executive (0.9%) (0.5%) (0.2%) 0.0%
Supercar 0.1%   0.1%   0.2%   0.5%
Overall Avg Book Movement (1.3%) (1.7%) (2.4%) (3.5%)
 1 YR/10K 3 YR/60K 5 YR/80K 10 YR/100k
MPV Small   (14.7%) (6.2%)
MPV Large (0.2%) 0.2%   (0.8%) (3.2%)
"""
MOVERS_PAGE = """Car Market Overview
Notable Movers 1-yr 20k
Notable Movers 3-yr 60k
 MIN £ MAX £ AVG £
KIA STONIC (17- ) (800) (600) (716)
MG MOTOR UK MG 4 (22- ) Electric 250 350 292
VOLVO XC90 (14- ) HYBRID (3,900) (1,700) (2,437)
 MIN £ MAX £ AVG £
ABARTH 500/595/695 (09-26) (350) (150) (250)
SEAT ATECA (16-24) DIESEL (350) (250 (286)
"""


def test_value_movements_read_with_short_rows_from_the_right():
    rows = cap_hpi.parse_value_movements(['cover', VALUES_PAGE])
    city = {r['age_band']: r['pct'] for r in rows if r['segment'] == 'City Car'}
    assert city == {'1yr_10k': -0.6, '3yr_60k': -1.2, '5yr_80k': -1.7, '10yr_100k': -0.6}
    assert cap_hpi.overall_3yr_60k(rows) == -1.7
    mpv_small = [r for r in rows if r['segment'] == 'MPV Small']
    assert [(r['age_band'], r['pct'], r['partial'], r['table']) for r in mpv_small] == [
        ('5yr_80k', -14.7, True, 'sub_sector'), ('10yr_100k', -6.2, True, 'sub_sector')]
    assert {r['age_band']: r['pct'] for r in rows if r['segment'] == 'MPV Large'}['3yr_60k'] == 0.2


def test_notable_movers_two_bands_and_a_missing_bracket():
    rows = cap_hpi.parse_notable_movers([MOVERS_PAGE])
    assert [r['band'] for r in rows] == ['1yr_20k'] * 3 + ['3yr_60k'] * 2
    volvo = rows[2]
    assert (volvo['model'], volvo['min_gbp'], volvo['max_gbp'], volvo['avg_gbp']) == \
        ('VOLVO XC90 (14- ) HYBRID', -3900, -1700, -2437)
    assert rows[1]['model'] == 'MG MOTOR UK MG 4 (22- ) Electric' and rows[1]['avg_gbp'] == 292
    seat = rows[4]
    assert (seat['model'], seat['max_gbp'], seat['avg_gbp']) == ('SEAT ATECA (16-24) DIESEL', -250, -286)


def test_report_series_and_month():
    assert cap_hpi.series('car-market-overview-november-2026') == ('cap_market_overview', None)
    assert cap_hpi.series('Future Car Market Overview Used Car November 2026') == ('cap_future_overview', 'used_car')
    assert cap_hpi.series('future-car-market-overview-new-cars-october-26') == ('cap_future_overview', 'new_cars')
    assert cap_hpi.series('future-car-market-overview-smr-november-2026') == ('cap_future_overview', 'smr')
    assert cap_hpi.series('some-other-editorial') is None
    assert cap_hpi.report_month('Car Market Overview October 26') == '2026-10'
    assert cap_hpi.report_month('Car-Market-Overview-November26.pdf') == '2026-11'
