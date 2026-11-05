"""Shared state and helpers used by all domain routers.

Holds auth/session state (VALID_USERS, session_config, session tracking),
the Jinja2 templates instance, environment constants and event tracking.
Importing this module loads users from Aurora (same behaviour as the old
monolithic router.py import).
"""
import datetime
import hashlib
import logging
import os
import time
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates

from utils.db_utils import (get_database_connection, get_prod_pricer_connection,
                            load_all_users, load_all_page_access, load_page_names, read_user_permissions,
                            record_event, get_settings, read_user_details,
                            touch_session, close_sessions, read_session_settings,
                            new_session_id, SETTINGS_DEFAULTS)
from utils import app_log
from .page_registry import ALL_PAGE_KEYS, CONFIGURED_MARKER, DEFAULT_PAGE_KEYS, PAGES, PAGES_BY_KEY

logger = logging.getLogger(__name__)


def _uk_now() -> datetime.datetime:
    return datetime.datetime.now(tz=ZoneInfo('Europe/London')).replace(tzinfo=None)


# No base path: dev, prod and local all serve from the root of their domain
# (dev-uk-intel-hub / uk-intel-hub.aramis-group.eu, http://127.0.0.1:8080/).
# Old /dev/... page links are redirected in main.py.
BASE_URL = os.getenv("BASE_URL", "")
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
    to hard-reload. The stamp is a hash of the file's contents, so any edit
    produces a new URL and the browser refetches automatically.

    A content hash rather than the modification time: main.py tells browsers
    to keep a versioned file for a year without asking again, which is only
    safe if the stamp is guaranteed to change whenever the file does. A
    modification time depends on how the build copied the file; the contents
    do not.

    Usage in a template:  {{ static_url('css/buying_requirements.css') }}
    """
    rel = path.lstrip('/')
    version = _static_version_cache.get(rel)
    if version is None:
        try:
            with open(os.path.join(_static_dir, *rel.split('/')), 'rb') as f:
                version = hashlib.md5(f.read(), usedforsecurity=False).hexdigest()[:12]
        except OSError:
            version = '0'
        # In Lambda the bundle is immutable, so caching the lookup is safe.
        # Locally it is not cached: the reloader only restarts the server for
        # Python changes, so an edited .js or .css would keep its old stamp
        # and the browser would keep using its old copy.
        if os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
            _static_version_cache[rel] = version
    return f"{BASE_URL}/static/{rel}?v={version}"


templates.env.globals['static_url'] = static_url


# Every page gets static/js/client_errors.js at the top of <head>, so a browser
# error (even one that stops the page's own script loading) is written to the
# server log. Added here rather than in each template so new pages get it too.
_render_template = templates.TemplateResponse


def _template_response_with_error_log(*args, **kwargs):
    response = _render_template(*args, **kwargs)
    body = getattr(response, "body", b"")
    if b"<head>" in body:
        tag = (f'<head>\n    <script src="{static_url("js/client_errors.js")}" '
               f'data-base="{BASE_URL}"></script>').encode()
        response.body = body.replace(b"<head>", tag, 1)
        response.headers["content-length"] = str(len(response.body))
    return response


templates.TemplateResponse = _template_response_with_error_log


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


# Each page's menu category (Settings > Page Names), stored in the same
# page_names table as cat__<page_key>, so it is cached and refreshed with the
# page names. Blank = no category. Only how the menu is laid out; it has no
# part in who may open a page.
CATEGORY_PREFIX = 'cat__'


def page_category(page_key: str) -> str:
    if time.time() - _PAGE_NAMES_LOADED_AT >= PAGE_NAMES_TTL_SECONDS:
        reload_page_names()
    return PAGE_NAMES.get(CATEGORY_PREFIX + page_key, '')


# Menu order (Settings > Page Names), in the same page_names table:
# ord__<page_key> holds a page's position number, and catpos__<n> holds the
# name of the category in position n (keyed by position, as a category name
# can be longer than the 64-character page_key column).
ORDER_PREFIX = 'ord__'
CATEGORY_POS_PREFIX = 'catpos__'


# Pages hidden on Settings > Page Names, stored in the same table as
# hide__<page_key> = '1'. A hidden page is left off the home tiles, the menu
# and the Page Access list on User Accounts, and nobody but the admin can open it (get_user_pages).
# Saved grants are kept, so unhiding it gives everyone their access back.
HIDDEN_PREFIX = 'hide__'


def page_hidden(page_key: str) -> bool:
    if time.time() - _PAGE_NAMES_LOADED_AT >= PAGE_NAMES_TTL_SECONDS:
        reload_page_names()
    return PAGE_NAMES.get(HIDDEN_PREFIX + page_key) == '1'


def visible_pages() -> list:
    """PAGES without the hidden ones, in PAGES order."""
    return [p for p in PAGES if not page_hidden(p.key)]


def page_order(page_key: str) -> int | None:
    value = PAGE_NAMES.get(ORDER_PREFIX + page_key, '')
    return int(value) if value.isdigit() else None


def category_order() -> list[str]:
    """Category names in their saved order."""
    positions = []
    for key, name in PAGE_NAMES.items():
        if key.startswith(CATEGORY_POS_PREFIX) and key[len(CATEGORY_POS_PREFIX):].isdigit():
            positions.append((int(key[len(CATEGORY_POS_PREFIX):]), name))
    return [name for _, name in sorted(positions)]


def user_menu(user: str | None) -> dict:
    """The burger menu's pages for this user:
    {'categories': [(name, [...]), ...], 'pages': [...no category...]}.
    Pages follow their saved order number, then PAGES order; categories
    follow the saved category order, then the order they are first met.
    Each page is {'key', 'name', 'url'}. Usage in a template:
    {% set menu = user_menu(user_name) %}"""
    allowed = get_user_pages(user)
    if time.time() - _PAGE_NAMES_LOADED_AT >= PAGE_NAMES_TTL_SECONDS:
        reload_page_names()
    ranked = sorted(
        (p for p in visible_pages() if p.key in allowed),
        key=lambda p: (page_order(p.key) is None, page_order(p.key) or 0),
    )
    loose, grouped = [], {}
    for page in ranked:
        item = {'key': page.key, 'name': page_name(page.key), 'url': BASE_URL + page.path}
        category = page_category(page.key)
        if category:
            grouped.setdefault(category, []).append(item)
        else:
            loose.append(item)
    saved = category_order()
    names = sorted(grouped, key=lambda c: saved.index(c) if c in saved else len(saved))
    return {'categories': [(c, grouped[c]) for c in names], 'pages': loose}


templates.env.globals['page_category'] = page_category
templates.env.globals['user_menu'] = user_menu


# Live users settings (Settings > Live Users), cached per process the same way
# as page names. Every page reads them to draw the circles, so they must not
# cost a database read per page. The page header's and menus' sizes ride
# along, as every page draws them too.
PRESENCE_SETTING_KEYS = ('presence_enabled', 'presence_users_see_each_other',
                         'presence_users_see_home_list', 'presence_interval_seconds', 'presence_drop_off_seconds',
                         'presence_circle_size', 'presence_max_circles', 'presence_border_width',
                         'presence_gap', 'presence_border_colour', 'presence_text_colour',
                         'presence_more_colour', 'presence_ring_width', 'presence_ring_gap',
                         'account_menu_font_size', 'side_menu_font_size', 'side_menu_width',
                         'page_title_size', 'page_menu_btn_size', 'page_head_pad_y',
                         'page_head_pad_x', 'page_head_gap',
                         'ap_page_bg', 'ap_surface', 'ap_header_bg', 'ap_menu_bg', 'ap_text',
                         'ap_heading', 'ap_muted', 'ap_border', 'ap_menu_text', 'ap_primary',
                         'ap_primary_hover', 'ap_table_header_bg', 'ap_table_header_text',
                         'ap_surface_opacity', 'ap_header_opacity', 'ap_menu_opacity',
                         'ap_radius', 'ap_header_shadow',
                         'home_page_bg', 'home_title_colour', 'home_btn_bg', 'home_btn_hover_bg',
                         'home_btn_text', 'home_title_size', 'home_title_gap', 'home_logo_size',
                         'home_logo_radius', 'home_logo_gap', 'home_btn_width', 'home_btn_height',
                         'home_btn_font_size', 'home_btn_font_weight', 'home_btn_radius',
                         'home_btn_3d', 'home_btn_3d_edge',
                         'home_grid_gap', 'home_grid_max_width', 'home_pad_top', 'home_pad_side',
                         'home_pad_bottom')
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


def _rgba(hex_colour: str, opacity_pct) -> str:
    """'#rrggbb' and a percentage as an rgba() colour. A stored value that is
    not a valid colour falls back to white, so a bad row cannot break a page."""
    h = str(hex_colour or '').strip().lstrip('#')
    if len(h) != 6 or any(c not in '0123456789abcdefABCDEF' for c in h):
        h = 'ffffff'
    try:
        alpha = max(0, min(100, int(opacity_pct))) / 100
    except (TypeError, ValueError):
        alpha = 1
    return f"rgba({int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)}, {alpha:g})"


def appearance_css() -> str:
    """The Settings > Appearance values as one :root rule, drawn by page_header
    on every page. It sets the shared tokens in theme.css, so a change here
    reaches every page. Colours are '#rrggbb' only (validated on save)."""
    c = presence_config()
    radius = max(0, min(30, int(c['ap_radius'])))
    shadow = max(0, min(100, int(c['ap_header_shadow']))) / 100
    return (
        ":root{"
        f"--c-page-bg:{c['ap_page_bg']};"
        f"--c-surface:{_rgba(c['ap_surface'], c['ap_surface_opacity'])};"
        f"--c-text:{c['ap_text']};--c-text-heading:{c['ap_heading']};--c-text-muted:{c['ap_muted']};"
        f"--c-border:{c['ap_border']};"
        f"--c-primary:{c['ap_primary']};--c-primary-hover:{c['ap_primary_hover']};"
        f"--c-table-header-bg:{c['ap_table_header_bg']};--c-table-header-text:{c['ap_table_header_text']};"
        f"--radius-lg:{radius}px;--radius-md:{round(radius * 0.75)}px;--radius-sm:{round(radius * 0.5)}px;"
        f"--page-head-bg:{_rgba(c['ap_header_bg'], c['ap_header_opacity'])};"
        f"--page-head-shadow:{shadow:g};"
        f"--side-menu-bg:{_rgba(c['ap_menu_bg'], c['ap_menu_opacity'])};--side-menu-text:{c['ap_menu_text']};"
        "}"
    )


templates.env.globals['appearance_css'] = appearance_css

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
USER_PERMISSIONS_HELD: dict = {}     # {username: set(key in db_utils.USER_PERMISSIONS)}
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
            # A failure here must not stop page access: nobody holds a
            # permission until it can be read again.
            try:
                held = read_user_permissions(conn)
            except Exception as e:
                conn.rollback()
                logger.warning("Could not load user permissions - denying them for now: %s", e)
                held = {}
        USER_PAGE_ACCESS.clear()          # replace wholesale, so removed grants disappear
        USER_PAGE_ACCESS.update(access)
        USER_PERMISSIONS_HELD.clear()
        USER_PERMISSIONS_HELD.update(held)
        _PERMISSIONS_LOADED_AT = time.time()
    except Exception as e:
        logger.warning("Could not load users/page access from Aurora — falling back to hardcoded admin only: %s", e)
    VALID_USERS.update(_ADMIN_FALLBACK)


def is_admin(user: str | None) -> bool:
    return user == ADMIN_USERNAME


# Not 'is_admin': some pages pass an is_admin True/False of their own, which
# would hide a function of the same name.
templates.env.globals['user_is_admin'] = is_admin


def is_local_app() -> bool:
    """Running on your own computer, not Lambda (AWS sets AWS_LAMBDA_FUNCTION_NAME
    in every Lambda). The account menu's Sign In To AWS only works here."""
    return not os.getenv("AWS_LAMBDA_FUNCTION_NAME")


