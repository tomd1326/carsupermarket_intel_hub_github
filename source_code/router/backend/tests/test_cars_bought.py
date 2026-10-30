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


def test_clean_custom_fields_keeps_keys_and_drops_blanks():
    out = cb.clean_custom_fields([{"key": "rrp", "label": "Retail Price", "type": "money"},
                                  {"label": "Prep Notes", "type": "bad"}, {"label": "  "}])
    assert out == [{"key": "rrp", "label": "Retail Price", "type": "money", "required": False},
                   {"key": "prep_notes", "label": "Prep Notes", "type": "text", "required": False}]


def test_file_kind_refuses_other_files():
    assert cb.file_kind("photo.JPG") == "image"
    with pytest.raises(ValueError):
        cb.file_kind("letter.docx")


NAMES = cb.read_column_names(None)


def test_match_field_erac_headings():
    assert cb.match_field("PRICE EXCL. VAT", NAMES) == "vehicle_net"
    assert cb.match_field("VAT (Standard Rate)", NAMES) == "vehicle_vat"
    assert cb.match_field("INCLUSIVE VAT", NAMES) == "vehicle_gross"
    assert cb.match_field("SERVICE FEE", NAMES) == "fee_net"
    assert cb.match_field("LICENSE", NAMES) == "vrm"
    assert cb.match_field("VAT %", NAMES) is None


def test_money_pounds_and_pence_boxes():
    assert float(cb.money("19500 00")) == 19500.0
    assert float(cb.money("£17,750.00")) == 17750.0


def test_parse_sheet_erac_layout():
    csv = (b"ERAC UK LTD,,,,,\n"
           b"UNIT NUMBER,DOR,LICENSE,MILEAGE,PRICE EXCL. VAT,SERVICE FEE,VAT (Standard Rate),INCLUSIVE VAT\n"
           b"8FY1XS,22/05/2024,YF24BHD,\"35,311\",\"17,750.00\",67.08,\"3,563.42\",\"21,380.50\"\n"
           b"Grand Total,,,,,,,\n")
    out = cb.normalise_extract(cb.parse_sheet("erac.csv", csv, NAMES, cb.DEFAULT_FEE_WORDS))
    v = out["vehicles"][0]
    assert len(out["vehicles"]) == 1 and v["vrm"] == "YF24BHD" and v["mileage"] == 35311
    assert (v["vehicle_net"], v["fees_net"], v["vehicle_vat"], v["vehicle_gross"]) == (17750.0, 67.08, 3550.0, 21300.0)
    assert (v["fees_vat"], v["fees_gross"]) == (13.42, 80.5)
    assert v["first_registered"] == "2024-05-22"


def _f(ftype, label, value):
    return {"Type": {"Text": ftype}, "LabelDetection": {"Text": label} if label else None,
            "ValueDetection": {"Text": value}}


def test_parse_expense_openlane_fees_named_before_the_car():
    item = lambda desc, net, vat, gross: {"LineItemExpenseFields": [
        _f("ITEM", "Description", desc), _f("OTHER", "Net", net), _f("OTHER", "VAT", vat), _f("PRICE", "Gross", gross)]}
    resp = {"ExpenseDocuments": [{
        "SummaryFields": [_f("INVOICE_RECEIPT_ID", "Invoice No.", "PSI061549"),
                          _f("INVOICE_RECEIPT_DATE", "Inv./Tax Point Date", "29/10/26"),
                          _f("SUBTOTAL", "Total GBP Excl. VAT", "8,200.66"), _f("TAX", "20% VAT", "1,640.14"),
                          _f("TOTAL", "Total GBP", "9,840.80")],
        "LineItemGroups": [{"LineItems": [
            item("Buyer's fee for NA73TNF", "384.00", "76.80", "460.80"),
            item("Mechanical Indicator for NA73TNF", "25.00", "5.00", "30.00"),
            item("Vehicle Reg, No. NA73TNF", "7,791.66", "1,558.34", "9,350.00")]}]}]}
    out = cb.normalise_extract(cb.parse_expense([resp], NAMES, cb.DEFAULT_FEE_WORDS))
    assert [v["vrm"] for v in out["vehicles"]] == ["NA73TNF"]
    v = out["vehicles"][0]
    assert v["vehicle_gross"] == 9350.0 and v["fees_gross"] == 490.8
    checks = cb.totals_check(out["vehicles"], out["invoice_net"], out["invoice_vat"], out["invoice_gross"])
    assert all(c["matches"] for c in checks) and len(checks) == 3


def _layout(pages):
    return cb.normalise_extract(cb.parse_layout(pages, NAMES, cb.DEFAULT_FEE_WORDS, cb.DEFAULT_SUPPLIER_NAMES))


def _all_match(out):
    checks = cb.totals_check(out["vehicles"], out["invoice_net"], out["invoice_vat"], out["invoice_gross"])
    return len(checks) == 3 and all(c["matches"] for c in checks)


