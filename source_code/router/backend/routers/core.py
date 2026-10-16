"""Shared state and helpers used by all domain routers.

Holds auth/session state (VALID_USERS, _active_sessions, _sessions_invalidated_at),
the Jinja2 templates instance, environment constants and event tracking.
Importing this module loads users from Aurora (same behaviour as the old
monolithic router.py import).
"""
import datetime
import logging
import os
import time
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from utils.db_utils import (get_database_connection, get_prod_pricer_connection,
                            load_all_users, load_all_page_access, load_page_names,
                            record_event, get_settings, SETTINGS_DEFAULTS)
from .page_registry import ALL_PAGE_KEYS, CONFIGURED_MARKER, DEFAULT_PAGE_KEYS, PAGES_BY_KEY

logger = logging.getLogger(__name__)


def _uk_now() -> datetime.datetime:
    return datetime.datetime.now(tz=ZoneInfo('Europe/London')).replace(tzinfo=None)


BASE_URL = os.getenv("BASE_URL", "/dev")
_ENV = 'local' if os.getenv('LOCAL_ENV') == '1' else ('prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev')
_S3_ENV = 'prod' if os.getenv('AWS_REGION') == 'eu-west-1' else 'dev'


def worker_secret() -> str:
    """The shared secret proving a worker call came from our own Lambda.

    Worker routes (keeper change chunks, SharePoint downloads) cannot use
    require_page_api: the Lambda invokes them itself, with no session. Without
    this check anyone who can reach the API could start runs (which is the
    state /api/reports/run in reports.py is in).

    Only needed when the work is dispatched over HTTP, i.e. on Lambda. Running
    locally the worker is called in-process and never crosses a route, so this
    is not consulted — the secret does not have to exist to run on a laptop.

    Falls back to the Autotrader secret already in the same store if
    WORKER_SHARED_SECRET has not been added yet. That value is not published
    anywhere and is already required for the app to do anything at all, so it
    is a reasonable stand-in; add the dedicated key when convenient.

    utils/lambda_http.py does not log the dispatched event, so the secret in
    the payload never reaches CloudWatch.
    """
    from utils.secrets_manager_client import get_secret
    secrets = get_secret(f'{_S3_ENV}/carsupermarket/car_pricer/api_access')
    token = secrets.get('WORKER_SHARED_SECRET') or secrets.get('AUTOTRADER_SECRET')
    if not token:
        raise RuntimeError(
            'No worker credential available: add WORKER_SHARED_SECRET to '
            f'{_S3_ENV}/carsupermarket/car_pricer/api_access'
        )
    return token


def check_worker_secret(supplied: str) -> None:
    import hmac
    expected = worker_secret()
    if not supplied or not hmac.compare_digest(str(supplied), expected):
        logger.warning('Worker route called with a bad or missing secret')
        raise HTTPException(status_code=403, detail='Forbidden')

# templates/ lives in the backend root, one level above this package
templates_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
templates = Jinja2Templates(directory=templates_dir)

_static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
_static_version_cache: dict = {}


def static_url(path: str) -> str:
    """
    Build a URL for a static asset with a cache-busting version stamp.

    Without this, browsers keep serving a cached buying_requirements.js/.css
    after a deploy — a change looks like it "did nothing" until the user knows
    to hard-reload. The stamp is the file's modification time, so any edit
    produces a new URL and the browser refetches automatically.

    Usage in a template:  {{ static_url('css/buying_requirements.css') }}
    """
    rel = path.lstrip('/')
    version = _static_version_cache.get(rel)
    if version is None:
        try:
            version = str(int(os.path.getmtime(os.path.join(_static_dir, *rel.split('/')))))
        except OSError:
            version = '0'
        # In Lambda the bundle is immutable, so caching the lookup is safe. Locally
        # the reloader restarts the process on each edit, which clears this.
        _static_version_cache[rel] = version
    return f"{BASE_URL}/static/{rel}?v={version}"


templates.env.globals['static_url'] = static_url


# Page names set on Settings > Page Names, cached per process. Like page
# access, each Lambda container keeps its own copy, so a rename shows
# everywhere within PAGE_NAMES_TTL_SECONDS. A failed read keeps the last copy
# (or the default names) and waits the same time before trying again, so one
# page with many names on it does not retry the database for each one.
PAGE_NAMES: dict = {}
_PAGE_NAMES_LOADED_AT: float = 0.0
PAGE_NAMES_TTL_SECONDS = 60


def reload_page_names() -> None:
    global _PAGE_NAMES_LOADED_AT
    _PAGE_NAMES_LOADED_AT = time.time()
    try:
        with get_prod_pricer_connection() as conn:
            names = load_page_names(conn)
        PAGE_NAMES.clear()
        PAGE_NAMES.update(names)
    except Exception as e:
        logger.warning("Could not load page names - using the last known names: %s", e)


