"""The developments log and the update emails built from it (Settings >
Developments, and the What's New page everyone can read).

One copy of the data, in prod's database, reached from every environment with
get_prod_activity_connection, so a development added anywhere is the same
development everywhere. A new entry starts as a draft: only live entries appear
on What's New or can go in an email.

Emails are built here as HTML that Outlook desktop can draw (tables and inline
styles, see templates/emails/development_update.html) and sent through
utils/email_client.py from the page, after a confirmation box.
"""
import datetime
import logging
import os
import re
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup, escape
from psycopg2.extras import Json

from utils.db_utils import get_prod_activity_connection
from utils.ddl_once import once_per_database

logger = logging.getLogger(__name__)

STATUSES = ("draft", "live")
_TITLE_MAX = 200
_TYPE_MAX = 60
_TEXT_MAX = 20000
_FAQ_MAX = 50
_SUBJECT_MAX = 200
_EMAILS_LISTED = 50

# Everything about the email's wording and look that can be changed on the page.
TEMPLATE_DEFAULTS = {
    "site_url": "https://uk-intel-hub.aramis-group.eu",
    "default_send_to": "",
    "subject": "Intelligence Hub Update",
    "heading": "Intelligence Hub Update",
    "intro": "Hello all,\n\nHere is the latest on what has changed in the Intelligence Hub.",
    "sign_off": "Any questions, just reply to this email.\n\nThanks,\nTom",
    "footer": "More detail, guides and FAQs are on the What's New page of the Intelligence Hub.",
    "types": "New Module\nNew Report\nImprovement",
    "page_button_label": "Open {page}",
    "more_link_label": "Read More",
    "replaces_label": "Replaces",
    "no_access_text": "You do not have access to this page yet. Reply to the update email to ask for it.",
    "header_colour": "#1e3a5f",
    "header_text_colour": "#ffffff",
    "accent_colour": "#0d6efd",
    "button_colour": "#7ed321",
    "button_edge_colour": "#5f9e19",
    "button_text_colour": "#0d3d5f",
    "text_colour": "#343a40",
    "background_colour": "#f1f3f5",
    "font_size": "15",
    "heading_size": "24",
    "title_size": "18",
    "footer_size": "12",
    "email_width": "640",
}
_COLOUR_KEYS = {k for k in TEMPLATE_DEFAULTS if k.endswith("_colour")}
_NUMBER_LIMITS = {"font_size": (10, 24), "heading_size": (14, 40), "title_size": (12, 30), "footer_size": (9, 18),
                  "email_width": (480, 900)}
# Blank is allowed for these; every other wording box must hold something.
_MAY_BE_BLANK = {"sign_off", "footer", "intro", "replaces_label", "default_send_to"}
_TEMPLATE_TEXT_MAX = 2000

# The first entry, added once when the table is first made. A draft, for checking.
_SEEDS = [{
    "title": "Apex Missing Finance Now In The Website",
    "dev_type": "New Report",
    "page_key": "apex_missing_finance",
    "summary": "The Missing Apex Finance Excel file is no longer being used. "
               "Please use the Apex Missing Finance page in the Intelligence Hub instead.",
    "details": "The page lists orders whose finance commission or subsidy is missing in Apex.",
    "replaces": "Missing Apex Finance Excel file",
    "guide": "Open Apex Missing Finance from the menu. Use the filter buttons and the search box to "
             "narrow the list, Refresh Data to load the latest figures, and CSV to download the list.",
    "faqs": [{"question": "Do I still need the Excel file?",
              "answer": "No. Please use the page in the Intelligence Hub from now on."}],
}]

_COLUMNS = ("id, title, dev_type, page_key, summary, details, replaces, guide, faqs, "
            "released_on, status, updated_by, updated_at")


