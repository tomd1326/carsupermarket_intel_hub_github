"""The list of pages access can be granted to, and the keys that name them.

This is the single source of truth for both halves of page access: the checks
on the routes, and the tiles rendered on the home page. Keeping one list means
a page cannot end up linked-but-blocked, or blocked-but-linked.

Imports nothing from other routers on purpose — every router imports `core`,
and `core` imports this, so a dependency the other way would be circular.

Adding a page:
  1. Add a Page here.
  2. Put require_page(<key>) on its route, and require_page_api(<key>) on the
     endpoints its page calls.
Nothing else. The tile and the grid column both follow from this list.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Page:
    key: str            # stored in user_page_access.page_key — never change one in place
    path: str           # URL, appended to BASE_URL
    label: str          # shown on the home tile and as the grid's column heading
    default_for_existing: bool   # see DEFAULT_PAGE_KEYS below


# Order here is the order of the home tiles and of the grid's columns.
PAGES: tuple[Page, ...] = (
    # The four everyone can reach today. Any user with no saved configuration
    # gets exactly these, so nobody's access changes when this ships.
    Page('buying_requirements', '/buying-requirements',        'Buying Requirements',      True),
    Page('tasks',               '/tasks',                      'Supplier Stock Evaluator', True),
    Page('mot_check',           '/mot-check',                  'MOT Check',                True),
    Page('orders_deliveries',   '/reports/orders-deliveries',  'Orders & Deliveries Report', True),
    # Admin-only today. Pre-ticked for nobody but the admin, so they stay that
    # way until someone is deliberately granted one.
    Page('kpi',                 '/kpi',                        'KPI Dashboard',            False),
    Page('weekly_business_review', '/weekly-business-review',  'Weekly Business Review',   False),
    Page('google_ads_campaigns', '/google-ads-campaigns',      'Google Ads Campaigns',     False),
    Page('stock_list',          '/stock-list',                 'Stock List',               False),
    Page('cap_lookup',          '/cap-lookup',                 'CAP ID Lookup',            False),
    Page('assess_vehicle',      '/assess-vehicle',             'Assess Vehicle',           False),
    # Hidden from the home tiles today but reachable by URL, because its route
    # only checks that you are logged in. Enforcement closes that.
    Page('subsidy_rates',       '/subsidy-rates',              'Subsidy Rate Calculator',  False),
    Page('apex_reports',        '/reports/apex',               'Apex Reports',             False),
)

PAGES_BY_KEY: dict[str, Page] = {p.key: p for p in PAGES}
ALL_PAGE_KEYS: frozenset[str] = frozenset(PAGES_BY_KEY)

# What a user gets when they have never been configured — i.e. no
# CONFIGURED_MARKER row. Once configured, their stored grants are used exactly
# as saved, an empty set included.
DEFAULT_PAGE_KEYS: frozenset[str] = frozenset(
    p.key for p in PAGES if p.default_for_existing
)

# Written alongside a user's grants to record "this user has been configured".
# Without it, unticking every box saves zero rows, which is indistinguishable
# from a user nobody has touched — so the defaults would silently come back.
#
# Storage bookkeeping, never a page: it is deliberately NOT in ALL_PAGE_KEYS,
# is never rendered as a column, never appears in the set returned by
# get_user_pages, and is rejected if it ever arrives as a submitted page key.
CONFIGURED_MARKER = '__configured__'

# The Settings area is intentionally absent from PAGES. It stays admin-only, so
# nobody can be handed user management by a stray tick.