templates.env.globals['is_local_app'] = is_local_app


# Name and email for the account menu at the top right of every page, cached
# per process the same way as page names. Settings > Users reloads this
# container's copy on save; other containers catch up within the TTL. A failed
# read keeps the last copy and waits the same time before trying again.
_USER_DETAILS: dict = {}
_USER_DETAILS_LOADED_AT: float = 0.0
USER_DETAILS_TTL_SECONDS = 60


def reload_user_details() -> None:
    global _USER_DETAILS_LOADED_AT
    _USER_DETAILS_LOADED_AT = time.time()
    try:
        with get_database_connection() as conn:
            details = read_user_details(conn)
        _USER_DETAILS.clear()
        _USER_DETAILS.update(details)
    except Exception as e:
        logger.warning("Could not load user details - using the last known values: %s", e)


def user_details(username: str | None) -> dict:
    """{'first_name', 'last_name', 'email'}, empty strings where not stored.
    Usage in a template:  {% set ud = user_details(user_name) %}"""
    if time.time() - _USER_DETAILS_LOADED_AT >= USER_DETAILS_TTL_SECONDS:
        reload_user_details()
    return dict(_USER_DETAILS.get(username) or {'first_name': '', 'last_name': '', 'email': ''})


templates.env.globals['user_details'] = user_details


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
        return frozenset(DEFAULT_PAGE_KEYS - _hidden_page_keys())
    # Configured: exactly what was saved, an empty set included. The marker is
    # bookkeeping, never a page. Hidden pages are taken off (kept in storage).
    return frozenset((stored & ALL_PAGE_KEYS) - _hidden_page_keys())


