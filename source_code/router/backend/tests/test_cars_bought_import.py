"""Importing the old Cars Bought sheet. Run from source_code/router/backend:
    poetry run pytest tests/test_cars_bought_import.py
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from utils import cars_bought as cb
from utils import cars_bought_import as imp

FIELDS = [{"key": "notes", "label": "Notes", "type": "text"}, {"key": "sat_nav", "label": "Sat Nav", "type": "yesno"},
          {"key": "rrp", "label": "RRP", "type": "money"}]
HEADINGS = ["DATE", "BUYERS", "SUPPLIER", "REG", "MAKE", "CAP ID", "NOTES", "SAT NAV", "PURCHASE PRICE", "RRP", "SOMETHING ELSE"]
DAY = datetime.date(2026, 10, 29)


def row(reg="FY75 XUM", date="29/10/2026", supplier="SHOREHAM", buyer="BEN CAMPBELL", sat="YES", rrp="£23,298"):
    return [date, buyer, supplier, reg, "NISSAN", "105653", "V5 TO FOLLOW", sat, "£20,070.80", rrp, "x"]


def plan(rows, stored=None):
    return imp.plan(HEADINGS, rows, imp.suggest_mapping(HEADINGS, FIELDS), FIELDS, stored or {})


def test_suggest_mapping_matches_the_sheets_headings_and_leaves_unknown_ones_out():
    assert imp.suggest_mapping(HEADINGS, FIELDS) == {
        "DATE": "purchase_date", "BUYERS": "buyer", "SUPPLIER": "supplier_name", "REG": "vrm", "MAKE": "make", "CAP ID": "cap_id",
        "NOTES": "custom:notes", "SAT NAV": "custom:sat_nav", "PURCHASE PRICE": "vehicle_gross", "RRP": "custom:rrp"}


def test_read_csv_takes_the_first_row_with_anything_in_it_as_headings():
    assert imp.read_csv(b"\xef\xbb\xbf,,\nDATE,REG\n\n29/10/2026,AB12CDE\n") == (["DATE", "REG"], [["29/10/2026", "AB12CDE"]])


def test_read_csv_reads_a_file_excel_saved_without_utf8():
    assert imp.read_csv("REG,PRICE\nAB12CDE,£100\n".encode("cp1252"))[1] == [["AB12CDE", "£100"]]


def test_plan_reads_a_car_with_its_custom_fields():
    out = plan([row()])
    assert (out["cars"], out["problems"], out["missing"]) == (1, [], [])
    batch = out["batches"][0]
    assert (batch["purchase_date"], batch["supplier_name"], batch["buyer"]) == (DAY, "SHOREHAM", "BEN CAMPBELL")
    car = batch["vehicles"][0]
    assert (car["vrm"], car["make"], car["cap_id"], car["vehicle_gross"]) == ("FY75XUM", "NISSAN", "105653", "£20,070.80")
    assert car["custom"] == {"notes": "V5 TO FOLLOW", "sat_nav": "Yes", "rrp": 23298.0}


def test_plan_groups_one_purchase_per_date_supplier_and_buyer():
    out = plan([row("AA11AAA"), row("BB11BBB"), row("CC11CCC", supplier="URG"), row("DD11DDD", date="30/10/2026"), row("EE11EEE", buyer="TOM LEWIS")])
    assert (out["cars"], len(out["batches"])) == (5, 4)


def test_plan_leaves_out_a_car_already_stored_on_that_date_but_adds_one_from_another_date():
    out = plan([row("AA11AAA"), row("BB11BBB")], {"AA11AAA": {DAY}, "BB11BBB": {datetime.date(2025, 1, 1)}})
    assert (out["cars"], out["already_here"], out["bought_on_another_date"]) == (1, 1, 1)
    assert out["batches"][0]["vehicles"][0]["vrm"] == "BB11BBB"


def test_plan_reports_rows_it_cannot_use_with_their_row_number():
    out = plan([row(reg=""), row("BB11BBB", date="soon"), row("CC11CCC", supplier=""), row("DD11DDD"), row("DD11DDD")])
    assert out["cars"] == 1
    assert out["problems"] == ["Row 2 left out: no VRM", "Row 3 (BB11BBB) left out: date not read: soon",
                               "Row 4 (CC11CCC) left out: no supplier", "Row 6 (DD11DDD) left out: on an earlier row with the same date"]


def test_plan_keeps_a_car_whose_custom_value_does_not_fit_and_says_so():
    out = plan([row(sat="MAYBE", rrp="POA")])
    assert out["cars"] == 1
    assert out["batches"][0]["vehicles"][0]["custom"] == {"notes": "V5 TO FOLLOW"}
    assert out["problems"] == ["Row 2 (FY75XUM): Sat Nav left blank: MAYBE", "Row 2 (FY75XUM): RRP left blank: POA"]


def test_plan_says_which_required_columns_have_no_home():
    out = imp.plan(HEADINGS, [row()], {"MAKE": "make"}, FIELDS, {})
    assert out["missing"] == ["Date", "Supplier", "VRM"]
    assert out["cars"] == 0


def test_an_imported_car_with_no_vat_given_is_unknown_not_margin():
    assert cb.vat_status({"source": imp.SOURCE, "vehicle_vat": None}) == "Unknown"
    assert cb.vat_status({"source": imp.SOURCE, "vehicle_vat": 3000}) == "VAT Qualifying"
    assert cb.vat_status({"source": "upload", "vehicle_vat": None}) == "Margin"


def test_plan_ignores_a_field_that_is_not_an_option():
    out = imp.plan(["REG", "DATE", "SUPPLIER"], [["AA11AAA", "29/10/2026", "URG"]],
                   {"REG": "vrm", "DATE": "purchase_date", "SUPPLIER": "supplier_name; drop"}, [], {})
    assert out["missing"] == ["Supplier"]
