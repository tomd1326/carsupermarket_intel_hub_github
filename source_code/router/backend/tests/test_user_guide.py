"""The User Guide (utils/user_guide.py, routers/user_guide.py).

The risks covered: someone seeing a section they should not, a new section being
visible to anyone but the admin, typed text being read as HTML, a saved link being
something other than a web address, and someone other than the admin changing it.

No test touches a real database.

Run from source_code/router/backend:
    poetry run pytest tests/test_user_guide.py
"""

import os
import sys

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from routers import user_guide as router
from routers.core import PageRedirect
from routers.page_registry import ALL_PAGE_KEYS
from utils import user_guide as ug

ADMIN = router.is_admin.__globals__['ADMIN_USERNAME']
_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')


def see(user, can_open=(), **section):
    base = {"visibility": ug.ONLY_ME, "page_key": "trade_report", "chosen_users": []}
    return ug.can_see({**base, **section}, user, lambda u: u == ADMIN, lambda u, key: key in can_open)


# ── Who can see a section ────────────────────────────────────────────────────

def test_only_me_is_the_admin_alone():
    assert see(ADMIN)
    assert not see("jo", can_open=["trade_report"])


def test_admin_sees_every_section():
    for visibility in ug.VISIBILITIES:
        assert see(ADMIN, visibility=visibility, page_key=None)


def test_page_access_follows_the_page():
    assert see("jo", can_open=["trade_report"], visibility=ug.PAGE_ACCESS)
    assert not see("jo", can_open=["kpi"], visibility=ug.PAGE_ACCESS)
    assert not see("jo", can_open=["kpi"], visibility=ug.PAGE_ACCESS, page_key=None)


def test_everyone_overrides_page_access():
    assert see("jo", visibility=ug.EVERYONE)


def test_chosen_people_override_page_access():
    assert see("jo", visibility=ug.CHOSEN, chosen_users=["jo"])
    assert not see("sam", can_open=["trade_report"], visibility=ug.CHOSEN, chosen_users=["jo"])


def test_signed_out_sees_nothing():
    assert not see(None, visibility=ug.EVERYONE)


def test_unknown_visibility_shows_nothing():
    assert not see("jo", visibility="other")


# ── Checks ───────────────────────────────────────────────────────────────────

def test_a_new_section_defaults_to_only_me():
    assert ug.clean_section({"title": "T"}, ALL_PAGE_KEYS, {"jo"})["visibility"] == ug.ONLY_ME


def test_seeds_are_only_me_on_a_real_page():
    for seed in ug._SEEDS:
        assert seed["page_key"] in ALL_PAGE_KEYS


@pytest.mark.parametrize("raw", [
    {"title": " "},
    {"title": "T", "visibility": "bad"},
    {"title": "T", "page_key": "no_such_page"},
    {"title": "T", "visibility": ug.PAGE_ACCESS},
    {"title": "T", "visibility": ug.CHOSEN},
    {"title": "T", "visibility": ug.CHOSEN, "chosen_users": ["not_a_user"]},
    {"title": "T", "visibility": ug.CHOSEN, "chosen_users": "jo"},
])
def test_bad_sections_refused(raw):
    with pytest.raises(ValueError):
        ug.clean_section(raw, ALL_PAGE_KEYS, {"jo"})


@pytest.mark.parametrize("key, value", [("font_size", "abc"), ("font_size", "99"), ("nav_width", "10"), ("title", " ")])
def test_bad_settings_refused(key, value):
    with pytest.raises(ValueError):
        ug.clean_settings({key: value})


def test_bad_stored_numbers_cannot_reach_the_page_css():
    settings = ug.settings_from_stored({"font_size": "15px;}body{display:none", "nav_width": "300"})
    assert settings["font_size"] == ug.SETTINGS_DEFAULTS["font_size"]
    assert settings["nav_width"] == "300"


# ── Turning text into the page ───────────────────────────────────────────────

def test_typed_html_is_escaped():
    html = str(ug.render_body('<script>alert(1)</script>\n- <img src=x onerror=y>\n## <b>x</b>\n> <i>n</i>'))
    assert "<script" not in html and "<img" not in html and "<b>" not in html and "<i>" not in html


def test_formatting():
    html = str(ug.render_body("## Head\n\nOne **bold**\nTwo\n\n- A\n- B\n\n1. C\n\n> Note"))
    assert "<h3>Head</h3>" in html
    assert "<p>One <strong>bold</strong><br>Two</p>" in html
    assert "<ul>\n<li>A</li>\n<li>B</li>\n</ul>" in html
    assert "<ol>\n<li>C</li>\n</ol>" in html
    assert '<div class="guide-note">Note</div>' in html


def test_empty_text():
    assert str(ug.render_body("")) == ""
    assert str(ug.render_body(None)) == ""


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,x", "//evil.example", "/\\evil.example"])
def test_bad_links_stay_as_text(url):
    assert "<a " not in str(ug.render_body(f"[x]({url})"))


def test_links_cannot_break_out_of_the_address():
    html = str(ug.render_body('[x](https://a.eu/"onmouseover="y)'))
    assert 'onmouseover="y' not in html