def test_layout_bca_heading_and_amounts_not_aligned():
    page = (
        "   BRITISH CAR AUCTIONS LIMITED                                      INVOICE DP/8207\n"
        "                                                                     Document date      29/10/26\n"
        "      ITEM DESCRIPTION                                                                   PRICE          VAT       VAT %       TOTAL\n"
        "      DPZ21/ZN064   SL22 ZWV VAUXHALL CORSA 1.2 A ELD 20  ASSURED WHITE      7,083.33     1,416.67      20.00       8,500.00\n"
        "      ODOMETER:19094WARRANTED 1ST REG:30/04/22  MOT:3/03/27-NO DOC\n"
        "      L/B-HERE S/H-HERE CH:VXKUPHMHDN4212882 EN:10XKDU0078781 QUALIFYIN\n"
        "                    Buyers Fee (Business)                             360.83        72.17      20.00         433.00\n"
        "                    BCA Assured Charge                                 36.00         7.20      20.00          43.20\n"
        "                                                 TOTAL       �         7,480.16       1,496.04            8,976.20\n"
        "                                                                     TOTAL DUE             �           8,976.20\n")
    out = _layout([page])
    v = out["vehicles"][0]
    assert (out["supplier_name"], out["invoice_number"], out["invoice_date"]) == ("BCA", "DP/8207", "2026-10-29")
    assert (v["vrm"], v["mileage"], v["vin"], v["first_registered"]) == ("SL22ZWV", 19094, "VXKUPHMHDN4212882", "2022-04-30")
    assert (v["vehicle_net"], v["fees_gross"]) == (7083.33, 476.2)
    assert _all_match(out)


def test_layout_erac_two_line_heading_and_second_page():
    head = ("UNIT NUMBER   DOR          LICENSE    CHASSIS ID           REG   MILEAGE    PRICE EXCL.   TRANSPORT   SERVICE FEE   VAT (Standard   INCLUSIVE\n"
            "                                                           YR                VAT                                     Rate)           VAT\n")
    row = "{u}   22/05/2024   {vrm}    WBA50FF0208E40657    2024   35,311     �17,750.00     �0.00       �67.08        �3,563.42       �21,380.50\n"
    page1 = "ERAC UK LTD.     Invoice #:   A7170014\nDate:  29/10/2026\n" + head + row.format(u="8FY1XS", vrm="YF24BHD")
    page2 = (row.format(u="8FC577", vrm="DS74UFM") +
             "    Grand Total:     �35,500.00     �0.00     �134.16     �7,126.84     �42,761.00\n")
    out = _layout([page1, page2])
    assert out["supplier_name"] == "Enterprise" and out["invoice_number"] == "A7170014"
    assert [v["vrm"] for v in out["vehicles"]] == ["YF24BHD", "DS74UFM"]
    v = out["vehicles"][0]
    # The row's VAT covers car and service fee: the fee gets 20% (13.42), the car the rest.
    assert (v["mileage"], v["vehicle_net"], v["vehicle_vat"], v["vehicle_gross"]) == (35311, 17750.0, 3550.0, 21300.0)
    assert (v["fees_net"], v["fees_vat"], v["fees_gross"]) == (67.08, 13.42, 80.5)
    assert _all_match(out)


def test_row_fee_vat_never_more_than_the_row_vat():
    # A margin-scheme car: the row's VAT is only the fee's, so the car's VAT is 0.
    fees = [{"description": "Service Fee", "net": 67.08, "vat": None, "gross": None, "in_row": True}]
    assert cb.split_row_fees(fees, 13.42, 12080.50, 20) == (0.0, 12000.0)
    assert fees[0]["vat"] == 13.42 and fees[0]["gross"] == 80.5


def test_layout_smc_form_with_no_table_row():
    page = ("Invoice Total                                   16,500.00\n"
            "Total VAT Amount                                 2,750.00\n"
            "Total Net Amount                               13,750.00\n"
            "Mileage: 51,341\n"
            "                       13,750.00              20.00       2,750.00\n"
            "Chassis No: SB1Z53BE90E109874\n"
            "Reg No: HY72 ODU\n"
            "Vehicle:  Toyota Corolla Design HEV CVT\n"
            "Details                         Net Amount          VAT Rate              VAT\n"
            "  VAT Reg No:\n"
            "        Invoice Date         26/10/2026\n"
            "        Invoice Number        248860\n"
            "Southern Motor Contracts Limited\n")
    out = _layout([page])
    v = out["vehicles"][0]
    assert (out["supplier_name"], out["invoice_number"], out["invoice_date"]) == ("Southern Motor Contracts", "248860", "2026-10-26")
    assert (v["vrm"], v["mileage"], v["supplier_description"]) == ("HY72ODU", 51341, "Toyota Corolla Design HEV CVT")
    assert (v["vehicle_net"], v["vehicle_vat"], v["vehicle_gross"]) == (13750.0, 2750.0, 16500.0)
    assert _all_match(out)


def test_parse_expense_handwritten_form_without_table():
    resp = {"ExpenseDocuments": [{
        "SummaryFields": [_f("OTHER", "Reg. No", "EA18 KWY"), _f("OTHER", "Mileage", "70,683"),
                          _f("OTHER", "Purchase Price", "19500 00")],
        "LineItemGroups": []}]}
    v = cb.normalise_extract(cb.parse_expense([resp], NAMES, cb.DEFAULT_FEE_WORDS))["vehicles"][0]
    assert (v["vrm"], v["mileage"], v["vehicle_gross"]) == ("EA18KWY", 70683, 19500.0)