def _hidden_page_keys() -> set:
    return {p.key for p in PAGES if page_hidden(p.key)}


def user_can(user: str | None, page_key: str) -> bool:
    return page_key in get_user_pages(user)


def user_has_permission(user: str | None, permission: str) -> bool:
    """A special permission (db_utils.USER_PERMISSIONS). The admin holds them all.
    Same rules as get_user_pages: denied if the cache cannot be refreshed, and
    for a deleted account."""
    if is_admin(user):
        return True
    if not user:
        return False
    if not _permissions_are_fresh():
        _reload_users_from_aurora()
        if not _permissions_are_fresh():
            logger.warning("Permissions could not be refreshed - denying %s", user)
            return False
    return user in VALID_USERS and permission in USER_PERMISSIONS_HELD.get(user, ())


templates.env.globals['user_has_permission'] = user_has_permission


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


def require_page_api(*page_keys: str):
    """Dependency for data/download endpoints. 401 when signed out, 403 when
    signed in without access — matching what the endpoints already returned.
    With several page keys, access to any one of them is enough (the KPI chart
    endpoints also serve the Weekly Business Review page).

    Every endpoint gets its page's permission, including the four default
    tools: a URL can be requested directly, so being reachable only from a
    page the user has passed is not a control.
    """
    def _dependency(request: Request) -> str:
        user = get_current_user(request)
        if not user:
            raise HTTPException(status_code=401)
        if not any(user_can(user, key) for key in page_keys):
            logger.info("Access denied (api): %s -> %s", user, ", ".join(page_keys))
            raise HTTPException(status_code=403)
        return user
    return _dependency