@once_per_database
def _ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('developments')")
        is_new = cur.fetchone()[0] is None
        cur.execute("""
            CREATE TABLE IF NOT EXISTS developments (
                id SERIAL PRIMARY KEY,
                title VARCHAR(200) NOT NULL,
                dev_type VARCHAR(60) NOT NULL,
                page_key VARCHAR(64),
                summary TEXT NOT NULL DEFAULT '',
                details TEXT NOT NULL DEFAULT '',
                replaces TEXT NOT NULL DEFAULT '',
                guide TEXT NOT NULL DEFAULT '',
                faqs JSONB NOT NULL DEFAULT '[]',
                released_on DATE,
                status VARCHAR(10) NOT NULL DEFAULT 'draft',
                updated_by VARCHAR(100),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS development_emails (
                id SERIAL PRIMARY KEY,
                subject VARCHAR(200) NOT NULL,
                intro TEXT NOT NULL DEFAULT '',
                development_ids INTEGER[] NOT NULL,
                development_titles JSONB NOT NULL DEFAULT '[]',
                html TEXT NOT NULL,
                plain_text TEXT NOT NULL,
                created_by VARCHAR(100),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS development_email_template (
                key VARCHAR(60) PRIMARY KEY,
                value TEXT NOT NULL,
                updated_by VARCHAR(100),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")
        if is_new:
            for seed in _SEEDS:
                cur.execute(
                    "INSERT INTO developments (title, dev_type, page_key, summary, details, replaces, guide, faqs, "
                    "status, updated_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'draft', 'setup')",
                    (seed["title"], seed["dev_type"], seed["page_key"], seed["summary"], seed["details"],
                     seed["replaces"], seed["guide"], Json(seed["faqs"])))
    conn.commit()


# ── Checks ───────────────────────────────────────────────────────────────────

def _text(value, limit: int = _TEXT_MAX) -> str:
    return str(value if value is not None else "").replace("\r\n", "\n").strip()[:limit]


def _clean_date(value) -> datetime.date | None:
    value = _text(value, 10)
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"'{value}' is not a valid release date.")


def clean_development(raw: dict, page_keys) -> dict:
    """The checked values of one development. Raises ValueError with a message
    for the page when something is wrong."""
    raw = raw or {}
    title = _text(raw.get("title"), _TITLE_MAX)
    dev_type = _text(raw.get("dev_type"), _TYPE_MAX)
    summary = _text(raw.get("summary"))
    status = _text(raw.get("status"), 10) or "draft"
    page_key = _text(raw.get("page_key"), 64) or None
    if not title:
        raise ValueError("Give the development a title.")
    if not dev_type:
        raise ValueError("Give the development a type.")
    if status not in STATUSES:
        raise ValueError("Status must be Draft or Live.")
    if status == "live" and not summary:
        raise ValueError("A live development needs a summary, as that is what the email shows.")
    if page_key and page_key not in page_keys:
        raise ValueError("That page does not exist.")
    faqs = []
    raw_faqs = raw.get("faqs") or []
    if not isinstance(raw_faqs, list):
        raise ValueError("FAQs must be a list.")
    for faq in raw_faqs[:_FAQ_MAX]:
        if not isinstance(faq, dict):
            raise ValueError("Each FAQ needs a question and an answer.")
        question, answer = _text(faq.get("question"), 500), _text(faq.get("answer"), 5000)
        if not question and not answer:
            continue
        if not question or not answer:
            raise ValueError("Each FAQ needs both a question and an answer.")
        faqs.append({"question": question, "answer": answer})
    return {
        "title": title, "dev_type": dev_type, "page_key": page_key, "summary": summary,
        "details": _text(raw.get("details")), "replaces": _text(raw.get("replaces"), _TITLE_MAX),
        "guide": _text(raw.get("guide")), "faqs": faqs,
        "released_on": _clean_date(raw.get("released_on")), "status": status,
    }


def clean_site_url(value) -> str | None:
    """The address email links are built from, without a trailing slash, or None
    if it is not a plain https address. Only https is accepted, so a saved value
    can never turn a link into javascript: or similar."""
    value = _text(value, 200).rstrip("/")
    if re.search(r"[\s\"'<>\\]", value):
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
        return None
    return value


def _template_value_error(key: str, value: str) -> str | None:
    if key == "site_url":
        return None if clean_site_url(value) else "Website Address must be a plain https address."
    if key in _COLOUR_KEYS:
        return None if re.fullmatch(r"#[0-9a-fA-F]{6}", value) else f"{key} must be a colour like #1e3a5f."
    if key in _NUMBER_LIMITS:
        low, high = _NUMBER_LIMITS[key]
        return None if value.isdigit() and low <= int(value) <= high else f"{key} must be from {low} to {high}."
    if not value and key not in _MAY_BE_BLANK:
        return f"{key} cannot be blank."
    return None


def clean_template(raw: dict) -> dict:
    """Checked template values for saving. Raises ValueError on the first bad one."""
    raw = raw or {}
    cleaned = {}
    for key, default in TEMPLATE_DEFAULTS.items():
        value = _text(raw.get(key, default), _TEMPLATE_TEXT_MAX)
        error = _template_value_error(key, value)
        if error:
            raise ValueError(error)
        cleaned[key] = clean_site_url(value) if key == "site_url" else value
    return cleaned


def template_from_stored(stored: dict) -> dict:
    """The template to use: stored values, with the default for any that is
    missing or does not pass the checks. Values go into colours and links in the
    email, so they are checked on the way out too."""
    result = dict(TEMPLATE_DEFAULTS)
    for key in TEMPLATE_DEFAULTS:
        value = stored.get(key)
        if value is not None and not _template_value_error(key, value):
            result[key] = value
    return result


def type_list(template: dict, developments: list | None = None) -> list:
    """The type names: the template's list in order, then any others in use."""
    names = [line.strip() for line in template.get("types", "").splitlines() if line.strip()]
    for dev in developments or []:
        if dev["dev_type"] not in names:
            names.append(dev["dev_type"])
    return names


# ── Developments ─────────────────────────────────────────────────────────────

def released_label(value) -> str:
    return f"{value.day} {value:%B %Y}" if value else ""


def _uk_day_label(when) -> str:
    """A stored timestamp as its day in the UK, so a late email shows the right day."""
    return released_label(when.astimezone(ZoneInfo("Europe/London"))) if when else ""


def _development_row(row) -> dict:
    dev = dict(zip([c.strip() for c in _COLUMNS.split(",")], row))
    dev["released_label"] = released_label(dev["released_on"])
    dev["released_on"] = dev["released_on"].isoformat() if dev["released_on"] else ""
    dev["updated_at"] = dev["updated_at"].isoformat() if dev["updated_at"] else ""
    dev["faqs"] = dev["faqs"] or []
    return dev


def list_developments(live_only: bool = False) -> list:
    """Newest first. Each also says when it last went in an email."""
    where = "WHERE status = 'live'" if live_only else ""
    with get_prod_activity_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_COLUMNS} FROM developments {where} "
                        "ORDER BY released_on DESC NULLS LAST, id DESC")
            developments = [_development_row(r) for r in cur.fetchall()]
            cur.execute("SELECT unnest(development_ids), MAX(created_at) FROM development_emails GROUP BY 1")
            last_emailed = dict(cur.fetchall())
    for dev in developments:
        dev["last_emailed"] = _uk_day_label(last_emailed.get(dev["id"]))
    return developments


