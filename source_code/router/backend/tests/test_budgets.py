"""Weekly Business Review budgets: the CSV reading and edit checks in
routers/budgets.py that need no database.

The risks covered: £, commas, minus signs and % read wrong, the VOLUME and
CASH heading rows read as budget lines, a line saved under the wrong month,
and a typed figure that is not a number saved anyway.

Run from source_code/router/backend:
    poetry run pytest tests/test_budgets.py
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from routers import budgets as b

SAMPLE = '﻿' + '\n'.join([
    'Month,,2026-09,2026-10',
    'Net Deliveries B2C,nb cars B2C,"2,100","1,891"',
    'GPU-M (£/unit),£/u,£801,£799',
    'COCA £/u,,-£269,-£321',
    'Sales costs k£,,"-1,720","-1,563"',
    'EBITDA %,,3.80%,2.00%',
    '"SMART +, Insurance",,£45,£66',
    ',,,',
    'VOLUME TARGET,,2026-09,2026-10',
    'Net Orders,nb cars B2C,"2,543",',
    'CASH TARGET,,2026-09,2026-10',
    'inventories EoP,,2450,2442',
])


@pytest.mark.parametrize("text,expected", [
    ("£1,700", (1700.0, False)), ("-£281", (-281.0, False)), ("-1,616", (-1616.0, False)),
    ("60%", (60.0, True)), ("2.10%", (2.1, True)), ("(45)", (-45.0, False)),
    ("", (None, False)), ("-", (None, False)), ("16,948", (16948.0, False)),
])
def test_parse_number(text, expected):
    assert b.parse_number(text) == expected


def test_parse_number_rejects_text():
    with pytest.raises(ValueError):
        b.parse_number("n/a")


def test_parse_csv_sections_and_values():
    parsed = b.parse_budget_csv(SAMPLE)
    assert parsed["errors"] == []
    by_label = {l["label"]: l for l in parsed["lines"]}
    assert set(by_label) == {"Net Deliveries B2C", "GPU-M (£/unit)", "COCA £/u", "Sales costs k£", "EBITDA %",
                             "SMART +, Insurance", "Net Orders", "inventories EoP"}
    assert by_label["Net Deliveries B2C"]["section"] == "P&L"
    assert by_label["Net Deliveries B2C"]["values"] == {"2026-09": 2100.0, "2026-10": 1891.0}
    assert by_label["Net Deliveries B2C"]["note"] == "nb cars B2C"
    assert by_label["COCA £/u"]["values"]["2026-10"] == -321.0
    assert by_label["EBITDA %"]["is_percent"] is True
    assert by_label["EBITDA %"]["values"]["2026-09"] == 3.8
    assert by_label["Net Orders"]["section"] == "Volume Target"
    assert by_label["Net Orders"]["values"] == {"2026-09": 2543.0, "2026-10": None}
    assert by_label["inventories EoP"]["section"] == "Cash Target"
    assert [l["row_order"] for l in parsed["lines"]] == list(range(len(parsed["lines"])))


def test_parse_csv_bad_value_and_duplicate():
    text = 'Month,,2026-09\nA,,abc\nA,,5\n'
    parsed = b.parse_budget_csv(text)
    assert len(parsed["lines"]) == 1
    assert parsed["lines"][0]["values"] == {"2026-09": None}
    assert len(parsed["errors"]) == 2


def test_parse_csv_line_before_months():
    parsed = b.parse_budget_csv('A,,5\nMonth,,2026-09\nB,,6\n')
    assert [l["label"] for l in parsed["lines"]] == ["B"]
    assert len(parsed["errors"]) == 1


def test_check_new_line():
    assert b.check_new_line({"section": " P&L ", "label": " Marketing k£ ", "note": ""}) == ("P&L", "Marketing k£", "")
    for bad in ({"section": "", "label": "A"}, {"section": "P&L", "label": "  "},
                {"section": "x" * 51, "label": "A"}, "not a dict"):
        with pytest.raises(ValueError):
            b.check_new_line(bad)


@pytest.mark.parametrize("line_id,to,expected", [
    (3, "top", [3, 1, 2, 4]), (3, "up", [1, 3, 2, 4]), (3, "down", [1, 2, 4, 3]), (3, "bottom", [1, 2, 4, 3]),
    (1, "up", [1, 2, 3, 4]), (1, "top", [1, 2, 3, 4]), (4, "down", [1, 2, 3, 4]), (1, "bottom", [2, 3, 4, 1]),
])
def test_reorder_ids(line_id, to, expected):
    assert b.reorder_ids([1, 2, 3, 4], line_id, to) == expected


def test_reorder_ids_rejects_bad_input():
    with pytest.raises(ValueError):
        b.reorder_ids([1, 2], 9, "up")
    with pytest.raises(ValueError):
        b.reorder_ids([1, 2], 1, "sideways")


def test_check_edit():
    assert b.check_edit({"line_id": "3", "month": "2026-10", "value": "£1,891"}) == (3, "2026-10", 1891.0)
    assert b.check_edit({"line_id": 3, "month": "2026-10", "value": ""}) == (3, "2026-10", None)
    for bad in ({"line_id": "x", "month": "2026-10", "value": "1"},
                {"line_id": 3, "month": "2026-13", "value": "1"},
                {"line_id": 3, "month": "2026-10", "value": "abc"}):
        with pytest.raises(ValueError):
            b.check_edit(bad)
