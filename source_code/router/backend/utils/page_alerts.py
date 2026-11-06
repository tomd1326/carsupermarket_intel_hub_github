"""Alerts set up in a page's own Settings popup and sent by hand from the page.

Each page keeps one alert in kpi_layout under 'page_alert_<page key>': who it goes to
(users with an email on Settings > User Accounts), the subject, the email itself as an
ordered list of blocks, and 'style': the style fields this page sets for itself. A block is
typed text, a preview table of the page's rows (its own columns, formats and cell colours),
or a button linking to the page. Text and subject can hold {count}, {date}, {page} and
{last_built}.

How the email looks (font, sizes, colours, heading bar, footer) is one global style kept in
kpi_layout under STYLE_KEY and edited on Settings > Email Alerts. A page's own 'style' holds
only the fields it overrides; everything else follows the global style.

The page's router supplies the rows and its column settings; this file checks the settings
and builds the email HTML. Inline styles and plain tables only, as Outlook ignores style
sheets.
"""
from __future__ import annotations

import datetime
import html
import re

KEY_PREFIX = 'page_alert_'
STYLE_KEY = 'alert_email_style'
BLOCK_TYPES = ('text', 'table', 'link')
PLACEHOLDERS = ('count', 'date', 'page', 'last_built')
MAX_BLOCKS = 20
MAX_TEXT = 4000
MAX_SUBJECT = 200
MAX_FOOTER = 200
MAX_TABLE_ROWS = 500
MIN_SIZE, MAX_SIZE = 8, 24

# Email programs (Outlook, Gmail) cannot load web fonts, so DM Sans only shows where it is
# installed on the reader's computer; Arial is what everyone else sees.
FONTS = {
    'DM Sans': "'DM Sans',Arial,Helvetica,sans-serif",
    'Arial': "Arial,Helvetica,sans-serif",
    'Calibri': "Calibri,Arial,sans-serif",
    'Verdana': "Verdana,Arial,sans-serif",
    'Georgia': "Georgia,'Times New Roman',serif",
}

# key -> (label, kind, default). The pages build their style editors from this list, so a
# new field only needs a line here and a use in build_email.
STYLE_FIELDS = {
    'font': ('Font', 'font', 'DM Sans'),
    'font_size': ('Text And Button Size (Pixels)', 'size', 14),
    'table_font_size': ('Preview Table Size (Pixels)', 'size', 12),
    'text_colour': ('Text Colour', 'colour', '#343a40'),
    'background_colour': ('Background Colour', 'colour', '#ffffff'),
    'banner': ('Show Heading Bar With The Page Name', 'bool', True),
    'accent_colour': ('Heading Bar Colour', 'colour', '#dbeafe'),
    'accent_text_colour': ('Heading Bar Text, Button Text And Link Colour', 'colour', '#1e3a5f'),
    'highlight_colour': ('Heading Bar Stripe And Button Colour', 'colour', '#d4ee9f'),
    'table_header_colour': ('Table Heading Colour', 'colour', '#dbeafe'),
    'table_header_text_colour': ('Table Heading Text Colour', 'colour', '#1e3a5f'),
    'table_border_colour': ('Table Border Colour', 'colour', '#b0b0b0'),
    'footer': ('Footer Text (Empty: None)', 'text', 'Sent from Intelligence Hub'),
}

_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")


# ── Style ─────────────────────────────────────────────────────────────────────

def default_style() -> dict:
    return {k: d for k, (_, _, d) in STYLE_FIELDS.items()}


def style_meta() -> dict:
    """What the style editors need to draw their rows."""
    return {'fields': [{'key': k, 'label': l, 'kind': kind, 'default': d} for k, (l, kind, d) in STYLE_FIELDS.items()],
            'fonts': list(FONTS)}