def test_good_links():
    assert 'href="https://a.eu/x" target="_blank"' in str(ug.render_body("[x](https://a.eu/x)"))
    assert 'href="/reports/trade"' in str(ug.render_body("[x](/reports/trade)"))


# ── Who may use the routes ───────────────────────────────────────────────────

@pytest.mark.parametrize("call", [
    lambda: router.user_guide_data(None, user="jo"),
    lambda: router.user_guide_save(None, {"title": "T"}, user="jo"),
    lambda: router.user_guide_delete(None, {"id": 1}, user="jo"),
    lambda: router.user_guide_reorder(None, {"ids": [1]}, user="jo"),
    lambda: router.user_guide_generate(None, user="jo"),
    lambda: router.user_guide_settings_save(None, {}, user="jo"),
    lambda: router.user_guide_preview(None, {"body": ""}, user="jo"),
])
def test_admin_routes_refuse_everyone_else(call):
    with pytest.raises(HTTPException) as e:
        call()
    assert e.value.status_code == 403


def test_settings_page_sends_everyone_else_home():
    assert router.user_guide_settings_page(None, user="jo").status_code == 303


def test_guide_sends_signed_out_people_to_login():
    with pytest.raises(PageRedirect):
        router.user_guide_page(None, user=None)


# ── Generating sections ──────────────────────────────────────────────────────

class _FakeCursor:
    def __init__(self):
        self.inserted = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, values=None):
        self.inserted.append(values)

    def fetchone(self):
        return (len(self.inserted),)


class _FakeConnection:
    def __init__(self):
        self.written = _FakeCursor()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self.written

    def commit(self):
        pass


def _generate(monkeypatch, pages, existing=()):
    conn = _FakeConnection()
    sections = [{"page_key": key} for key in existing]
    monkeypatch.setattr(ug, "read_guide", lambda: (sections, dict(ug.SETTINGS_DEFAULTS, starter="About {page}")))
    monkeypatch.setattr(ug, "get_prod_activity_connection", lambda: conn)
    return ug.generate_sections(pages, ADMIN), conn.written.inserted


def test_generated_section_is_only_me_with_the_page_name_in_the_starter(monkeypatch):
    added, inserted = _generate(monkeypatch, [("kpi", "KPI Dashboard")])
    assert added == [1]
    assert inserted == [("KPI Dashboard", "About KPI Dashboard", "kpi", ug.ONLY_ME, ADMIN)]


def test_a_page_with_a_section_is_left_alone(monkeypatch):
    added, inserted = _generate(monkeypatch, [("kpi", "KPI Dashboard")], existing=["kpi"])
    assert added == [] and inserted == []


def _route_generate(monkeypatch, payload):
    given = {}

    def fake(pages, user):
        given["pages"] = pages
        return [7]
    monkeypatch.setattr(router.ug, "generate_sections", fake)
    monkeypatch.setattr(router, "_reload_menu", lambda: None)
    monkeypatch.setattr(router, "page_name", lambda key: "Name " + key)
    return router.user_guide_generate(None, payload, user=ADMIN), given


def test_generate_one_page_touches_only_that_page(monkeypatch):
    result, given = _route_generate(monkeypatch, {"page_key": "kpi"})
    assert result == {"status": "ok", "added": [7]}
    assert given["pages"] == [("kpi", "Name kpi")]


def test_generate_refuses_a_page_that_does_not_exist(monkeypatch):
    result, given = _route_generate(monkeypatch, {"page_key": "no_such_page"})
    assert result["status"] == "error" and not given


def test_generate_with_no_page_covers_every_page_in_use(monkeypatch):
    monkeypatch.setattr(router, "visible_pages", lambda: [router.PAGES[0], router.PAGES[1]])
    _, given = _route_generate(monkeypatch, {})
    assert [key for key, _ in given["pages"]] == [router.PAGES[0].key, router.PAGES[1].key]


# ── The account menu link ────────────────────────────────────────────────────

def _menu(monkeypatch, sections, can_open=()):
    monkeypatch.setattr(router, "_reload_menu", lambda: None)
    monkeypatch.setattr(router, "_menu_sections", sections)
    monkeypatch.setattr(router, "user_can", lambda u, key: key in can_open)


def test_menu_link_only_for_those_with_something_to_read(monkeypatch):
    section = {"visibility": ug.ONLY_ME, "page_key": "trade_report", "chosen_users": []}
    _menu(monkeypatch, [section], can_open=["trade_report"])
    assert router.user_guide_menu(ADMIN)["show"] is True
    assert router.user_guide_menu("jo")["show"] is False
    _menu(monkeypatch, [{**section, "visibility": ug.PAGE_ACCESS}], can_open=["trade_report"])
    assert router.user_guide_menu("jo")["show"] is True


def test_account_menu_and_settings_nav_link_to_the_guide():
    with open(os.path.join(_ROOT, 'templates', '_navigation.html'), encoding='utf-8') as f:
        assert '/user-guide' in f.read()
    with open(os.path.join(_ROOT, 'templates', '_settings_nav.html'), encoding='utf-8') as f:
        assert '/settings/user-guide' in f.read()
