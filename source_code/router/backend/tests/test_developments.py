"""Settings > Developments and What's New (utils/developments.py, routers/developments.py).

The risks covered: a bad address turning an email link into something else,
typed text being read as HTML, a draft reaching an email or What's New, the wrong
developments going in an email, recipients seeing each other, the records
landing in the wrong database, and someone other than the admin using the page.

No test touches a real database.

Run from source_code/router/backend:
    poetry run pytest tests/test_developments.py
"""

import os
import re
import sys

import pytest
from fastapi import HTTPException
from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from routers import developments as router
from routers.page_registry import ALL_PAGE_KEYS
from utils import developments as dv

ADMIN = router.is_admin.__globals__['ADMIN_USERNAME']
PAGES = {"apex_missing_finance": {"name": "Apex Missing Finance", "path": "/reports/apex-missing-finance"}}
_TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'templates')


def dev(dev_id=1, **changes):
    base = {"id": dev_id, "title": "Report", "dev_type": "New Report", "page_key": "apex_missing_finance",
            "summary": "A summary", "details": "", "replaces": "", "guide": "", "faqs": [],
            "released_label": "", "status": "live"}
    return {**base, **changes}


def build(developments, subject="Update", intro="Hello", **template_changes):
    return dv.build_email(developments, subject, intro, {**dv.TEMPLATE_DEFAULTS, **template_changes}, PAGES)


# ── Website address ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "javascript:alert(1)", "http://uk-intel-hub.aramis-group.eu", "https://", "uk-intel-hub.aramis-group.eu",
    'https://a.eu/"><script>', "https://a.eu/x y", "https://a.eu?x=1", "https://user@a.eu", "",
])
def test_bad_site_address_refused(bad):
    assert dv.clean_site_url(bad) is None
    with pytest.raises(ValueError):
        dv.clean_template({"site_url": bad})


def test_site_address_loses_its_trailing_slash():
    assert dv.clean_site_url("https://uk-intel-hub.aramis-group.eu/") == "https://uk-intel-hub.aramis-group.eu"


def test_bad_stored_values_fall_back_to_the_defaults():
    template = dv.template_from_stored({"site_url": "javascript:alert(1)", "header_colour": "red;}", "heading": "Mine"})
    assert template["site_url"] == dv.TEMPLATE_DEFAULTS["site_url"]
    assert template["header_colour"] == dv.TEMPLATE_DEFAULTS["header_colour"]
    assert template["heading"] == "Mine"


@pytest.mark.parametrize("key, value", [
    ("accent_colour", "blue"), ("accent_colour", "#12345"), ("font_size", "abc"), ("font_size", "99"),
    ("email_width", "10"), ("heading", " "),
])
def test_bad_template_values_refused(key, value):
    with pytest.raises(ValueError):
        dv.clean_template({key: value})


# ── Development checks ───────────────────────────────────────────────────────

def test_clean_development_needs_the_basics():
    good = {"title": "T", "dev_type": "Improvement"}
    assert dv.clean_development(good, ALL_PAGE_KEYS)["status"] == "draft"
    for bad in ({**good, "title": " "}, {**good, "dev_type": ""}, {**good, "status": "nonsense"},
                {**good, "status": "live"}, {**good, "page_key": "not_a_page"}, {**good, "released_on": "soon"},
                {**good, "faqs": [{"question": "Q", "answer": ""}]}, {**good, "faqs": "text"}):
        with pytest.raises(ValueError):
            dv.clean_development(bad, ALL_PAGE_KEYS)


def test_blank_faq_rows_are_dropped():
    cleaned = dv.clean_development({"title": "T", "dev_type": "Improvement",
                                    "faqs": [{"question": " ", "answer": ""}, {"question": "Q", "answer": "A"}]},
                                   ALL_PAGE_KEYS)
    assert cleaned["faqs"] == [{"question": "Q", "answer": "A"}]


# ── The email ────────────────────────────────────────────────────────────────

def test_typed_text_is_escaped():
    html = build([dev(title="<script>a</script>", summary="<b>b</b>", replaces="<i>c</i>")],
                 subject="<u>s</u>", intro="<img src=x>", heading="<h1>d</h1>")["html"]
    for bad in ("<script>a", "<b>b</b>", "<i>c</i>", "<u>s</u>", "<img src=x>", "<h1>d</h1>"):
        assert bad not in html
    assert "&lt;script&gt;a" in html


def test_line_breaks_are_kept():
    assert "one<br>two" in build([dev(summary="one\ntwo")])["html"]


def test_only_the_given_developments_appear():
    html = build([dev(1, title="Wanted")])["html"]
    assert "Wanted" in html and "Not Wanted" not in html


