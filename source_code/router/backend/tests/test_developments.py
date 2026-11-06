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
    assert dv.clean_development(good, ALL_PAGE_KEYS)["status"] == "planned"
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


def test_first_setup_adds_the_apex_entry_as_planned(monkeypatch):
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
    assert any("'planned'" in sql for sql, _ in conn.statements if sql.startswith("INSERT INTO developments"))


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
    lambda: router.developments_request_emails_resend(1, None, user="someone"),
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


# ── Statuses and the table changes ───────────────────────────────────────────

@pytest.mark.parametrize("status", list(dv.STATUSES))
def test_every_status_is_accepted_and_fits_the_column(status):
    cleaned = dv.clean_development({"title": "T", "dev_type": "Improvement", "status": status, "summary": "S"},
                                   ALL_PAGE_KEYS)
    assert cleaned["status"] == status and len(status) <= dv._STATUS_MAX


def test_old_status_column_is_widened_and_drafts_become_planned(monkeypatch):
    conn = FakeConn()
    original = FakeCursor.execute

    def execute(self, sql, params=None):
        original(self, sql, params)
        if "character_maximum_length" in sql:
            self.rows = [(10,)]

    monkeypatch.setattr(FakeCursor, "execute", execute)
    dv._ensure_tables(conn)
    sqls = [sql for sql, _ in conn.statements]
    assert any("ALTER COLUMN status TYPE VARCHAR(30)" in s for s in sqls)
    assert any("SET DEFAULT 'planned'" in s for s in sqls)
    assert any("SET status = 'planned' WHERE status = 'draft'" in s for s in sqls)
    for column in ("source", "requested_by", "request_text", "created_by", "created_at",
                   "confirmation_sent_at", "alert_sent_at", "email_error"):
        assert any(f"ADD COLUMN IF NOT EXISTS {column} " in s for s in sqls)


def test_wide_status_column_is_left_alone(monkeypatch):
    conn = FakeConn()
    original = FakeCursor.execute

    def execute(self, sql, params=None):
        original(self, sql, params)
        if "character_maximum_length" in sql:
            self.rows = [(30,)]

    monkeypatch.setattr(FakeCursor, "execute", execute)
    dv._ensure_tables(conn)
    assert not any("ALTER COLUMN status TYPE" in sql for sql, _ in conn.statements)


# ── Request A Feature ────────────────────────────────────────────────────────

def request_dev(**changes):
    base = {"id": 9, "title": "Faster Export", "page_key": "apex_missing_finance", "requested_by": "jsmith",
            "request_text": "Please {name} make it <b>faster</b>", "created_label": "6 Nov 2026 14:05",
            "source": "request", "confirmation_sent_at": "", "alert_sent_at": ""}
    return {**base, **changes}


@pytest.fixture
def request_setup(monkeypatch):
    """The request routes with no database and no real email. Returns what was
    sent and what was recorded."""
    state = {"sent": [], "recorded": [], "fail": set(), "submitted": [], "dev": request_dev(), "repeat": False}

    def send_email(to, subject, html, sender=None, bcc=None):
        label = "alert" if "New Intelligence Hub Request" in subject else "confirmation"
        if label in state["fail"]:
            raise RuntimeError(f"{label} 500 from Graph")
        state["sent"].append({"to": to, "subject": subject, "html": html, "label": label})

    def submit(user, page_key, title, text):
        state["submitted"].append((user, page_key, title, text))
        return state["dev"], state["repeat"]

    monkeypatch.setattr(router, "get_user_pages", lambda user: frozenset({"apex_missing_finance"}))
    monkeypatch.setattr(router, "user_details", lambda u: {
        "jsmith": {"first_name": "Jo", "last_name": "Smith", "email": "jo@x.com"},
        ADMIN: {"first_name": "Tom", "last_name": "D", "email": "tom@x.com"}}.get(u, {}))
    monkeypatch.setattr(router, "page_name", lambda key: "Apex Missing Finance")
    monkeypatch.setattr(router, "_track", lambda *a, **k: None)
    monkeypatch.setattr(dv, "get_template", lambda: dict(dv.TEMPLATE_DEFAULTS))
    monkeypatch.setattr(dv, "submit_request", submit)
    monkeypatch.setattr(dv, "record_request_emails", lambda *a: state["recorded"].append(a))
    monkeypatch.setattr(dv, "get_development", lambda i: state["dev"])
    monkeypatch.setattr(router.email_client, "configured_sender", lambda: "hub@x.com")
    monkeypatch.setattr(router.email_client, "send_email", send_email)
    return state