def page_name(page_key: str) -> str:
    """The page's name as set in Settings, else its default from page_registry.
    Usage in a template:  {{ page_name('kpi') }}"""
    if time.time() - _PAGE_NAMES_LOADED_AT >= PAGE_NAMES_TTL_SECONDS:
        reload_page_names()
    if page_key in PAGE_NAMES:
        return PAGE_NAMES[page_key]
    page = PAGES_BY_KEY.get(page_key)
    return page.label if page else page_key


templates.env.globals['page_name'] = page_name

# The whole site's name, shown on the home page under "CarSupermarket.com".
# Set on Settings > Page Names and stored in the same page_names table under
# this key, so it is cached and refreshed with the page names.
SITE_NAME_KEY = '__site'
SITE_NAME_DEFAULT = 'Buying Tools'


def site_name() -> str:
    """Usage in a template:  {{ site_name() }}"""
    if time.time() - _PAGE_NAMES_LOADED_AT >= PAGE_NAMES_TTL_SECONDS:
        reload_page_names()
    return PAGE_NAMES.get(SITE_NAME_KEY) or SITE_NAME_DEFAULT


templates.env.globals['site_name'] = site_name


# Live users settings (Settings > Live Users), cached per process the same way
# as page names. Every page reads them to draw the circles, so they must not
# cost a database read per page.
PRESENCE_SETTING_KEYS = ('presence_enabled', 'presence_interval_seconds', 'presence_drop_off_seconds',
                         'presence_circle_size', 'presence_max_circles', 'presence_border_width',
                         'presence_gap', 'presence_border_colour', 'presence_text_colour',
                         'presence_more_colour')
_PRESENCE_CONFIG: dict = {k: SETTINGS_DEFAULTS[k] for k in PRESENCE_SETTING_KEYS}
_PRESENCE_LOADED_AT: float = 0.0
PRESENCE_TTL_SECONDS = 60


def reload_presence_config() -> None:
    global _PRESENCE_LOADED_AT
    _PRESENCE_LOADED_AT = time.time()
    try:
        with get_database_connection() as conn:
            settings = get_settings(conn)
        _PRESENCE_CONFIG.update({k: settings[k] for k in PRESENCE_SETTING_KEYS})
    except Exception as e:
        logger.warning("Could not load live users settings - using the last known values: %s", e)


def presence_config() -> dict:
    """Usage in a template:  {% set pc = presence_config() %}"""
    if time.time() - _PRESENCE_LOADED_AT >= PRESENCE_TTL_SECONDS:
        reload_presence_config()
    return dict(_PRESENCE_CONFIG)


templates.env.globals['presence_config'] = presence_config

# Generic initialization
s3_bucketname = 'aa-raw-sources-uk'
s3_bucket_region = 'eu-west-1'  # bucket is always in eu-west-1 regardless of Lambda region


_ADMIN_FALLBACK = {
    'tdrayson': 'e026581a53dc97e3381566ad0f74e99ae65e0297f9eb41bb76596627885bf8a0',
}
VALID_USERS: dict = {}


ADMIN_USERNAME = 'tdrayson'

# Page access, cached per process. The app runs as several Lambda containers,
# each with its own copy, so this is treated as having a MAXIMUM AGE rather
# than being refreshed at login: past the TTL the next request re-reads.
#
# USER_PAGE_ACCESS holds what was stored, including the CONFIGURED_MARKER —
# get_user_pages is what turns that into a usable set of pages.
USER_PAGE_ACCESS: dict = {}
_PERMISSIONS_LOADED_AT: float = 0.0
PERMISSION_TTL_SECONDS = 60


def _reload_users_from_aurora():
    """Reload all users from Aurora into VALID_USERS. Admin is always valid regardless of DB state."""
    global _PERMISSIONS_LOADED_AT
    try:
        with get_database_connection() as conn:
            users = load_all_users(conn)
            # Same connection — the permissions read costs no extra round trip.
            # Deliberately inside the try but read AFTER users, so a permissions
            # failure cannot stop successfully loaded users reaching VALID_USERS.
            VALID_USERS.clear()
            VALID_USERS.update(users)
            logger.info("Loaded %d users from Aurora", len(users))
            access = load_all_page_access(conn)
        USER_PAGE_ACCESS.clear()          # replace wholesale, so removed grants disappear
        USER_PAGE_ACCESS.update(access)
        _PERMISSIONS_LOADED_AT = time.time()
    except Exception as e:
        logger.warning("Could not load users/page access from Aurora — falling back to hardcoded admin only: %s", e)
    VALID_USERS.update(_ADMIN_FALLBACK)