def _check_style_value(key: str, value) -> str | None:
    label, kind, _ = STYLE_FIELDS[key]
    if kind == 'colour' and not (isinstance(value, str) and _COLOUR.fullmatch(value)):
        return f"{label} must be a colour"
    if kind == 'size' and (not isinstance(value, int) or isinstance(value, bool) or not MIN_SIZE <= value <= MAX_SIZE):
        return f"{label} must be a whole number from {MIN_SIZE} to {MAX_SIZE}"
    if kind == 'font' and value not in FONTS:
        return f"{label} must be one of the fonts listed"
    if kind == 'bool' and not isinstance(value, bool):
        return f"{label} must be on or off"
    if kind == 'text' and (not isinstance(value, str) or len(value) > MAX_FOOTER):
        return f"{label} must be text of at most {MAX_FOOTER} characters"
    return None


def validate_style(style, whole: bool) -> str | None:
    """whole=True: the global style, every field present. whole=False: a page's own fields."""
    if not isinstance(style, dict) or not set(style) <= set(STYLE_FIELDS) or (whole and set(style) != set(STYLE_FIELDS)):
        return "Bad style settings"
    for key, value in style.items():
        err = _check_style_value(key, value)
        if err:
            return err
    return None


def saved_style(stored) -> dict:
    """The global style: defaults, then whatever valid fields are saved."""
    out = default_style()
    if isinstance(stored, dict):
        out.update({k: v for k, v in stored.items() if k in out and _check_style_value(k, v) is None})
    return out


def effective_style(global_style: dict, own: dict) -> dict:
    return {**global_style, **{k: v for k, v in (own or {}).items() if k in STYLE_FIELDS}}


# ── The alert's own settings ──────────────────────────────────────────────────

def default_config(admin_username: str) -> dict:
    return {
        'users': [admin_username],
        'subject': '{page}: {count} Rows',
        'style': {},
        'blocks': [
            {'type': 'text', 'text': 'Hi,\n\n{page} has {count} rows (report last built {last_built}).'},
            {'type': 'table', 'max_rows': 50},
            {'type': 'link', 'text': 'Open {page}'},
        ],
    }


def saved_config(stored, admin_username: str) -> dict:
    """The stored alert, or the default when nothing is saved yet."""
    base = default_config(admin_username)
    if not isinstance(stored, dict):
        return base
    return {**base, **{k: stored[k] for k in base if k in stored}}


def validate(cfg, known_users: set) -> str | None:
    """None when the alert can be saved, else what is wrong with it."""
    if not isinstance(cfg, dict) or set(cfg) != {'users', 'subject', 'blocks', 'style'}:
        return "Bad alert settings"
    users, subject, blocks = cfg['users'], cfg['subject'], cfg['blocks']
    err = validate_style(cfg['style'], whole=False)
    if err:
        return err
    if not isinstance(users, list) or any(u not in known_users for u in users):
        return "An alert recipient is not a user on Settings > User Accounts"
    if not isinstance(subject, str) or not subject.strip() or len(subject) > MAX_SUBJECT:
        return f"Enter a subject of at most {MAX_SUBJECT} characters"
    if not isinstance(blocks, list) or not blocks or len(blocks) > MAX_BLOCKS:
        return f"The email needs 1 to {MAX_BLOCKS} parts"
    for b in blocks:
        if not isinstance(b, dict) or b.get('type') not in BLOCK_TYPES:
            return "Bad email part"
        if b['type'] == 'table':
            if set(b) != {'type', 'max_rows'} or not isinstance(b['max_rows'], int) or isinstance(b['max_rows'], bool) \
                    or not 1 <= b['max_rows'] <= MAX_TABLE_ROWS:
                return f"Preview table rows must be a whole number from 1 to {MAX_TABLE_ROWS}"
        else:
            limit = MAX_TEXT if b['type'] == 'text' else 100
            if set(b) != {'type', 'text'} or not isinstance(b['text'], str) or not b['text'].strip() \
                    or len(b['text']) > limit:
                return f"Each {'text' if b['type'] == 'text' else 'link button'} part needs text of at most {limit} characters"
    return None


