"""Cars Bought helpers. Run from source_code/router/backend:
    poetry run pytest tests/test_cars_bought.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import pytest

from utils import cars_bought as cb


def test_parse_vrm_list_cleans_and_dedups():
    assert cb.parse_vrm_list("na73 tnf\nHY72ODU, sl22-zwv\n\nNA73TNF") == ["NA73TNF", "HY72ODU", "SL22ZWV"]


def test_normalise_extract_bca_invoice():
    # The BCA example: car, buyer's fee and assured charge on one invoice.
    raw = {"supplier_name": "British Car Auctions Limited", "invoice_number": "DP/8207", "invoice_date": "29/10/26",
           "invoice_net": "7,480.16", "invoice_vat": 1496.04, "invoice_gross": 8976.20,
           "vehicles": [{"vrm": "SL22 ZWV", "mileage": "19,094", "first_registered": "30/04/2022",
                         "description": "VAUXHALL CORSA 1.2", "vehicle_net": 7083.33, "vehicle_vat": 1416.67,
                         "vehicle_gross": 8500.00,
                         "fees": [{"description": "Buyers Fee (Business)", "net": 360.83, "vat": 72.17, "gross": 433.00},
                                  {"description": "BCA Assured Charge", "net": 36.00, "vat": 7.20, "gross": 43.20}]},
                        {"vrm": "", "description": "not a car"}]}
    out = cb.normalise_extract(raw)
    assert out["invoice_date"] == "2026-10-29"
    assert out["invoice_net"] == 7480.16
    assert len(out["vehicles"]) == 1
    v = out["vehicles"][0]
    assert v["vrm"] == "SL22ZWV" and v["mileage"] == 19094 and v["first_registered"] == "2022-04-30"
    assert (v["fees_net"], v["fees_vat"], v["fees_gross"]) == (396.83, 79.37, 476.20)
    checks = cb.totals_check(out["vehicles"], out["invoice_net"], out["invoice_vat"], out["invoice_gross"])
    assert [c["matches"] for c in checks] == [True, True, True]


def test_totals_check_flags_a_difference():
    rows = [{"vehicle_gross": 9350.00, "fees_gross": 460.80}]
    checks = cb.totals_check(rows, None, None, 9840.80)
    assert len(checks) == 1 and not checks[0]["matches"]


def test_parse_json_reply_with_fences():
    assert cb._parse_json_reply('```json\n{"vehicles": []}\n```') == {"vehicles": []}
    with pytest.raises(ValueError):
        cb._parse_json_reply("sorry")


def test_clean_custom_fields_keeps_keys_and_drops_blanks():
    out = cb.clean_custom_fields([{"key": "rrp", "label": "Retail Price", "type": "money"},
                                  {"label": "Prep Notes", "type": "bad"}, {"label": "  "}])
    assert out == [{"key": "rrp", "label": "Retail Price", "type": "money", "required": False},
                   {"key": "prep_notes", "label": "Prep Notes", "type": "text", "required": False}]


def test_file_to_text_csv_keeps_every_cell():
    text = cb.file_to_text("list.csv", b"Reg,Mileage,Price\nNA73TNF,22043,9350\n")
    assert "Reg | Mileage | Price" in text and "NA73TNF | 22043 | 9350" in text


def test_file_kind_refuses_other_files():
    assert cb.file_kind("photo.JPG") == "image"
    with pytest.raises(ValueError):
        cb.file_kind("letter.docx")


def test_photo_goes_to_ocr():
    with pytest.raises(cb.ScanNeeded):
        cb.file_to_text("invoice.jpg", b"\xff\xd8")


def test_textract_text_pairs_label_with_handwriting():
    blocks = [
        {"Id": "w1", "BlockType": "WORD", "Text": "Purchase"},
        {"Id": "w2", "BlockType": "WORD", "Text": "Price"},
        {"Id": "w3", "BlockType": "WORD", "Text": "19500"},
        {"Id": "k", "BlockType": "KEY_VALUE_SET", "EntityTypes": ["KEY"],
         "Relationships": [{"Type": "CHILD", "Ids": ["w1", "w2"]}, {"Type": "VALUE", "Ids": ["v"]}]},
        {"Id": "v", "BlockType": "KEY_VALUE_SET", "EntityTypes": ["VALUE"],
         "Relationships": [{"Type": "CHILD", "Ids": ["w3"]}]},
        {"Id": "l1", "BlockType": "LINE", "Text": "Used Vehicle Purchase Invoice"},
    ]
    text = cb.textract_text(blocks)
    assert "Purchase Price: 19500" in text and "Used Vehicle Purchase Invoice" in text