def get_developments(ids: list) -> list:
    """These developments, in the same order as the list on the page."""
    return [d for d in list_developments() if d["id"] in set(ids)]


def save_development(raw: dict, user: str, page_keys) -> dict:
    cleaned = clean_development(raw, page_keys)
    values = (cleaned["title"], cleaned["dev_type"], cleaned["page_key"], cleaned["summary"], cleaned["details"],
              cleaned["replaces"], cleaned["guide"], Json(cleaned["faqs"]), cleaned["released_on"],
              cleaned["status"], user)
    dev_id = (raw or {}).get("id")
    with get_prod_activity_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            if dev_id:
                cur.execute(
                    "UPDATE developments SET title = %s, dev_type = %s, page_key = %s, summary = %s, details = %s, "
                    "replaces = %s, guide = %s, faqs = %s, released_on = %s, status = %s, updated_by = %s, "
                    f"updated_at = NOW() WHERE id = %s RETURNING {_COLUMNS}", (*values, int(dev_id)))
            else:
                cur.execute(
                    "INSERT INTO developments (title, dev_type, page_key, summary, details, replaces, guide, faqs, "
                    "released_on, status, updated_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    f"RETURNING {_COLUMNS}", values)
            row = cur.fetchone()
        conn.commit()
    if row is None:
        raise ValueError("That development no longer exists.")
    return _development_row(row)


def delete_development(dev_id: int) -> None:
    """Past emails keep their own copy of the text, so they are not affected."""
    with get_prod_activity_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM developments WHERE id = %s", (dev_id,))
        conn.commit()


# ── Email template ───────────────────────────────────────────────────────────

def get_template() -> dict:
    with get_prod_activity_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM development_email_template")
            return template_from_stored(dict(cur.fetchall()))


def save_template(raw: dict, user: str) -> dict:
    cleaned = clean_template(raw)
    with get_prod_activity_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            for key, value in cleaned.items():
                cur.execute(
                    "INSERT INTO development_email_template (key, value, updated_by) VALUES (%s, %s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                    "updated_by = EXCLUDED.updated_by, updated_at = NOW()",
                    (key, value, user))
        conn.commit()
    return cleaned


# ── Building the email ───────────────────────────────────────────────────────

def _lines(text) -> Markup:
    """Escaped text with its line breaks kept."""
    return escape(text or "").replace("\n", Markup("<br>"))


_env = Environment(
    loader=FileSystemLoader(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")),
    autoescape=True,
)
_env.filters["lines"] = _lines


def _email_entry(dev: dict, template: dict, pages: dict) -> dict:
    site = clean_site_url(template["site_url"]) or TEMPLATE_DEFAULTS["site_url"]
    page = pages.get(dev.get("page_key"))
    has_more = bool(dev.get("details") or dev.get("guide") or dev.get("faqs"))
    return {
        "title": dev["title"], "summary": dev.get("summary", ""), "replaces": dev.get("replaces", ""),
        "released_label": dev.get("released_label", ""),
        "page_url": f"{site}{page['path']}" if page else "",
        "page_label": template["page_button_label"].replace("{page}", page["name"]) if page else "",
        "more_url": f"{site}/whats-new#dev-{dev['id']}" if has_more else "",
    }


def _grouped(developments: list, template: dict, pages: dict) -> list:
    """[(type name, [entries])], in the template's order of types."""
    return [(name, [_email_entry(d, template, pages) for d in developments if d["dev_type"] == name])
            for name in type_list(template, developments)
            if any(d["dev_type"] == name for d in developments)]


def _plain_text(subject: str, intro: str, groups: list, template: dict) -> str:
    lines = [subject, "", intro, ""] if intro else [subject, ""]
    for name, entries in groups:
        lines += [name, "-" * len(name)]
        for entry in entries:
            lines.append(entry["title"])
            if entry["summary"]:
                lines.append(entry["summary"])
            if entry["replaces"]:
                lines.append(f"{template['replaces_label']}: {entry['replaces']}")
            if entry["page_url"]:
                lines.append(f"{entry['page_label']}: {entry['page_url']}")
            if entry["more_url"]:
                lines.append(f"{template['more_link_label']}: {entry['more_url']}")
            lines.append("")
    if template["sign_off"]:
        lines += [template["sign_off"], ""]
    if template["footer"]:
        lines.append(template["footer"])
    return "\n".join(lines).strip() + "\n"


def build_email(developments: list, subject: str, intro: str, template: dict, pages: dict) -> dict:
    """The email as {'html', 'text'}. pages is {page key: {'name', 'path'}}."""
    groups = _grouped(developments, template, pages)
    html = _env.get_template("emails/development_update.html").render(
        subject=subject, intro=intro, groups=groups, t=template)
    return {"html": html, "text": _plain_text(subject, intro, groups, template)}


def clean_subject(value) -> str:
    """One line: a line break in an email header would start a new header."""
    subject = " ".join(_text(value, _SUBJECT_MAX).split())
    if not subject:
        raise ValueError("Give the email a subject.")
    return subject


def email_problem(ids: list, developments: list) -> str | None:
    """Why these cannot go in an email, or None. Drafts are refused because the
    email would link to a What's New entry nobody can see yet."""
    if not ids:
        return "Tick at least one development."
    if len(developments) != len(set(ids)):
        return "One of the ticked developments no longer exists. Reload the page."
    drafts = [d["title"] for d in developments if d["status"] != "live"]
    if drafts:
        return "Set these to Live first: " + ", ".join(drafts)
    return None


def clean_recipients(value) -> list:
    """The addresses in a send-to box, each once. Semicolons, commas or spaces
    may separate them, so a list pasted from Outlook or the Users page works."""
    recipients, seen = [], set()
    for address in re.split(r"[;,\s]+", str(value or "").strip()):
        if not address or address.lower() in seen:
            continue
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
            raise ValueError(f"This is not an email address: {address}")
        seen.add(address.lower())
        recipients.append(address)
    if not recipients:
        raise ValueError("Enter at least one address to send to.")
    return recipients


# ── Past emails ──────────────────────────────────────────────────────────────

def record_email(subject: str, intro: str, developments: list, built: dict, user: str) -> int:
    """Keep a copy of the email. Pressing the buttons twice on the same email
    gives one record, not two."""
    with get_prod_activity_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT id, subject, html FROM development_emails ORDER BY id DESC LIMIT 1")
            latest = cur.fetchone()
            if latest and latest[1] == subject and latest[2] == built["html"]:
                return latest[0]
            cur.execute(
                "INSERT INTO development_emails (subject, intro, development_ids, development_titles, html, "
                "plain_text, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (subject, intro, [d["id"] for d in developments], Json([d["title"] for d in developments]),
                 built["html"], built["text"], user))
            email_id = cur.fetchone()[0]
        conn.commit()
    return email_id


def list_emails() -> list:
    with get_prod_activity_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT id, subject, development_titles, created_by, created_at FROM development_emails "
                        "ORDER BY id DESC LIMIT %s", (_EMAILS_LISTED,))
            rows = cur.fetchall()
    return [{"id": r[0], "subject": r[1], "titles": r[2] or [], "created_by": r[3],
             "created_label": _uk_day_label(r[4])} for r in rows]


def get_email(email_id: int) -> dict | None:
    with get_prod_activity_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT subject, html, plain_text FROM development_emails WHERE id = %s", (email_id,))
            row = cur.fetchone()
    return {"subject": row[0], "html": row[1], "text": row[2]} if row else None