def fill(text: str, values: dict) -> str:
    """Swap {count} etc. for their values; anything else in braces is left as typed."""
    return re.sub(r"\{(\w+)\}", lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)


def uk_datetime_text(value) -> str:
    """'2026-11-06T09:15:00' -> '06/11/2026 09:15'."""
    if not value:
        return 'unknown'
    try:
        d = datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return d.strftime('%d/%m/%Y %H:%M')


# ── Cells, formatted as the page shows them ───────────────────────────────────

def _number(v):
    if v is None or v == '' or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def status_text(v) -> str:
    """STATUS_PAID -> Paid, STATUS_ACKNOWLEDGED -> Acknowledged."""
    if v is None or v == '':
        return '—'
    return ' '.join(w.capitalize() for w in re.sub(r'^STATUS_', '', str(v)).lower().split('_') if w)


def cell_html(col: dict, v, link_colour: str = '#0d6efd') -> str:
    dp = col.get('decimals') if isinstance(col.get('decimals'), int) else 0
    fmt = col.get('format')
    n = _number(v)
    if fmt == 'gbp':
        return '—' if n is None else ('-£' if n < 0 else '£') + f"{abs(n):,.{dp}f}"
    if fmt == 'number':
        return '—' if n is None else f"{n:,.{dp}f}"
    if fmt == 'pct':
        return '—' if n is None else f"{n * 100:.{dp}f}%"
    if fmt == 'date':
        return '/'.join(reversed(str(v)[:10].split('-'))) if v else '—'
    if fmt == 'status':
        return html.escape(status_text(v))
    if fmt == 'link':
        if v and re.match(r'^https?://', str(v), re.I):
            return (f'<a href="{html.escape(str(v), quote=True)}" style="color:{link_colour}">'
                    f'{html.escape(col.get("label") or "Open")}</a>')
        return ''
    return '—' if v is None or v == '' else html.escape(str(v))


def _same_value(v, rule_value: str) -> bool:
    if v is None:
        return False
    if isinstance(v, (int, float)) and not isinstance(v, bool) and _number(rule_value.strip() or None) is not None:
        return float(v) == float(rule_value)
    return str(v) == rule_value


def _rule_matches(rule: dict, v) -> bool:
    blank = v is None or v == ''
    op, value = rule.get('operator'), rule.get('value') or ''
    if op == 'is_blank':
        return blank
    if op == 'is_not_blank':
        return not blank
    if op == 'contains':
        return not blank and value.lower() in str(v).lower()
    if op in ('less_than', 'greater_than'):
        a, b = _number(v), _number(value)
        if blank or a is None or b is None:
            return False
        return a < b if op == 'less_than' else a > b
    if op == 'not_equals':
        return not _same_value(v, value)
    return _same_value(v, value)


def cell_colour(rules: list, column: str, v) -> str | None:
    """The first matching rule's colour, as on the page."""
    return next((r['colour'] for r in rules if r.get('column') == column and _rule_matches(r, v)), None)


# ── The email ─────────────────────────────────────────────────────────────────