@pytest.mark.parametrize("call", [
    lambda: router.request_feature_options(None, user=None),
    lambda: router.request_feature_submit(None, {"title": "T", "text": "x"}, user=None),
])
def test_request_routes_refuse_signed_out_people(call):
    with pytest.raises(HTTPException) as e:
        call()
    assert e.value.status_code == 401


def test_request_page_list_is_only_the_users_pages(monkeypatch):
    monkeypatch.setattr(router, "get_user_pages", lambda user: frozenset({"apex_missing_finance"}))
    monkeypatch.setattr(router, "page_hidden", lambda key: False)
    monkeypatch.setattr(router, "page_name", lambda key: key)
    monkeypatch.setattr(dv, "get_template", lambda: dict(dv.TEMPLATE_DEFAULTS))
    data = router.request_feature_options(None, user="jsmith")
    assert [p["key"] for p in data["pages"]] == ["apex_missing_finance"]
    assert data["general_label"] == dv.TEMPLATE_DEFAULTS["request_general_label"]


def test_request_for_a_page_the_user_cannot_open_is_refused(request_setup):
    result = router.request_feature_submit(None, {"page_key": "kpi", "title": "T", "text": "x"}, user="jsmith")
    assert result["status"] == "error"
    assert request_setup["submitted"] == [] and request_setup["sent"] == []


def test_general_request_needs_no_page(request_setup):
    result = router.request_feature_submit(None, {"page_key": "", "title": "T", "text": "x"}, user="jsmith")
    assert result["status"] == "ok"
    assert request_setup["submitted"][0][1] is None


def test_request_sends_confirmation_to_the_user_and_alert_to_the_admin(request_setup):
    result = router.request_feature_submit(None, {"page_key": "apex_missing_finance", "title": "T", "text": "x"},
                                           user="jsmith")
    assert result["status"] == "ok" and result["id"] == 9
    to = {e["label"]: e["to"] for e in request_setup["sent"]}
    assert to == {"confirmation": "jo@x.com", "alert": ["tom@x.com"]}
    assert request_setup["recorded"] == [(9, True, True, "")]


def test_request_text_is_escaped_and_braces_left_alone(request_setup):
    router.request_feature_submit(None, {"page_key": "", "title": "T", "text": "x"}, user="jsmith")
    html = request_setup["sent"][0]["html"]
    assert "<b>faster</b>" not in html and "&lt;b&gt;faster&lt;/b&gt;" in html
    assert "Please {name} make it" in html


def test_one_email_failing_still_sends_the_other_and_keeps_the_request(request_setup):
    request_setup["fail"].add("alert")
    result = router.request_feature_submit(None, {"page_key": "", "title": "T", "text": "x"}, user="jsmith")
    assert result["status"] == "ok" and result["id"] == 9
    assert [e["label"] for e in request_setup["sent"]] == ["confirmation"]
    dev_id, confirmation, alert, error = request_setup["recorded"][0]
    assert (dev_id, confirmation, alert) == (9, True, False) and "Alert" in error


def test_failed_confirmation_is_told_to_the_user(request_setup):
    request_setup["fail"].add("confirmation")
    result = router.request_feature_submit(None, {"page_key": "", "title": "T", "text": "x"}, user="jsmith")
    assert result["status"] == "ok" and "could not be sent" in result["message"]


def test_repeat_submit_only_sends_what_has_not_gone(request_setup):
    request_setup["dev"] = request_dev(confirmation_sent_at="2026-11-06T14:05:00", alert_sent_at="")
    request_setup["repeat"] = True
    router.request_feature_submit(None, {"page_key": "", "title": "T", "text": "x"}, user="jsmith")
    assert [e["label"] for e in request_setup["sent"]] == ["alert"]


def test_resend_only_sends_the_missing_email(request_setup):
    request_setup["dev"] = request_dev(alert_sent_at="2026-11-06T14:05:00")
    result = router.developments_request_emails_resend(9, None, user=ADMIN)
    assert result["status"] == "ok"
    assert [e["label"] for e in request_setup["sent"]] == ["confirmation"]


def test_resend_when_both_went_sends_nothing(request_setup):
    request_setup["dev"] = request_dev(confirmation_sent_at="x", alert_sent_at="y")
    result = router.developments_request_emails_resend(9, None, user=ADMIN)
    assert result["status"] == "ok" and request_setup["sent"] == []


def test_request_needs_a_title_and_words():
    with pytest.raises(ValueError):
        dv.clean_request_title("  ")
    with pytest.raises(ValueError):
        dv.clean_request_text("")
    assert dv.clean_request_title(" Faster\nExport ") == "Faster Export"


def test_bad_alert_address_refused_in_the_template():
    with pytest.raises(ValueError):
        dv.clean_template({"request_alert_to": "not-an-address"})
    assert dv.clean_template({"request_alert_to": ""})["request_alert_to"] == ""


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
