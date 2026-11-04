"""Weekly Business Review Comments: the checks in routers/wbr_comments.py that
need no database.

The risks covered: someone being alerted who cannot open the review, a
mention matched case-sensitively or with the full stop after it, the same
person alerted twice for one comment, an empty or over-long comment, and a
comment saved against something that is not a chart.

Run from source_code/router/backend:
    poetry run pytest tests/test_wbr_comments.py
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from routers import wbr_comments as wc

PEOPLE = [{"username": "tdrayson", "name": "Tom Drayson", "email": "tom.drayson@carsupermarket.com"},
          {"username": "jpervin", "name": "Jonny Pervin", "email": "jonny.pervin@carsupermarket.com"}]


@pytest.fixture(autouse=True)
def people(monkeypatch):
    monkeypatch.setattr(wc, "_commenters", lambda pages=None: PEOPLE)


def test_mentions_in_order_once_each_any_case():
    assert wc._mentions("@JPervin and @tdrayson, then @jpervin again") == ["jpervin", "tdrayson"]


def test_mention_with_full_stop_after_it():
    assert wc._mentions("over to you @jpervin.") == ["jpervin"]


def test_unknown_or_no_access_names_are_not_mentions():
    # mbarrick is not in the people who can open the review.
    assert wc._mentions("@mbarrick have a look, email me at x@y.com") == []


def test_body_is_trimmed_and_checked():
    assert wc._clean_body("  hello  ") == "hello"
    with pytest.raises(ValueError):
        wc._clean_body("   ")
    with pytest.raises(ValueError):
        wc._clean_body("x" * (wc.BODY_MAX + 1))


@pytest.mark.parametrize("target, ok", [
    ("chart:r1a2b3|m", True),
    ("chart:r1a2b3|w", True),
    ("chart:r1a2b3|q", False),
    ("chart:|m", False),
    ("table:r1|m", False),
    ("chart:r1|m; DROP TABLE x", False),
])
def test_target_shape(target, ok):
    assert bool(wc._TARGET_RE.match(target)) is ok