def test_groups_follow_the_template_order():
    html = build([dev(1, dev_type="Improvement", title="B"), dev(2, dev_type="New Module", title="A"),
                  dev(3, dev_type="Something Else", title="C")])["html"]
    assert html.index("New Module") < html.index("Improvement") < html.index("Something Else")
    assert "New Report" not in html


def test_links_use_the_template_address():
    site = "https://uk-intel-hub.aramis-group.eu"
    built = build([dev(7, guide="How to")], site_url=site)
    assert f'href="{site}/reports/apex-missing-finance"' in built["html"]
    assert f'href="{site}/whats-new#dev-7"' in built["html"]
    assert "Open Apex Missing Finance" in built["html"]
    assert f"{site}/whats-new#dev-7" in built["text"]


def test_no_read_more_link_when_there_is_nothing_more():
    built = build([dev(7)])
    assert "/whats-new" not in built["html"] and "/whats-new" not in built["text"]


def test_no_page_button_without_a_page():
    assert "Open " not in build([dev(page_key=None, guide="How to")])["html"]


# ── What can go in an email ──────────────────────────────────────────────────

def test_email_problem():
    assert dv.email_problem([1], [dev(1)]) is None
    assert "Tick" in dv.email_problem([], [])
    assert "no longer exists" in dv.email_problem([1, 2], [dev(1)])
    assert "Draft One" in dv.email_problem([1], [dev(1, status="draft", title="Draft One")])


def test_subject_is_one_line():
    assert dv.clean_subject("  One\r\nBcc: someone@example.com ") == "One Bcc: someone@example.com"
    with pytest.raises(ValueError):
        dv.clean_subject(" ")


# ── Sending ──────────────────────────────────────────────────────────────────

def test_recipients_are_split_and_each_kept_once():
    assert dv.clean_recipients("a@x.com; b@x.com,\nA@x.com") == ["a@x.com", "b@x.com"]


@pytest.mark.parametrize("value", ["", "  ; ", None, "a@x.com; not-an-address"])
def test_recipients_refuse_blank_or_bad_addresses(value):
    with pytest.raises(ValueError):
        dv.clean_recipients(value)


def test_past_email_is_sent_on_bcc_only(monkeypatch):
    sent = {}
    monkeypatch.setattr(router, "is_admin", lambda user: True)
    monkeypatch.setattr(router.dv, "get_email", lambda i: {"subject": "S", "html": "<p>x</p>", "text": "x"})
    monkeypatch.setattr(router.email_client, "configured_sender", lambda: "hub@x.com")
    monkeypatch.setattr(router.email_client, "send_email",
                        lambda to, subject, html, sender=None, bcc=None: sent.update(to=to, bcc=bcc, sender=sender))
    result = router.developments_email_resend(1, None, {"to": "a@x.com; b@x.com"}, user="admin")
    assert result["status"] == "ok"
    assert sent == {"to": "hub@x.com", "bcc": ["a@x.com", "b@x.com"], "sender": "hub@x.com"}