def _table_html(columns: list, col_idx: dict, rows: list, rules: list, max_rows: int, s: dict) -> str:
    font, size, border = f"font-family:{FONTS[s['font']]};", s['table_font_size'], f"1px solid {s['table_border_colour']}"
    right = {'gbp', 'number', 'pct'}

    def align(c):
        return 'right' if c.get('format') in right else 'center' if c.get('format') == 'link' else 'left'

    head = ''.join(
        f'<th style="{font}font-size:{size}px;background:{s["table_header_colour"]};color:{s["table_header_text_colour"]};'
        f'border:{border};padding:4px 6px;text-align:{align(c)}">'
        f'{html.escape(c.get("label") or c["column"])}</th>' for c in columns)
    body = []
    for r in rows[:max_rows]:
        cells = []
        for c in columns:
            v = r[col_idx[c['column']]]
            colour = cell_colour(rules, c['column'], v)
            cells.append(f'<td style="{font}font-size:{size}px;color:{s["text_colour"]};border:{border};padding:3px 6px;'
                         f'white-space:nowrap;text-align:{align(c)};{f"background:{colour};" if colour else ""}">'
                         f'{cell_html(c, v, s["accent_text_colour"])}</td>')
        body.append('<tr>' + ''.join(cells) + '</tr>')
    if not rows:
        return f'<p style="{font}font-size:{size}px;color:#64748b">No rows.</p>'
    note = ''
    if len(rows) > max_rows:
        note = (f'<p style="{font}font-size:{size}px;color:#64748b;margin:4px 0 0 0">Showing the first {max_rows:,} '
                f'of {len(rows):,} rows. Open the page for the full list.</p>')
    return (f'<table cellspacing="0" cellpadding="0" style="border-collapse:collapse">'
            f'<thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>{note}')


def _link_html(text: str, url: str, s: dict) -> str:
    return (f'<table cellspacing="0" cellpadding="0"><tr><td style="background:{s["highlight_colour"]};'
            f'border:1px solid {s["accent_text_colour"]};border-radius:4px;padding:9px 18px">'
            f'<a href="{html.escape(url, quote=True)}" style="font-family:{FONTS[s["font"]]};font-size:{s["font_size"]}px;'
            f'color:{s["accent_text_colour"]};text-decoration:none;font-weight:bold">{html.escape(text)}</a></td></tr></table>')


def build_email(cfg: dict, style: dict, values: dict, page_url: str, columns: list, col_idx: dict,
                rows: list, rules: list) -> tuple[str, str]:
    """(subject, body HTML). style is the finished style (global plus this page's own),
    columns are the page's saved ones still in the table, rows already sorted as the page
    sorts them, col_idx maps a column name to its place in a row."""
    s = style
    font = f"font-family:{FONTS[s['font']]};"
    subject = fill(cfg['subject'], values).replace('\n', ' ').strip()
    parts = []
    for b in cfg['blocks']:
        if b['type'] == 'text':
            text = html.escape(fill(b['text'], values).replace('\r\n', '\n')).replace('\n', '<br>')
            parts.append(f'<p style="{font}font-size:{s["font_size"]}px;color:{s["text_colour"]};margin:0 0 12px 0">{text}</p>')
        elif b['type'] == 'table':
            parts.append(f'<div style="margin:0 0 12px 0">{_table_html(columns, col_idx, rows, rules, b["max_rows"], s)}</div>')
        else:
            parts.append(f'<div style="margin:0 0 12px 0">{_link_html(fill(b["text"], values), page_url, s)}</div>')
    # Tables, not divs: Outlook and some web mail drop padding and backgrounds on a div.
    def row(inner: str, style: str = '') -> str:
        return f'<tr><td style="{style}">{inner}</td></tr>'

    rows_html = []
    if s['banner']:
        rows_html.append(row(html.escape(str(values.get("page", ""))),
                             f'{font}background:{s["accent_colour"]};color:{s["accent_text_colour"]};'
                             f'font-size:{s["font_size"] + 6}px;font-weight:bold;padding:14px 20px;'
                             f'border-bottom:4px solid {s["highlight_colour"]}'))
    rows_html.append(row("".join(parts), 'padding:20px 20px 8px 20px'))
    if s['footer'].strip():
        rows_html.append(row(html.escape(fill(s["footer"], values)),
                             f'{font}font-size:{max(MIN_SIZE, s["font_size"] - 2)}px;color:#64748b;'
                             f'padding:8px 20px 16px 20px;border-top:1px solid {s["table_border_colour"]}'))
    return subject, (f'<table width="100%" cellspacing="0" cellpadding="0" '
                     f'style="{font}background:{s["background_colour"]};color:{s["text_colour"]}">'
                     f'{"".join(rows_html)}</table>')
