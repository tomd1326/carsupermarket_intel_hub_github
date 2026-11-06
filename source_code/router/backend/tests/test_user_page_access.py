"""Page access on Settings > User Accounts: what the popup ticks, what its save
writes, and that new accounts start with no pages.

Run from source_code/router/backend:
    poetry run pytest tests/test_user_page_access.py
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import pytest
from jinja2 import Environment, FileSystemLoader

from routers import settings
from routers.page_registry import ALL_PAGE_KEYS, CONFIGURED_MARKER, DEFAULT_PAGE_KEYS, PAGES, shown_page_access
from utils import db_utils


def _fake_conn(fetchone=None):
    cur = mock.MagicMock()
    cur.fetchone.side_effect = fetchone if isinstance(fetchone, list) else [fetchone] * 10
    conn = mock.MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


def _marker_inserts(cur):
    return [c for c in cur.execute.call_args_list
            if 'INSERT INTO USER_PAGE_ACCESS' in c.args[0].upper() and CONFIGURED_MARKER in c.args[1]]


def test_saved_access_is_shown_exactly():
    assert shown_page_access({CONFIGURED_MARKER, 'kpi'}) == {'kpi'}
    assert shown_page_access({CONFIGURED_MARKER}) == set()


def test_never_saved_active_user_keeps_the_defaults():
    assert shown_page_access(None) == set(DEFAULT_PAGE_KEYS)


def test_new_user_starts_with_no_pages():
    conn, cur = _fake_conn()
    db_utils.add_user(conn, "jo")
    assert len(_marker_inserts(cur)) == 1
    inserts = [c for c in cur.execute.call_args_list if 'INSERT INTO USERS' in c.args[0].upper()]
    assert len(inserts) == 1 and inserts[0].args[1] == ("jo", db_utils.SSO_PWD_PLACEHOLDER)


def _save(ticked, stored=None, hidden=(), is_new=False):
    """Run _save_user_access; returns (result, what set_page_access_many was given)."""
    visible = [p for p in PAGES if p.key not in hidden]
    with mock.patch.object(settings, 'load_all_page_access', return_value={'jo': stored} if stored else {}), \
            mock.patch.object(settings.core, 'visible_pages', return_value=visible), \
            mock.patch.object(settings, 'set_page_access_many') as write:
        result = settings._save_user_access(mock.MagicMock(), 'jo', ticked, 'admin', is_new=is_new)
    return result, (write.call_args.args[1] if write.called else None)


def test_ticks_not_shown_leave_an_existing_user_alone():
    assert _save(None, stored={CONFIGURED_MARKER, 'kpi'}) == (None, None)


def test_ticks_not_shown_give_a_new_user_no_pages():
    assert _save(None, is_new=True) == (set(), {'jo': set()})


def test_unchanged_ticks_write_nothing():
    assert _save({'kpi'}, stored={CONFIGURED_MARKER, 'kpi'}) == (None, None)


def test_active_user_on_defaults_is_not_written_when_unchanged():
    assert _save(set(DEFAULT_PAGE_KEYS)) == (None, None)


def test_unticking_everything_saves_no_pages():
    assert _save(set(), stored={CONFIGURED_MARKER, 'kpi'}) == (set(), {'jo': set()})


def test_new_user_with_nothing_ticked_is_still_written():
    assert _save(set(), is_new=True) == (set(), {'jo': set()})


def test_hidden_page_access_is_kept():
    result, written = _save({'mot_check'}, stored={CONFIGURED_MARKER, 'kpi'}, hidden={'kpi'})
    assert result == {'kpi', 'mot_check'}
    assert written == {'jo': {'kpi', 'mot_check'}}


def _render_users(access_loaded=True, viewing_prod=False):
    """users.html with its includes replaced, to check the template's own output."""
    templates_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'templates')
    env = Environment(loader=FileSystemLoader(templates_dir))
    source = env.loader.get_source(env, 'users.html')[0]
    source = source.replace("{% from '_navigation.html' import page_header with context %}", "")
    source = source.replace("{{ page_header('Settings', user) }}", "")
    source = source.replace("{% include '_settings_head.html' %}", "")
    source = source.replace("{% include '_settings_nav.html' %}", "")
    env.globals.update(static_url=lambda p: p, page_name=lambda k: 'Name ' + k)
    return env.from_string(source).render(
        request=mock.MagicMock(query_params={}), user='tdrayson', BASE_URL='', admin_username='tdrayson',
        error=None,
        all_users=['ann', 'bob', 'tdrayson'], buyers=set(), people={}, last_logins={},
        pages=PAGES, access={'ann': {'kpi', 'dse'}, 'bob': set()} if access_loaded else {},
        access_loaded=access_loaded, can_view_prod=False, source='this', viewing_prod=viewing_prod, this_env='dev',
    )


def test_users_page_shows_pages_count_filter_and_ticks():
    html = _render_users()
    assert f'2 of {len(PAGES)}' in html
    assert f'0 of {len(PAGES)}' in html
    assert 'data-pages="dse,kpi"' in html
    assert 'password' not in html.lower()
    assert 'name="access_shown"' in html
    assert html.count('class="form-check-input um-access"') == len(ALL_PAGE_KEYS)


def test_users_page_without_access_sends_no_ticks():
    html = _render_users(access_loaded=False)
    assert 'name="access_shown"' not in html
    assert 'um-access-all' not in html
    assert 'could not be loaded' in html


def test_old_page_access_address_redirects_to_user_accounts():
    resp = settings.page_access_page(user=settings.ADMIN_USERNAME)
    assert resp.status_code == 303
    assert resp.headers['location'].endswith('/settings/users')