def is_admin(user: str | None) -> bool:
    return user == ADMIN_USERNAME


def _permissions_are_fresh() -> bool:
    return bool(_PERMISSIONS_LOADED_AT) and (time.time() - _PERMISSIONS_LOADED_AT) < PERMISSION_TTL_SECONDS


def get_user_pages(user: str | None) -> frozenset:
    """Which pages this user may see.

    The admin is answered before any cache or database read, so he keeps access
    during an outage and can never be locked out.

    For everyone else a stale cache is refreshed first. If that refresh fails,
    access is DENIED rather than falling back to what was cached — otherwise a
    database outage would silently extend access indefinitely. The timestamp is
    left alone on failure so the next request retries.
    """
    if is_admin(user):
        return ALL_PAGE_KEYS
    if not user:
        return frozenset()

    if not _permissions_are_fresh():
        _reload_users_from_aurora()
        if not _permissions_are_fresh():
            logger.warning("Page access could not be refreshed — denying %s", user)
            return frozenset()

    # A deleted account must be rejected BEFORE the unconfigured-user defaults
    # below, or it would be handed the default pages on its way out.
    if user not in VALID_USERS:
        return frozenset()

    stored = USER_PAGE_ACCESS.get(user)
    if stored is None or CONFIGURED_MARKER not in stored:
        # Never configured — today's access, so nothing changes for anyone
        # until an admin saves their row.
        return DEFAULT_PAGE_KEYS
    # Configured: exactly what was saved, an empty set included. The marker is
    # bookkeeping, never a page.
    return frozenset(stored & ALL_PAGE_KEYS)


def user_can(user: str | None, page_key: str) -> bool:
    return page_key in get_user_pages(user)


class PageRedirect(Exception):
    """Send the browser somewhere else instead of rendering a page.

    NOT an HTTPException: exceptions.http_exception_handler catches every
    HTTPException and renders error.html unconditionally — it never looks at a
    Location header — so raising HTTPException(303) would show "Error 303"
    rather than redirecting. main.py registers a handler for this class ahead
    of the HTTPException one.
    """

    def __init__(self, location: str):
        self.location = location
        super().__init__(location)


def require_page(page_key: str):
    """Dependency for HTML page routes. Returns the username, or redirects —
    to /login when signed out, to /home when signed in without access.

    A dependency cannot `return` a RedirectResponse the way the old inline
    checks did, so denial is raised and handled in main.py.
    """
    def _dependency(request: Request) -> str:
        user = get_current_user(request)
        if not user:
            raise PageRedirect(BASE_URL + "/login")
        if not user_can(user, page_key):
            logger.info("Access denied: %s -> %s", user, page_key)
            raise PageRedirect(BASE_URL + "/home")
        return user
    return _dependency


def require_page_api(page_key: str):
    """Dependency for data/download endpoints. 401 when signed out, 403 when
    signed in without access — matching what the endpoints already returned.

    Every endpoint gets its page's permission, including the four default
    tools: a URL can be requested directly, so being reachable only from a
    page the user has passed is not a control.
    """
    def _dependency(request: Request) -> str:
        user = get_current_user(request)
        if not user:
            raise HTTPException(status_code=401)
        if not user_can(user, page_key):
            logger.info("Access denied (api): %s -> %s", user, page_key)
            raise HTTPException(status_code=403)
        return user
    return _dependency


_reload_users_from_aurora()

# session_id -> {user, login_time} — tracks active sessions server-side
_active_sessions: dict = {}
_sessions_invalidated_at: float = 0.0  # epoch — sessions logged in before this are force-logged out


def _track(request: Request, event_type: str, page: str, metadata: dict | None = None) -> None:
    session_id = request.session.get('session_id')
    username = request.session.get('user')
    if not session_id or not username:
        return
    try:
        with get_database_connection() as conn:
            record_event(conn, session_id, username, _ENV, event_type, page, metadata)
    except Exception as e:
        logger.warning("Failed to record event %s for %s: %s", event_type, username, e)


def get_current_user(request: Request):
    user = request.session.get('user')
    if not user:
        return None
    # Invalidate session if password has changed since login
    if request.session.get('pwd_hash') != VALID_USERS.get(user):
        _active_sessions.pop(request.session.get('session_id'), None)
        request.session.clear()
        return None
    # Invalidate session if admin triggered a force re-login after this session logged in
    if request.session.get('login_time_epoch', 0) < _sessions_invalidated_at:
        _active_sessions.pop(request.session.get('session_id'), None)
        request.session.clear()
        return None
    return user
