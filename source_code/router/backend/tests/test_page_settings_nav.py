"""Settings > Page Settings: each entry opens its section under the page's own
name, the nav lists the pages only while one is open, and only the admin can
change the Google Ads Campaigns list.

Run from source_code/router/backend:
    poetry run pytest tests/test_page_settings_nav.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import pytest
from jinja2 import Environment, FileSystemLoader

from routers import kpi, settings
from routers.page_registry import ALL_PAGE_KEYS

_TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'templates')


def _nav(active_section):
    env = Environment(loader=FileSystemLoader(_TEMPLATES), autoescape=True)
    env.globals['page_settings_sections'] = settings.PAGE_SETTINGS_SECTIONS
    env.globals['page_name'] = lambda key: f"Name of {key}"
    return env.get_template('_settings_nav.html').render(
        BASE_URL="", sections=settings.SETTINGS_SECTIONS, active_section=active_section)


def test_every_entry_names_a_real_page():
    assert {key for _, key in settings.PAGE_SETTINGS_SECTIONS} <= ALL_PAGE_KEYS


def test_no_section_is_listed_twice():
    page_slugs = {slug for slug, _ in settings.PAGE_SETTINGS_SECTIONS}
    assert not page_slugs & {s[0] for s in settings.SETTINGS_SECTIONS}


def test_page_section_is_titled_with_the_page_name(monkeypatch):
    monkeypatch.setattr(settings.core, "page_name", lambda key: f"Name of {key}")
    assert settings._section_title("supplier-evaluator") == "Name of tasks"


def test_other_section_keeps_its_own_title():
    assert settings._section_title("prod-to-dev") == "Copy Prod To Dev"


def test_unknown_section_has_no_title():
    assert settings._section_title("nothing-here") is None


def test_nav_lists_the_pages_while_one_is_open():
    html = _nav("mot-check")
    assert "Page Settings" in html
    for _, key in settings.PAGE_SETTINGS_SECTIONS:
        assert f"Name of {key}" in html


def test_nav_hides_the_pages_in_any_other_section():
    html = _nav("email")
    assert "Page Settings" in html
    assert "Name of" not in html


class _Request:
    async def json(self):
        raise AssertionError("A refused request must not get as far as reading the body")


@pytest.mark.parametrize("save", [kpi.kpi_ga4_campaigns_add, kpi.kpi_ga4_campaigns_remove])
def test_only_the_admin_can_change_the_campaigns(monkeypatch, save):
    monkeypatch.setattr(kpi, "is_admin", lambda user: False)
    out = asyncio.run(save(_Request(), user="someone"))
    assert out == {"status": "error", "message": "Only the admin can change the campaigns"}
