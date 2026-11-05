"""The User Guide: its sections, who can see each one, and its settings (Settings >
User Guide, and the /user-guide page).

One copy of the data, in prod's database, reached from every environment with
get_prod_activity_connection (the same as utils/developments.py), so a section
written anywhere is the same section everywhere.

A new section is seen by the admin only until he changes its visibility.
"""
import logging
import re

from markupsafe import Markup, escape
from psycopg2.extras import Json

from utils.db_utils import get_prod_activity_connection
from utils.ddl_once import once_per_database

logger = logging.getLogger(__name__)

ONLY_ME, PAGE_ACCESS, EVERYONE, CHOSEN = "only_me", "page", "everyone", "users"
VISIBILITIES = {
    ONLY_ME: "Only Me",
    PAGE_ACCESS: "People With Access To The Page",
    EVERYONE: "Everyone Signed In",
    CHOSEN: "Chosen People",
}

_TITLE_MAX = 200
_BODY_MAX = 100000

SETTINGS_DEFAULTS = {
    "title": "User Guide",
    "font_size": "15",
    "nav_width": "260",
    "starter": "## What This Page Is For\n\nWrite what {page} is for here.\n\n"
               "## How To Use It\n\n1. First step\n2. Next step\n\n## Questions\n\n- Add common questions here",
}
_NUMBER_LIMITS = {"font_size": (11, 22), "nav_width": (180, 420)}

# The first section, added once when the table is first made, seen by the admin only.
_SEEDS = [{
    "title": "Trade Report",
    "page_key": "trade_report",
    "body": """The Trade Report shows every trade sale, with its costs and margin, by month, site and auction. It replaces the Trade Report LIVE Excel file.

## Choosing What You See

Everything at the top of the page changes the report straight away.

- **View** picks the layout: Vehicles (the list of vehicles), Month To Date or Year To Date. Month To Date splits by site or auction; Year To Date splits by site, auction or month.
- **Period** picks the month or financial year. Financial years run from October to September.
- **Invoice Date** shows sales invoiced between two dates. While either date is set it replaces the Period on every view. Press Clear to go back to the Period.
- **Site**, **Customer** and **Supplier Category** narrow the sales further. Any filter that is on is listed under the top bar, and Clear All Filters removes them all.

## What The Figures Mean

- **Units**: the number of vehicles sold.
- **Avg Purchase Price** and **Avg Sale Price**: averages excluding VAT, for vehicles with a CAP value.
- **Avg CAP Clean**: the average live CAP clean value.
- **Purchase % CAP Clean** and **Sell % CAP Clean**: the purchase and sale price as a percentage of CAP Clean.
- **Avg Gross Margin**: the average gross margin.
- **Avg Total Costs**: the average total costs.
- **Avg Net Margin**: the average of gross margin less total costs.
- **Total Net Margin**: all the net margins added together.

## Seeing The Vehicles Behind A Figure

Press **View** under a column to list the vehicles behind it. The CSV button in that list downloads them.

## Moving A Car To Another Site

If you have the Approve Site Changes permission, press **Site Changes**, enter the VRM, the new site and the reason, then save. This applies straight away for everyone, on this report only. Other reports keep the Autoview site.

## How Fresh The Data Is

The top of the page shows when the data was loaded. The figures are rebuilt once a day. Press **Refresh Data** to load the latest.
""",
}]

_COLUMNS = "id, title, body, page_key, visibility, chosen_users, position, updated_by, updated_at"


