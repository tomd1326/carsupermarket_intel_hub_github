"""Weekly Business Review slides: the deck check (routers/kpi.py _clean_wbr_deck)
and the new settings' defaults.

The risks covered: an item placed off the slide, a repeated id (two items
would then share one chart), Shown above Max, a day chart with a window, and
the slides being on by default (they must stay off until turned on).

Run from source_code/router/backend:
    poetry run pytest tests/test_wbr_deck.py
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from routers import kpi


def item(**changes):
    base = {"id": "i1", "kind": "chart", "ref": "row1", "period": "m", "x": 0, "y": 0, "w": 8, "h": 6}
    return {**base, **changes}


def deck(*items, **slide):
    return {"slides": [{"id": "s1", "title": "Orders", "subtitle": "", "items": list(items), **slide}]}


def test_good_deck_is_kept():
    out = kpi._clean_wbr_deck(deck(item(show=7, max=24), item(id="i2", x=8.5, w=7.5)))
    assert out["slides"][0]["items"][0] == {"id": "i1", "kind": "chart", "ref": "row1", "period": "m",
                                            "x": 0.0, "y": 0.0, "w": 8.0, "h": 6.0, "show": 7, "max": 24}
    assert out["slides"][0]["items"][1]["x"] == 8.5


def test_same_chart_twice_is_allowed():
    out = kpi._clean_wbr_deck(deck(item(), item(id="i2", x=8)))
    assert [i["ref"] for i in out["slides"][0]["items"]] == ["row1", "row1"]


def test_unknown_fields_are_dropped():
    out = kpi._clean_wbr_deck(deck(item(colour="red"), extra="x"))
    assert "colour" not in out["slides"][0]["items"][0]
    assert "extra" not in out["slides"][0]


@pytest.mark.parametrize("bad", [
    item(x=45, w=8),                 # off the right of the slide
    item(y=-1),                      # above it
    item(w=0),                       # no size
    item(x=True),                    # not a number
    item(period="q"),
    item(kind="table"),              # tables are not built yet
    item(show=8, max=4),
    item(period="d", show=7),        # the day chart has its own scroller
    item(show=0),
])
def test_bad_item_is_refused(bad):
    with pytest.raises(ValueError):
        kpi._clean_wbr_deck(deck(bad))


def test_repeated_id_is_refused():
    with pytest.raises(ValueError):
        kpi._clean_wbr_deck(deck(item(), item()))


def test_slide_and_item_ids_must_differ():
    with pytest.raises(ValueError):
        kpi._clean_wbr_deck(deck(item(id="s1")))


def test_own_titles_are_kept_trimmed_and_blank_dropped():
    out = kpi._clean_wbr_deck(deck(item(title="  Gross Orders B2C ", subtitle=""), item(id="i2", title="x" * 250)))
    first, second = out["slides"][0]["items"]
    assert first["title"] == "Gross Orders B2C" and "subtitle" not in first
    assert len(second["title"]) == 200


def test_slides_off_by_default():
    settings = kpi._kpi_settings(None)
    assert settings["wbr_slides_on"] == 0
    assert settings["wbr_snap"] == 1
    assert settings["wbr_slide_border_colour"] == "#cbd5e1"
