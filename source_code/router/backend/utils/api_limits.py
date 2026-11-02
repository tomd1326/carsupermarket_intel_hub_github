"""The global CAP and Autotrader API settings (Settings: CAP / AT workers and
rate limits), applied the same way on every page that calls those APIs.

The rate limit lives on the CAP and Autotrader clients themselves (each is
one shared instance per server, with a RateLimiter every request passes
through), so setting it here limits every page's calls together. Workers is
how many calls a page runs at once.

    limits = api_limits.apply()      # before calling CAP / Autotrader
    ThreadPoolExecutor(max_workers=api_limits.workers('cap', len(items)))
"""

import contextlib
import logging
import threading
import time

from utils.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

_DEFAULTS = {"cap_workers": 25, "cap_rate_limit": 100, "at_workers": 40, "at_rate_limit": 60}
_CACHE_SECONDS = 60          # settings are read at most once a minute per server
_cache = {"at": 0.0, "limits": dict(_DEFAULTS)}
_lock = threading.Lock()


def read() -> dict:
    """{cap_workers, cap_rate_limit, at_workers, at_rate_limit} from Settings."""
    with _lock:
        if time.monotonic() - _cache["at"] < _CACHE_SECONDS:
            return dict(_cache["limits"])
    limits = dict(_DEFAULTS)
    try:
        from utils.db_utils import get_database_connection, get_settings
        with get_database_connection() as conn:
            s = get_settings(conn)
        for k in limits:
            try:
                limits[k] = max(1, int(s.get(k, limits[k])))
            except (TypeError, ValueError):
                pass
    except Exception as e:
        logger.warning("API limits: settings not read, using defaults: %s", e)
    with _lock:
        _cache.update(at=time.monotonic(), limits=limits)
    return dict(limits)


def _set_limiter(client, rate: int) -> None:
    # Replaced only when the setting changed, so calls already waiting in the
    # bucket are not let through early.
    if getattr(client.limiter, "rate_setting", None) != rate:
        client.limiter = RateLimiter(rate_limit=rate, time_window=1)
        client.limiter.rate_setting = rate


def apply() -> dict:
    """Put the Settings rate limits on the shared CAP and Autotrader clients
    and return the limits."""
    limits = read()
    try:
        from clients.cap_client import CAPClient
        _set_limiter(CAPClient(), limits["cap_rate_limit"])
    except Exception as e:
        logger.warning("API limits: CAP limit not set: %s", e)
    try:
        from clients.autotrader_client import AutoTraderClient
        _set_limiter(AutoTraderClient(), limits["at_rate_limit"])
    except Exception as e:
        logger.warning("API limits: Autotrader limit not set: %s", e)
    return limits


@contextlib.contextmanager
def quick():
    """Around CAP calls made for a page someone is waiting on: shorter
    timeout and fewer retries on this thread (see CAPClient._post_with_retry).
    Use it inside each worker thread, as it applies to the current thread only."""
    from clients.cap_client import CAPClient
    local = CAPClient()._local
    before = getattr(local, "quick", False)
    local.quick = True
    try:
        yield
    finally:
        local.quick = before


def workers(api: str, jobs: int) -> int:
    """Calls to run at once for this many jobs: the Settings workers for the
    API ('cap' or 'at'), never more than there are jobs."""
    return max(1, min(jobs, read()[f"{api}_workers"]))