def test_failed_send_is_reported(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("403 from Graph")
    monkeypatch.setattr(router, "is_admin", lambda user: True)
    monkeypatch.setattr(router.dv, "get_email", lambda i: {"subject": "S", "html": "<p>x</p>", "text": "x"})
    monkeypatch.setattr(router.email_client, "configured_sender", lambda: "hub@x.com")
    monkeypatch.setattr(router.email_client, "send_email", fail)
    result = router.developments_email_resend(1, None, {"to": "a@x.com"}, user="admin")
    assert result["status"] == "error" and "403" in result["message"]


# ── Which database, who, and what shows ──────────────────────────────────────

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.conn.statements.append((sql, params))
        self.rows = []
        if sql.startswith("SELECT to_regclass"):
            self.rows = [("developments",)]

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeConn:
    def __init__(self):
        self.statements = []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        pass


def test_records_use_the_connection_that_reaches_prod_from_everywhere(monkeypatch):
    """get_prod_pricer_connection would use the dev database on the deployed dev
    site, which would split the records into two copies."""
    used = []
    conn = FakeConn()

    class Context:
        def __enter__(self):
            used.append(True)
            return conn

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(dv, "get_prod_activity_connection", Context)
    dv.list_developments()
    dv.get_template()
    assert len(used) == 2
    source = open(dv.__file__, encoding='utf-8').read()
    assert 'get_prod_pricer_connection' not in source and 'get_database_connection' not in source


def test_only_live_rows_are_asked_for_on_whats_new(monkeypatch):
    conn = FakeConn()

    class Context:
        def __enter__(self):
            return conn

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(dv, "get_prod_activity_connection", Context)
    dv.list_developments(live_only=True)
    assert any("FROM developments WHERE status = 'live'" in sql for sql, _ in conn.statements)


def test_first_setup_adds_the_apex_draft(monkeypatch):
    conn = FakeConn()
    original = FakeCursor.execute

    def execute(self, sql, params=None):
        original(self, sql, params)
        if sql.startswith("SELECT to_regclass"):
            self.rows = [(None,)]

    monkeypatch.setattr(FakeCursor, "execute", execute)
    dv._ensure_tables(conn)
    inserts = [p for sql, p in conn.statements if sql.startswith("INSERT INTO developments")]
    assert len(inserts) == 1
    assert "apex_missing_finance" in inserts[0]
    assert any("'draft'" in sql for sql, _ in conn.statements if sql.startswith("INSERT INTO developments"))


def test_seed_page_exists():
    assert all(seed["page_key"] in ALL_PAGE_KEYS for seed in dv._SEEDS)


@pytest.mark.parametrize("call", [
    lambda: router.developments_data(None, user="someone"),
    lambda: router.developments_save(None, {"title": "T"}, user="someone"),
    lambda: router.developments_delete(None, {"id": 1}, user="someone"),
    lambda: router.developments_template_save(None, {}, user="someone"),
    lambda: router.developments_email_preview(None, {}, user="someone"),
    lambda: router.developments_email_generate(None, {}, user="someone"),
    lambda: router.developments_email_get(1, None, user="someone"),
    lambda: router.developments_email_send(None, {}, user="someone"),
    lambda: router.developments_email_resend(1, None, {}, user="someone"),
    lambda: router.developments_data(None, user=None),
])
def test_admin_routes_refuse_everyone_else(call):
    with pytest.raises(HTTPException) as e:
        call()
    assert e.value.status_code == 403


def test_settings_page_sends_everyone_else_home():
    response = router.developments_page(None, user="someone")
    assert response.status_code == 303 and response.headers["location"].endswith("/home")


def test_whats_new_sends_signed_out_people_to_login():
    with pytest.raises(router.PageRedirect):
        router.whats_new(None, user=None)


def test_generate_refuses_a_draft_and_keeps_no_record(monkeypatch):
    recorded = []
    monkeypatch.setattr(dv, "get_developments", lambda ids: [dev(1, status="draft", title="Draft One")])
    monkeypatch.setattr(dv, "get_template", lambda: dict(dv.TEMPLATE_DEFAULTS))
    monkeypatch.setattr(dv, "record_email", lambda *a: recorded.append(a) or 1)
    monkeypatch.setattr(router, "_pages", lambda: PAGES)
    result = router.developments_email_generate(None, {"subject": "S", "ids": [1]}, user=ADMIN)
    assert result["status"] == "error" and "Draft One" in result["message"]
    assert recorded == []


def test_generate_records_the_ticked_developments(monkeypatch):
    recorded = []
    asked = []
    monkeypatch.setattr(dv, "get_developments", lambda ids: asked.append(ids) or [dev(1)])
    monkeypatch.setattr(dv, "get_template", lambda: dict(dv.TEMPLATE_DEFAULTS))
    monkeypatch.setattr(dv, "record_email", lambda *a: recorded.append(a) or 5)
    monkeypatch.setattr(router, "_pages", lambda: PAGES)
    result = router.developments_email_generate(None, {"subject": "S", "intro": "I", "ids": ["1"]}, user=ADMIN)
    assert result["status"] == "ok" and result["id"] == 5
    assert asked == [[1]] and len(recorded) == 1


# ── Pages ────────────────────────────────────────────────────────────────────

def test_new_templates_are_escaped_by_the_site():
    """Starlette switches escaping on for .html templates; both pages rely on it."""
    source = open(os.path.join(_TEMPLATES, 'whats_new.html'), encoding='utf-8').read()
    assert '|safe' not in source
    source = open(os.path.join(_TEMPLATES, 'emails', 'development_update.html'), encoding='utf-8').read()
    assert '|safe' not in source


def test_preview_frame_is_sandboxed():
    source = open(os.path.join(_TEMPLATES, 'developments.html'), encoding='utf-8').read()
    assert 'id="email-preview" title="Email Preview" sandbox=""' in source


def test_every_template_box_on_the_page_is_a_real_setting():
    source = open(os.path.join(_TEMPLATES, 'developments.html'), encoding='utf-8').read()
    assert set(re.findall(r'data-template="(\w+)"', source)) == set(dv.TEMPLATE_DEFAULTS)


def test_nav_links_to_developments():
    from routers import settings
    env = Environment(loader=FileSystemLoader(_TEMPLATES), autoescape=True)
    env.globals['page_settings_sections'] = settings.PAGE_SETTINGS_SECTIONS
    env.globals['page_name'] = lambda key: key
    html = env.get_template('_settings_nav.html').render(
        BASE_URL="", sections=settings.SETTINGS_SECTIONS, active_section="developments")
    assert '/settings/developments' in html
