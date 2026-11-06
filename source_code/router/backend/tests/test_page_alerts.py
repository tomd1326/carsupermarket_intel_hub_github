"""Page alerts (utils/page_alerts.py): the checks on a saved alert and the email it builds.

The risks covered: a bad alert being saved, typed text being read as HTML, cells shown
differently from the page, the preview table not saying it was cut short.

No test touches a real database or sends an email.

Run from source_code/router/backend:
    poetry run pytest tests/test_page_alerts.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from utils import page_alerts as pa

USERS = {'tdrayson', 'someone'}


def _cfg(**over):
    return {**pa.default_config('tdrayson'), **over}


def test_default_is_valid():
    assert pa.validate(_cfg(), USERS) is None


def test_saved_config_falls_back_to_default():
    assert pa.saved_config(None, 'tdrayson') == pa.default_config('tdrayson')
    assert pa.saved_config({'subject': 'S'}, 'tdrayson')['subject'] == 'S'


def test_validate_rejects_bad_alerts():
    assert pa.validate(_cfg(users=['nobody']), USERS)
    assert pa.validate(_cfg(subject='  '), USERS)
    assert pa.validate(_cfg(blocks=[]), USERS)
    assert pa.validate(_cfg(blocks=[{'type': 'image'}]), USERS)
    assert pa.validate(_cfg(blocks=[{'type': 'table', 'max_rows': 0}]), USERS)
    assert pa.validate(_cfg(blocks=[{'type': 'table', 'max_rows': True}]), USERS)
    assert pa.validate(_cfg(blocks=[{'type': 'text', 'text': ''}]), USERS)
    assert pa.validate({**_cfg(), 'extra': 1}, USERS)
    assert pa.validate(_cfg(style={'font_size': 7}), USERS)
    assert pa.validate(_cfg(style={'table_font_size': 25}), USERS)
    assert pa.validate(_cfg(style={'font_size': True}), USERS)
    assert pa.validate(_cfg(style={'accent_colour': 'red'}), USERS)
    assert pa.validate(_cfg(style={'font': 'Comic Sans'}), USERS)
    assert pa.validate(_cfg(style={'nonsense': 1}), USERS)
    assert pa.validate(_cfg(style={'accent_colour': '#112233', 'banner': False}), USERS) is None


def test_style_global_and_own():
    assert pa.validate_style(pa.default_style(), whole=True) is None
    assert pa.validate_style({'font': 'Arial'}, whole=True)
    assert pa.saved_style(None) == pa.default_style()
    assert pa.saved_style({'font_size': 99, 'footer': 'X'})['font_size'] == 14
    assert pa.saved_style({'footer': 'X'})['footer'] == 'X'
    eff = pa.effective_style(pa.default_style(), {'accent_colour': '#112233'})
    assert eff['accent_colour'] == '#112233' and eff['font'] == 'DM Sans'


def test_fill_leaves_unknown_braces():
    assert pa.fill('{count} rows {other}', {'count': '3'}) == '3 rows {other}'


def test_cells_match_the_page():
    assert pa.cell_html({'format': 'gbp', 'decimals': 2}, -1234.5) == '-£1,234.50'
    assert pa.cell_html({'format': 'number', 'decimals': 1}, 7) == '7.0'
    assert pa.cell_html({'format': 'pct', 'decimals': 0}, 0.25) == '25%'
    assert pa.cell_html({'format': 'date'}, '2026-11-06T09:00:00') == '06/11/2026'
    assert pa.cell_html({'format': 'status'}, 'STATUS_PAID') == 'Paid'
    assert pa.cell_html({'format': 'text'}, None) == '—'
    assert pa.cell_html({'format': 'link', 'label': 'Order'}, 'javascript:alert(1)') == ''
    assert 'href="https://x/1"' in pa.cell_html({'format': 'link', 'label': 'Order'}, 'https://x/1')


def test_colour_rules_match_the_page():
    rules = [{'column': 'APR', 'operator': 'equals', 'value': '0', 'colour': '#111111'},
             {'column': 'ISSUE', 'operator': 'is_not_blank', 'value': '', 'colour': '#222222'}]
    assert pa.cell_colour(rules, 'APR', 0.0) == '#111111'
    assert pa.cell_colour(rules, 'APR', 1.5) is None
    assert pa.cell_colour(rules, 'ISSUE', 'x') == '#222222'
    assert pa.cell_colour(rules, 'ISSUE', '') is None


def test_email_escapes_text_and_notes_cut_rows():
    cfg = _cfg(subject='{page}: {count}', blocks=[{'type': 'text', 'text': '<b>{count}</b>\nbye'},
                                                    {'type': 'table', 'max_rows': 1},
                                                    {'type': 'link', 'text': 'Open {page}'}])
    cols = [{'column': 'VRM', 'label': 'VRM', 'format': 'text'}]
    style = pa.effective_style(pa.default_style(), {'accent_colour': '#112233'})
    subject, body = pa.build_email(cfg, style, {'count': '2', 'page': 'AMF'}, 'https://site/page', cols, {'VRM': 0},
                                   [['AB12CDE'], ['<script>']], [])
    assert subject == 'AMF: 2'
    assert '&lt;b&gt;2&lt;/b&gt;<br>bye' in body
    assert 'AB12CDE' in body and '<script>' not in body
    assert 'first 1 of 2 rows' in body
    assert 'href="https://site/page"' in body and 'Open AMF' in body
    assert "'DM Sans'" in body and 'font-size:14px' in body and 'font-size:12px' in body
    assert 'background:#112233' in body and 'border-bottom:4px solid #d4ee9f' in body and 'Sent from Intelligence Hub' in body
    assert pa.build_email(cfg, {**style, 'banner': False, 'footer': ''}, {'page': 'AMF'}, 'u', cols, {'VRM': 0}, [], [])[1].count('border-bottom:4px') == 0
