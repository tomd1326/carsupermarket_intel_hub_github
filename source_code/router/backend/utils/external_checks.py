"""When the automatic External Data check should run, and whether a stored file has changed.

Pure functions only, so they can be tested. routers/external_data.py calls them.

Automatic modes, set per source on Settings > External Data:
- off:          never.
- daily:        once a day, in the first hour of the check hours.
- month_start:  every hour in the first N working days of a month, looking for the
                previous month's file (SMMT publishes a month in the first days of the next).
- month_end:    every hour from N working days before the end of a month, looking for the
                NEXT month's report (cap hpi's November report comes out in late October),
                and on into the first 3 working days of that month in case it is late.
A month_start / month_end source stops for the month once its file is stored (the router
decides "stored", see external_data._target_done).
"""
import datetime

MODES = ('off', 'daily', 'month_start', 'month_end')
LATE_DAYS = 3   # month_end keeps looking this many working days into the target month


def _month_key(d: datetime.date) -> str:
    return f'{d.year:04d}-{d.month:02d}'


def _add_months(key: str, n: int) -> str:
    y, m = int(key[:4]), int(key[5:7])
    i = y * 12 + (m - 1) + n
    return f'{i // 12:04d}-{i % 12 + 1:02d}'


def working_days(year: int, month: int, holidays) -> list:
    """Monday to Friday of a month, less bank holidays (a set of dates)."""
    d = datetime.date(year, month, 1)
    out = []
    while d.month == month:
        if d.weekday() < 5 and d not in holidays:
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


def due(mode: str, days: int, today: datetime.date, holidays, last_run: datetime.date | None = None):
    """(run now?, target month 'YYYY-MM' or None) for one source on `today`.

    For 'daily' the caller passes the date of the last automatic run of this source."""
    holidays = set(holidays or ())
    days = max(1, int(days or 1))
    if mode == 'daily':
        return (last_run != today, None)
    this_month = working_days(today.year, today.month, holidays)
    if mode == 'month_start':
        return (today in this_month[:days], _add_months(_month_key(today), -1))
    if mode == 'month_end':
        if today in this_month[-days:]:
            return (True, _add_months(_month_key(today), 1))
        if today in this_month[:LATE_DAYS]:
            return (True, _month_key(today))
        return (False, None)
    return (False, None)


def in_hours(now: datetime.datetime, start: int, end: int) -> bool:
    """True from start:00 up to (not including) end:00, UK time."""
    return int(start) <= now.hour < int(end)


def refresh_decision(stored: dict, head_status: int | None, headers: dict | None) -> str:
    """Whether a stored file needs downloading again to see if the publisher replaced it.

    stored: {'etag', 'last_modified', 'size'} as saved with the file (any may be None).
    Returns 'same' (nothing to do), 'record' (same file; save the validators the HEAD
    request gave, which were not stored before), or 'download' (fetch it and compare
    its sha256 with the stored copy)."""
    if not head_status or head_status >= 400 or headers is None:
        return 'download'
    h = {k.lower(): v for k, v in headers.items()}
    etag, modified = h.get('etag'), h.get('last-modified')
    try:
        size = int(h['content-length']) if h.get('content-length') else None
    except ValueError:
        size = None
    if not etag and not modified and size is None:
        return 'download'
    if size is not None and stored.get('size') is not None and size != stored['size']:
        return 'download'
    if etag and stored.get('etag') and etag != stored['etag']:
        return 'download'
    if modified and stored.get('last_modified') and modified != stored['last_modified']:
        return 'download'
    if (etag and not stored.get('etag')) or (modified and not stored.get('last_modified')):
        # Nothing stored to compare with: the size has to agree, else download.
        if size is None or stored.get('size') is None:
            return 'download'
        return 'record'
    return 'same'