@once_per_database
def _ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('user_guide_sections')")
        is_new = cur.fetchone()[0] is None
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_guide_sections (
                id SERIAL PRIMARY KEY,
                title VARCHAR(200) NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                page_key VARCHAR(64),
                visibility VARCHAR(10) NOT NULL DEFAULT 'only_me',
                chosen_users JSONB NOT NULL DEFAULT '[]',
                position INTEGER NOT NULL DEFAULT 0,
                updated_by VARCHAR(100),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_guide_settings (
                key VARCHAR(60) PRIMARY KEY,
                value TEXT NOT NULL,
                updated_by VARCHAR(100),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")
        if is_new:
            for position, seed in enumerate(_SEEDS):
                cur.execute(
                    "INSERT INTO user_guide_sections (title, body, page_key, visibility, position, updated_by) "
                    "VALUES (%s, %s, %s, %s, %s, 'setup')",
                    (seed["title"], seed["body"], seed["page_key"], ONLY_ME, position))
    conn.commit()


# ── Who can see a section ────────────────────────────────────────────────────

def can_see(section: dict, user: str | None, is_admin, user_can) -> bool:
    """is_admin(user) and user_can(user, page_key) are passed in, as they live in
    routers/core.py, which utils must not import. Follows page access through
    user_can, so a hidden page or a removed grant takes the section with it."""
    if not user:
        return False
    if is_admin(user):
        return True
    visibility = section.get("visibility")
    if visibility == EVERYONE:
        return True
    if visibility == CHOSEN:
        return user in (section.get("chosen_users") or [])
    if visibility == PAGE_ACCESS:
        return bool(section.get("page_key")) and user_can(user, section["page_key"])
    return False


# ── Checks ───────────────────────────────────────────────────────────────────

def _text(value, limit: int) -> str:
    return str(value if value is not None else "").replace("\r\n", "\n").strip()[:limit]


def clean_section(raw: dict, page_keys, usernames) -> dict:
    """The checked values of one section. Raises ValueError with a message for the page."""
    raw = raw or {}
    title = _text(raw.get("title"), _TITLE_MAX)
    page_key = _text(raw.get("page_key"), 64) or None
    visibility = _text(raw.get("visibility"), 10) or ONLY_ME
    if not title:
        raise ValueError("Give the section a title.")
    if page_key and page_key not in page_keys:
        raise ValueError("That page does not exist.")
    if visibility not in VISIBILITIES:
        raise ValueError("Choose who can see this section.")
    if visibility == PAGE_ACCESS and not page_key:
        raise ValueError("Choose a page, or choose a different Who Can See.")
    raw_chosen = raw.get("chosen_users") or []
    if not isinstance(raw_chosen, list):
        raise ValueError("The chosen people could not be read. Reload the page.")
    chosen = sorted({str(u) for u in raw_chosen} & set(usernames))
    if visibility == CHOSEN and not chosen:
        raise ValueError("Tick at least one person, or choose a different Who Can See.")
    return {"title": title, "body": _text(raw.get("body"), _BODY_MAX), "page_key": page_key,
            "visibility": visibility, "chosen_users": chosen}


def clean_settings(raw: dict) -> dict:
    """Checked settings for saving. Raises ValueError on the first bad one."""
    raw = raw or {}
    cleaned = {}
    for key, default in SETTINGS_DEFAULTS.items():
        value = _text(raw.get(key, default), _BODY_MAX)
        if key in _NUMBER_LIMITS:
            low, high = _NUMBER_LIMITS[key]
            if not value.isdigit() or not low <= int(value) <= high:
                raise ValueError(f"{key.replace('_', ' ').title()} must be from {low} to {high}.")
        cleaned[key] = value
    cleaned["title"] = cleaned["title"][:60]
    if not cleaned["title"]:
        raise ValueError("Give the guide a name.")
    return cleaned


def settings_from_stored(stored: dict) -> dict:
    """Stored values, with the default for any that is missing or fails the checks.
    The numbers go into the page's CSS, so they are checked on the way out too."""
    result = dict(SETTINGS_DEFAULTS)
    for key in SETTINGS_DEFAULTS:
        value = stored.get(key)
        if value is None:
            continue
        if key in _NUMBER_LIMITS:
            low, high = _NUMBER_LIMITS[key]
            if not value.isdigit() or not low <= int(value) <= high:
                continue
        if value or key == "starter":
            result[key] = value
    return result


# ── Reading and saving ───────────────────────────────────────────────────────

def _section_row(row) -> dict:
    section = dict(zip([c.strip() for c in _COLUMNS.split(",")], row))
    section["chosen_users"] = section["chosen_users"] or []
    section["updated_at"] = section["updated_at"].isoformat() if section["updated_at"] else ""
    return section


def read_guide() -> tuple:
    """(every section in order, the settings), on one connection."""
    with get_prod_activity_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_COLUMNS} FROM user_guide_sections ORDER BY position, id")
            sections = [_section_row(r) for r in cur.fetchall()]
            cur.execute("SELECT key, value FROM user_guide_settings")
            return sections, settings_from_stored(dict(cur.fetchall()))


def save_section(raw: dict, user: str, page_keys, usernames) -> dict:
    cleaned = clean_section(raw, page_keys, usernames)
    values = (cleaned["title"], cleaned["body"], cleaned["page_key"], cleaned["visibility"],
              Json(cleaned["chosen_users"]), user)
    section_id = (raw or {}).get("id")
    with get_prod_activity_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            if section_id:
                cur.execute(
                    "UPDATE user_guide_sections SET title = %s, body = %s, page_key = %s, visibility = %s, "
                    f"chosen_users = %s, updated_by = %s, updated_at = NOW() WHERE id = %s RETURNING {_COLUMNS}",
                    (*values, int(section_id)))
            else:
                cur.execute(
                    "INSERT INTO user_guide_sections (title, body, page_key, visibility, chosen_users, updated_by, "
                    "position) VALUES (%s, %s, %s, %s, %s, %s, "
                    f"(SELECT COALESCE(MAX(position), -1) + 1 FROM user_guide_sections)) RETURNING {_COLUMNS}", values)
            row = cur.fetchone()
        conn.commit()
    if row is None:
        raise ValueError("That section no longer exists.")
    return _section_row(row)


def delete_section(section_id: int) -> None:
    with get_prod_activity_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_guide_sections WHERE id = %s", (section_id,))
        conn.commit()


def reorder_sections(ids: list) -> None:
    """Position in the list becomes the section's place in the left menu."""
    with get_prod_activity_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            for position, section_id in enumerate(ids):
                cur.execute("UPDATE user_guide_sections SET position = %s WHERE id = %s", (position, section_id))
        conn.commit()


def generate_sections(pages: list, user: str) -> list:
    """Add a section, from the starter text and seen by the admin only, for each
    (page key, page name) that has none yet. Returns the new sections' ids."""
    sections, settings = read_guide()
    covered = {s["page_key"] for s in sections}
    missing = [(key, name) for key, name in pages if key not in covered]
    added = []
    with get_prod_activity_connection() as conn:
        with conn.cursor() as cur:
            for key, name in missing:
                cur.execute(
                    "INSERT INTO user_guide_sections (title, body, page_key, visibility, updated_by, position) "
                    "VALUES (%s, %s, %s, %s, %s, "
                    "(SELECT COALESCE(MAX(position), -1) + 1 FROM user_guide_sections)) RETURNING id",
                    (name[:_TITLE_MAX], settings["starter"].replace("{page}", name), key, ONLY_ME, user))
                added.append(cur.fetchone()[0])
        conn.commit()
    return added


def save_settings(raw: dict, user: str) -> dict:
    cleaned = clean_settings(raw)
    with get_prod_activity_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            for key, value in cleaned.items():
                cur.execute(
                    "INSERT INTO user_guide_settings (key, value, updated_by) VALUES (%s, %s, %s) "
                    "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
                    "updated_by = EXCLUDED.updated_by, updated_at = NOW()",
                    (key, value, user))
        conn.commit()
    return cleaned


# ── Turning typed text into the page ─────────────────────────────────────────

# After escaping, so nothing typed can become HTML. Links are https, http or a
# path on this site only, so a saved link can never be javascript: or similar.
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+|/[^\s)/\\][^\s)]*)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _inline(text: str, base_url: str) -> str:
    def link(match):
        url = match.group(2)
        if url.startswith("/"):
            return f'<a href="{base_url}{url}">{match.group(1)}</a>'
        return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{match.group(1)}</a>'
    return _BOLD.sub(r"<strong>\1</strong>", _LINK.sub(link, str(escape(text))))


