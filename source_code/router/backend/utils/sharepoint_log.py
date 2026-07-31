"""In-memory capture of SharePoint-file log entries for the View Log button.

Lambda writes logs to stdout (CloudWatch) and keeps nothing, so there is
nothing to serve back to the page. This adds a bounded ring buffer that keeps
the most recent SharePoint-related records in the process, alongside the normal
stdout output — it does not replace or suppress it.

Only records mentioning the Daily Buyers Sheet or Cars Bought files are kept,
matched on the prefixes those code paths already log with. The buffer is
capped, so memory is bounded regardless of uptime, and oldest entries are
dropped first. Entries do not survive a cold start.
"""

import logging
import threading
from collections import deque
from datetime import datetime, timezone

# Bounded so a long-lived container cannot grow without limit.
_MAX_ENTRIES = 500

_entries: deque = deque(maxlen=_MAX_ENTRIES)
_lock = threading.Lock()

# Substrings identifying a log line as relating to a SharePoint file. These
# match the prefixes already used by the Daily Buyers Sheet and Cars Bought
# code paths in routers/buying_requirements.py.
_MATCH = (
    'daily buyers sheet',
    'cars bought',
    'dbs',
    'cb fetch',
    'sharepoint',
)


def _is_sharepoint_record(message: str) -> bool:
    low = message.lower()
    return any(m in low for m in _MATCH)


class SharePointLogHandler(logging.Handler):
    """Appends matching records to the ring buffer. Never raises."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if not _is_sharepoint_record(message):
                return
            entry = {
                'ts': datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                'level': record.levelname,
                'logger': record.name,
                'message': message,
            }
            with _lock:
                _entries.append(entry)
        except Exception:
            # A logging handler must never break the code that logged.
            pass


def get_entries() -> list:
    """Most recent first."""
    with _lock:
        return list(reversed(_entries))


def clear_entries() -> None:
    with _lock:
        _entries.clear()


def install() -> None:
    """Attach the handler to the root logger, once."""
    root = logging.getLogger()
    if any(isinstance(h, SharePointLogHandler) for h in root.handlers):
        return
    handler = SharePointLogHandler()
    handler.setLevel(logging.INFO)
    root.addHandler(handler)