_reload_users_from_aurora()


# Session timeout and Force Re-login time, from the settings table so every
# container agrees. Cached for SESSION_CONFIG_TTL_SECONDS; a failed read keeps
# the last values loaded (never the defaults, which would undo a Force Re-login).
_SESSION_CONFIG: dict = {k: SETTINGS_DEFAULTS[k] for k in ('session_timeout_minutes', 'sessions_invalidated_at')}
_SESSION_CONFIG_LOADED_AT: float = 0.0
SESSION_CONFIG_TTL_SECONDS = 30
# A session's last_seen_at is written at most this often per login.
SESSION_TOUCH_SECONDS = 60


def reload_session_config() -> None:
    global _SESSION_CONFIG_LOADED_AT
    _SESSION_CONFIG_LOADED_AT = time.time()
    try:
        with get_database_connection() as conn:
            _SESSION_CONFIG.update(read_session_settings(conn))
    except Exception as e:
        logger.warning("Could not load session settings - using the last known values: %s", e)


def session_config() -> dict:
    if time.time() - _SESSION_CONFIG_LOADED_AT >= SESSION_CONFIG_TTL_SECONDS:
        reload_session_config()
    return dict(_SESSION_CONFIG)


def _track_session(request: Request, user: str, timeout_minutes: int) -> bool:
    """Keep this login's user_sessions row up to date (see db_utils.touch_session).
    Returns False when the login has ended and must be signed out.

    The cookie's seen_epoch means most requests skip the database. The cookie
    is only changed after the write succeeds; on a database error the page
    still loads and the next request tries again."""
    now = time.time()
    session_id = request.session.get('session_id')
    seen = request.session.get('seen_epoch')
    if session_id and seen and now - seen < SESSION_TOUCH_SECONDS:
        return True
    try:
        with get_database_connection() as conn:
            current = touch_session(conn, session_id or new_session_id(), user, _ENV, timeout_minutes)
    except Exception as e:
        logger.warning("Could not record session activity for %s: %s", user, e)
        return True
    if current is None:
        return False
    request.session['session_id'] = current
    request.session['seen_epoch'] = now
    return True


def _end_this_session(request: Request, reason: str) -> None:
    session_id = request.session.get('session_id')
    if not session_id:
        return
    try:
        with get_database_connection() as conn:
            close_sessions(conn, reason, session_config()['session_timeout_minutes'], session_id=session_id)
    except Exception as e:
        logger.warning("Could not close session %s: %s", session_id, e)


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
    # Other containers change passwords and delete users: re-read on the same
    # TTL as page access, so this check is never more than a minute behind.
    if not _permissions_are_fresh():
        _reload_users_from_aurora()
    # Password changed or account deleted since login. The save already ended
    # the user's sessions; this ends this one in case it was missed.
    if request.session.get('pwd_hash') != VALID_USERS.get(user):
        _end_this_session(request, 'password_changed')
        request.session.clear()
        return None
    config = session_config()
    # Force Re-login after this login. Its sessions were ended at the click.
    if request.session.get('login_time_epoch', 0) < config['sessions_invalidated_at']:
        request.session.clear()
        return None
    if not _track_session(request, user, config['session_timeout_minutes']):
        request.session.clear()
        return None
    app_log.note_user(user)
    return user