def render_body(text: str, base_url: str = "") -> Markup:
    """The section's typed text as HTML. Understands # ## ### headings, - bullets,
    1. numbered lists, > notes, **bold** and [text](link). Everything else is plain
    text: lines stay as typed and a blank line starts a new paragraph."""
    out, paragraph = [], []
    open_list = None

    def end_paragraph():
        if paragraph:
            out.append("<p>" + "<br>".join(paragraph) + "</p>")
            paragraph.clear()

    def end_list():
        nonlocal open_list
        if open_list:
            out.append(f"</{open_list}>")
            open_list = None

    def list_item(tag, content):
        nonlocal open_list
        end_paragraph()
        if open_list != tag:
            end_list()
            out.append(f"<{tag}>")
            open_list = tag
        out.append(f"<li>{_inline(content, base_url)}</li>")

    for raw_line in str(text or "").replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        heading = re.fullmatch(r"(#{1,3})\s+(.+)", line)
        bullet = re.fullmatch(r"[-*]\s+(.+)", line)
        number = re.fullmatch(r"\d+[.)]\s+(.+)", line)
        note = re.fullmatch(r">\s*(.+)", line)
        if bullet:
            list_item("ul", bullet.group(1))
        elif number:
            list_item("ol", number.group(1))
        else:
            end_list()
            if not line:
                end_paragraph()
            elif heading:
                end_paragraph()
                level = len(heading.group(1)) + 1
                out.append(f"<h{level}>{_inline(heading.group(2), base_url)}</h{level}>")
            elif note:
                end_paragraph()
                out.append(f'<div class="guide-note">{_inline(note.group(1), base_url)}</div>')
            else:
                paragraph.append(_inline(line, base_url))
    end_list()
    end_paragraph()
    return Markup("\n".join(out))
