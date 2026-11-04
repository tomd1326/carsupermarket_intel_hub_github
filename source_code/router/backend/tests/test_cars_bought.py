"""Cars Bought helpers. Run from source_code/router/backend:
    poetry run pytest tests/test_cars_bought.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import pytest

from utils import cars_bought as cb


STORED_CAR = {"cap_capid": 99, "cap_manufacturer": "VW", "cap_model": "Golf", "cap_derivative": "1.5 Life"}


def test_stored_results_reuses_a_complete_stored_valuation_at_the_same_rounded_mileage():
    cached = {("AB12CDE", 20000): {"cap_live_clean": 9000, "cap_monthly_clean": 8000, "at_retail_valuation": 12000, "at_rating": 71.5}}
    out = cb.stored_results([("AB12CDE", 20300)], cached, {"AB12CDE": STORED_CAR})
    assert out == {"AB12CDE": {"cap_id": 99, "make": "VW", "model": "Golf", "derivative": "1.5 Life", "cap_clean": 9000.0,
                               "at_retail_valuation": 12000.0, "at_rating": 71.5, "note": None, "from_store": True}}


def test_stored_results_uses_monthly_clean_when_there_is_no_live_clean():
    cached = {("AB12CDE", 20000): {"cap_live_clean": None, "cap_monthly_clean": 8000, "at_retail_valuation": 12000}}
    assert cb.stored_results([("AB12CDE", 20000)], cached, {"AB12CDE": STORED_CAR})["AB12CDE"]["cap_clean"] == 8000.0


@pytest.mark.parametrize("items,val,car", [
    ([("AB12CDE", None)], {"cap_live_clean": 9000, "at_retail_valuation": 12000}, STORED_CAR),     # no mileage
    ([("AB12CDE", 20000)], {"cap_live_clean": None, "at_retail_valuation": 12000}, STORED_CAR),    # no CAP value
    ([("AB12CDE", 20000)], {"cap_live_clean": 9000, "at_retail_valuation": None}, STORED_CAR),     # no Autotrader value
    ([("AB12CDE", 20000)], {"cap_live_clean": 9000, "at_retail_valuation": 12000}, {"cap_capid": None}),   # no CAP ID
    ([("AB12CDE", 50000)], {"cap_live_clean": 9000, "at_retail_valuation": 12000}, STORED_CAR),    # other mileage
])
def test_stored_results_leaves_an_incomplete_car_for_a_live_call(items, val, car):
    assert cb.stored_results(items, {("AB12CDE", 20000): val}, {"AB12CDE": car}) == {}


def test_buyer_figures_from_rows_keeps_only_filled_in_figures():
    rows = [{"vrm": "A1", "custom": {"rrp": "12,500", "est_prep_cost": "", "notes": "x"}},
            {"vrm": "B2", "custom": {"notes": "x"}}, {"vrm": "C3"},
            {"vrm": "D4", "custom": {"rrp": 100, "est_prep_cost": "250.50"}}]
    out = cb.buyer_figures_from_rows(rows)
    assert [r["vrm"] for r in out] == ["A1", "D4"]
    assert "est_prep_cost" not in out[0] and float(out[0]["rrp"]) == 12500
    assert float(out[1]["est_prep_cost"]) == 250.5


def test_contact_options_sorts_and_shows_the_id_only_on_repeated_names():
    out = cb.contact_options([(3, "OPENLANE"), (1, "dbcar"), (2, "ZED"), (4, "ZED ")])
    assert [c["label"] for c in out] == ["dbcar", "OPENLANE", "ZED (ID 2)", "ZED (ID 4)"]
    assert out[3] == {"id": 4, "name": "ZED", "label": "ZED (ID 4)"}


def test_clean_supplier_contacts_keeps_a_clear_and_drops_rows_with_no_supplier():
    rows = [{"supplier_id": "5", "contact_id": "460221", "contact_name": " OPENLANE "},
            {"supplier_id": 6, "contact_id": "", "contact_name": "left over"},
            {"contact_id": 1, "contact_name": "No Supplier"}]
    assert cb.clean_supplier_contacts(rows) == [
        {"supplier_id": 5, "contact_id": 460221, "contact_name": "OPENLANE"},
        {"supplier_id": 6, "contact_id": None, "contact_name": None}]
    assert cb.clean_supplier_contacts(None) == []


def test_usable_supplier_names_drops_bad_rows_and_keeps_the_rest():
    rows = [{"text": "OPENLANE", "supplier": "OPENLANE"}, {"text": "ab", "supplier": "Too Short"},
            {"text": "No Supplier"}, "not a row", {"text": "openlane", "supplier": "Openlane Ltd"}]
    assert cb.usable_supplier_names(rows) == [{"text": "openlane", "supplier": "Openlane Ltd"}]


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


BCA_CUT_OFF_FEE_PAGE = (
    "   BRITISH CAR AUCTIONS LIMITED                                      INVOICE DP/8207\n"
    "      ITEM DESCRIPTION                                                                   PRICE          VAT       VAT %       TOTAL\n"
    "      WKZN23/ZEN035 LM72 CKJ AUDI Q4 E-TRON 0.0 A SP  21  ASSURED GREY     15,000.00     3,000.00      20.00      18,000.00\n"
    "      ODOMETER:29610WARRANTED 1ST REG:26/09/22  MOT:25/09/27-NO DOC\n"
    "      L/B-HERE S/H-HERE CH:WAUZZZFZ1NP054832 EN:EBJA QUALIFYING VEH\n"
    "                    Buyers Fee (Business)                             490.00        98.00      20.00         588.00\n"
    "                    BCA EV/Hybrid Assured Cha                          41.50         8.30      20.00          49.80\n"
    "                                                 TOTAL       �        15,531.50       3,106.30           18,637.80\n"
    "                    VAT SUMMARY AT 20%                              15,531.50     3,106.30\n")


def test_layout_bca_cut_off_fee_line_goes_in_the_fees():
    out = cb.normalise_extract(cb.parse_layout([BCA_CUT_OFF_FEE_PAGE], NAMES, cb.DEFAULT_FEE_WORDS,
                                               cb.DEFAULT_SUPPLIER_NAMES, cb.DEFAULT_SUPPLIER_RULES))
    assert len(out["vehicles"]) == 1
    v = out["vehicles"][0]
    assert (v["vrm"], v["vehicle_net"], v["vehicle_vat"], v["vehicle_gross"]) == ("LM72CKJ", 15000.0, 3000.0, 18000.0)
    assert (v["fees_net"], v["fees_vat"], v["fees_gross"]) == (531.5, 106.3, 637.8)
    assert [f["description"] for f in v["fees"]] == ["Buyers Fee (Business)", "BCA EV/Hybrid Assured Cha"]
    assert _all_match(out)


def test_layout_cut_off_fee_line_is_left_out_without_the_supplier_rule():
    v = _layout([BCA_CUT_OFF_FEE_PAGE])["vehicles"][0]
    assert v["fees_gross"] == 588.0


def test_supplier_rule_adds_fee_words_for_that_supplier_only():
    rules = [{"supplier": "BCA", "fee_words": ["levy"], "priced_lines_are_fees": False}]
    assert cb.apply_supplier_rule("bca", ["fee"], rules) == (["fee", "levy"], False)
    assert cb.apply_supplier_rule("OPENLANE", ["fee"], rules) == (["fee"], False)
    assert cb.apply_supplier_rule(None, ["fee"], rules) == (["fee"], False)


def test_clean_supplier_rules_drops_rows_with_no_supplier():
    out = cb.clean_supplier_rules([{"supplier": " BCA ", "fee_words": ["a", " "], "priced_lines_are_fees": 1},
                                   {"supplier": "", "fee_words": ["b"]}])
    assert out == [{"supplier": "BCA", "fee_words": ["a"], "priced_lines_are_fees": True}]


def test_layout_openlane_car_with_no_vat_amount():
    page = (
        "   Invoice No.:  PSI061548      OPENLANE UK\n"
        "   Inv./Tax Point Date:  29/10/26\n"
        "   Description                                                  VAT %      Net      VAT     Gross\n"
        "   Vehicle Details:  Reg. No.:HT22XLZ VIN: VXKUKZKXZNW054269 Reg. Date: 15/08/2022 Mileage: 51800\n"
        "   Buyer's fee for HT22XLZ                                         20    384.00    76.80    460.80\n"
        "   Mechanical Indicator for HT22XLZ                                20     25.00     5.00     30.00\n"
        "   Vehicle Reg, No. HT22XLZ                                         0  9,000.00           9,000.00\n"
        "                                                     Subtotal          9,409.00    81.80  9,490.80\n")
    out = _layout([page])
    v = out["vehicles"][0]
    assert (v["vehicle_net"], v["vehicle_vat"], v["vehicle_gross"]) == (9000.0, 0.0, 9000.0)
    assert [f["description"] for f in v["fees"]] == ["Buyer's fee for HT22XLZ", "Mechanical Indicator for HT22XLZ"]
    assert out["invoice_date"] == "2026-10-29"
    assert _all_match(out)


def test_layout_fleet_auction_total_columns_and_second_lines_for_a_car():
    page = (
        "   The Fleet Auction Group                         Invoice Number: 244060\n"
        "                                                   Date: 29/10/2026\n"
        "   Qty  Description                                 Unit Price      Total        VAT   Total Inc\n"
        "   1    Lot 222 (WV75SZD: Ford Puma 1.0 EcoBoost)   £16,000.00  £16,000.00  £3,200.00  £19,200.00\n"
        "        Mileage: 7,814 Miles Warranted\n"
        "        VIN: WF02XXERK2SA11261\n"
        "        DFR: 30/09/2025\n"
        "   1    Buyer's Premium (WV75SZD)                     £375.00     £375.00     £75.00     £450.00\n"
        "   1    BEN Donation (WV75SZD)                          £1.00       £1.00      £0.00       £1.00\n"
        "                                          TOTAL:  £16,376.00  £3,275.00  £19,651.00\n")
    out = _layout([page])
    assert len(out["vehicles"]) == 1
    v = out["vehicles"][0]
    assert out["supplier_name"] == "The Fleet Auction Group"
    assert (v["vehicle_net"], v["vehicle_vat"], v["vehicle_gross"]) == (16000.0, 3200.0, 19200.0)
    assert (v["mileage"], v["first_registered"]) == (7814, "2025-09-30")
    assert (v["fees_net"], v["fees_vat"], v["fees_gross"]) == (376.0, 75.0, 451.0)
    assert _all_match(out)


def test_layout_motorspace_sale_agreement():
    page = (
        "                                                       Motorspace Ltd\n"
        "    SALE AGREEMENT\n"
        "Date:                           Broker:\n"
        "                 13/10/2026     Sam Anderson\n"
        "Vehicle:     VOLKSWAGEN POLO 1.0 TSI R-Line 5dr DSG       Mileage:              14400\n"
        "Reg Number:  MW23BZF                                      Registration Date:    31/05/2023\n"
        "Sales Price:                         £14,850.00           Commission:           £150.00\n"
        "                                                          VAT:                  £30.00\n"
        "                                                          Total:                £180.00\n"
        "Selling Dealership:  Cromford Cars                        Buying Dealership:    CarSupermarket.com\n")
    out = _layout([page])
    v = out["vehicles"][0]
    assert (out["supplier_name"], out["invoice_date"], out["invoice_gross"]) == ("Cromford Cars", "2026-10-13", None)
    assert (v["vrm"], v["mileage"], v["first_registered"]) == ("MW23BZF", 14400, "2023-05-31")
    assert v["supplier_description"] == "VOLKSWAGEN POLO 1.0 TSI R-Line 5dr DSG"
    assert (v["vehicle_net"], v["vehicle_vat"], v["vehicle_gross"]) == (14850.0, 0.0, 14850.0)
    assert (v["fees_net"], v["fees_vat"], v["fees_gross"]) == (150.0, 30.0, 180.0)
    assert cb.vat_status(v) == "Margin" and cb.purchase_total(v) == 15030.0


def test_vat_status_goes_by_the_car_not_its_fees():
    assert cb.vat_status({"vehicle_vat": 3000.0, "fees_vat": 0}) == "VAT Qualifying"
    assert cb.vat_status({"vehicle_vat": None, "fees_vat": 81.8}) == "Margin"
    assert cb.vat_status({"vehicle_vat": 0, "fees_vat": 13.42}) == "Margin"


def test_purchase_total_is_the_car_every_fee_and_all_vat():
    assert cb.purchase_total({"vehicle_gross": 18000.0, "fees_gross": 637.8}) == 18637.8
    assert cb.purchase_total({"vehicle_net": 100.0, "vehicle_vat": 20.0, "fees_gross": None}) == 120.0


def test_match_supplier_longest_text_wins_whatever_the_order():
    names = [{"text": "Auctions", "supplier": "Short"}, {"text": "British Car Auctions", "supplier": "BCA"}]
    assert cb.match_supplier("BRITISH CAR AUCTIONS LIMITED", names) == ("BCA", "British Car Auctions")
    assert cb.match_supplier("Nothing here", names) == (None, None)


def test_clean_supplier_names_later_row_wins_and_short_text_is_refused():
    rows = [{"text": "BCA Ltd", "supplier": "Old"}, {"text": "bca ltd", "supplier": "BCA"}, {"text": "", "supplier": ""}]
    assert cb.clean_supplier_names(rows) == [{"text": "bca ltd", "supplier": "BCA"}]
    with pytest.raises(ValueError):
        cb.clean_supplier_names([{"text": "ab", "supplier": "X"}])
    with pytest.raises(ValueError):
        cb.clean_supplier_names([{"text": "Some Text", "supplier": ""}])


def test_suggest_suppliers_puts_company_names_first_and_leaves_ours_out():
    text = ("MOTOR DEPOT LTD  INVOICE 244060\n"
            "Description of goods\n"
            "Registered Office: Hook\n"
            "SHOREHAM VEHICLE SALES LIMITED  VAT Number: 776009712\n"
            "info@shoreham.example\n")
    out = cb.suggest_suppliers(text, cb.DEFAULT_OWN_NAMES)
    assert out[0] == {"text": "SHOREHAM VEHICLE SALES LIMITED", "supplier": "Shoreham Vehicle Sales"}
    assert not any("MOTOR DEPOT" in s["text"].upper() or "@" in s["text"] for s in out)


def test_layout_keeps_the_text_and_says_when_no_supplier_matched():
    page = ("   Unknown Trade Cars Ltd        Invoice No: 77\n"
            "   Description                       Net       VAT     Gross\n"
            "   AB12CDE FORD FOCUS           1,000.00    200.00  1,200.00\n")
    out = _layout([page])
    assert (out["supplier_name"], out["supplier_text"], out["supplier_matched"]) == (None, None, False)
    assert "Unknown Trade Cars Ltd" in out["found_text"]
    assert cb.suggest_suppliers(out["found_text"], [])[0]["supplier"] == "Unknown Trade Cars"


def test_add_sheet_fields_keeps_what_is_there_and_adds_the_rest():
    out = cb.add_sheet_fields([{"key": "rrp", "label": "RRP", "type": "money"}, {"key": "my_notes", "label": "Notes", "type": "text"}])
    assert [f["key"] for f in out] == ["rrp", "my_notes", "vendor", "sat_nav", "spec", "replaceable"]
    assert next(f for f in out if f["key"] == "sat_nav")["type"] == "yesno"


def test_clean_custom_fields_keeps_yes_no():
    assert cb.clean_custom_fields([{"label": "Sat Nav", "type": "yesno", "required": True}])[0] == {
        "key": "sat_nav", "label": "Sat Nav", "type": "yesno", "required": True}


def test_clean_custom_fields_keeps_suppliers_and_drops_blanks():
    out = cb.clean_custom_fields([{"label": "Lot", "type": "text", "suppliers": [" Enterprise ", "", "Motability"]},
                                  {"label": "Spec", "type": "text", "suppliers": ["  "]}])
    assert out[0]["suppliers"] == ["Enterprise", "Motability"]
    assert "suppliers" not in out[1]


def test_field_applies_to_its_suppliers_only():
    assert cb.field_applies({"key": "spec"}, "Enterprise")
    assert cb.field_applies({"key": "spec", "suppliers": []}, "")
    assert cb.field_applies({"key": "lot", "suppliers": ["Enterprise"]}, " enterprise ")
    assert not cb.field_applies({"key": "lot", "suppliers": ["Enterprise"]}, "Motability")
    assert not cb.field_applies({"key": "lot", "suppliers": ["Enterprise"]}, "")


MAG_PLAIN_PAGE = """SALES INVOICE
Motor Depot T/A Car Supermarket
Greasbrough Rd,
Rotherham
S61 4QQ
Invoice No.: 30377
Date/Tax Point: 04/11/2026
Ref No Description Amount VAT@20% Total
2610/01148
LOT 129
Rotherham Physical & Online Sale 04 Nov 2026
Reg No: NL73RMY | Chassis No: WAUZZZGB8RR002951
Vehicle: Audi A1 SPORTBACK 30 TFSI 110 S Line 5dr...
Mileage: 26804 Warranted | V5 Status: Yes | Keys: 2 | VAT: Qualifying
x
13,416.67 2,683.33 16,100.00
2610/01148 Buyer On Line Fee 30.00 6.00 36.00
2610/01148 MAG Inspection Check 30.00 6.00 36.00
2610/01148 Storage Charge 0.00 0.00 0.00
2610/01148 Taxi & Salvage 4.00 0.80 4.80
2610/01148 Purchase Premium 464.00 92.80 556.80
2610/01148 Delivery 0.00 0.00 0.00
VEHICLE LOCATION: ROTHERHAM , S61 4QQ 13,944.67 2,788.93 16,733.60
Balance:
£16,733.60
"""


def test_priced_rows_finds_the_car_fees_and_total_from_the_sums():
    out = cb.normalise_extract(cb.parse_layout([MAG_PLAIN_PAGE], NAMES, cb.DEFAULT_FEE_WORDS, cb.DEFAULT_SUPPLIER_NAMES))
    assert (out["invoice_number"], out["invoice_date"]) == ("30377", "2026-11-04")
    assert (out["invoice_net"], out["invoice_vat"], out["invoice_gross"]) == (13944.67, 2788.93, 16733.60)
    assert len(out["vehicles"]) == 1
    v = out["vehicles"][0]
    assert (v["vrm"], v["mileage"], v["vin"]) == ("NL73RMY", 26804, "WAUZZZGB8RR002951")
    assert (v["vehicle_net"], v["vehicle_vat"], v["vehicle_gross"]) == (13416.67, 2683.33, 16100.0)
    assert [f["description"] for f in v["fees"]] == ["Buyer On Line Fee", "MAG Inspection Check", "Taxi & Salvage", "Purchase Premium"]
    assert (v["fees_net"], v["fees_vat"], v["fees_gross"]) == (528.0, 105.6, 633.6)
    assert cb.purchase_total(v) == 16733.6


def test_priced_rows_two_cars_with_vrm_below_the_prices():
    page = """Item Amount VAT@20% Total
Lot 1 9,000.00 1,800.00 10,800.00
Reg No: NL73RMY
Buyer Fee 100.00 20.00 120.00
Lot 2 5,000.00 0.00 5,000.00
Reg No: HJ73LYV
Buyer Fee 50.00 10.00 60.00
Total 14,150.00 1,830.00 15,980.00
"""
    v = cb.normalise_extract(cb.parse_layout([page], NAMES, cb.DEFAULT_FEE_WORDS, []))["vehicles"]
    assert [x["vrm"] for x in v] == ["NL73RMY", "HJ73LYV"]
    assert [x["fees_gross"] for x in v] == [120.0, 60.0]
    assert (v[1]["vehicle_net"], v[1]["vehicle_vat"], v[1]["vehicle_gross"]) == (5000.0, 0.0, 5000.0)


MOTABILITY_EMAIL = """<html><head><style>p {color: red}</style></head><body>
<h2>Purchase confirmation</h2><p>Thank you for buying through mfldirect - here's your receipt and order details.</p>
<table><tr><td>Company name:</td><td>Motor Depot Ltd</td></tr>
<tr><td>Vehicle registration:</td><td>HJ73LYV</td></tr>
<tr><td>Make:</td><td>Renault</td></tr><tr><td>Model:</td><td>AUSTRAL</td></tr>
<tr><td>Variant:</td><td>AUSTRAL E-Tech FHEV Techno Esprit Alpine 5dr Auto SUV</td></tr>
<tr><td>Colour:</td><td>Grey</td></tr><tr><td>Current mileage:</td><td>23,398</td></tr>
<tr><td>Vehicle price:</td><td>&pound;16,650.00</td></tr><tr><td>Delivery cost:</td><td>&pound;185.00</td></tr>
<tr><td>Delivery VAT:</td><td>&pound;37.00</td></tr><tr><td>Total costs:</td><td>&pound;16,872.00</td></tr></table>
<p>Delivery location</p><p>Motor Depot Ltd</p><p>Stockholm Road</p><p>Hull</p><p>HU7 0XW</p></body></html>"""


def test_labelled_email_is_read_with_no_textract_and_is_margin():
    def no_scan():
        raise AssertionError("an email must not go to Textract")
    supplier_names = [{"text": "mfldirect", "supplier": "Motability"}]
    out, read_by = cb.read_invoice("confirmation.html", MOTABILITY_EMAIL.encode(), NAMES, cb.DEFAULT_FEE_WORDS, supplier_names, on_scan=no_scan)
    assert read_by == "text"
    assert (out["supplier_name"], out["invoice_gross"]) == ("Motability", 16872.0)
    v = out["vehicles"][0]
    assert (v["vrm"], v["mileage"]) == ("HJ73LYV", 23398)
    assert v["supplier_description"] == "Renault AUSTRAL AUSTRAL E-Tech FHEV Techno Esprit Alpine 5dr Auto SUV"
    assert (v["vehicle_net"], v["vehicle_vat"], v["vehicle_gross"]) == (16650.0, 0.0, 16650.0)
    assert (v["fees_net"], v["fees_vat"], v["fees_gross"]) == (185.0, 37.0, 222.0)
    assert cb.vat_status(v) == "Margin"
    assert cb.purchase_total(v) == 16872.0


def test_saved_eml_is_read_with_the_date_it_was_received():
    data = ("From: mfldirect <a@example.com>\r\nDate: Wed, 04 Nov 2026 10:00:00 +0000\r\nSubject: Purchase confirmation\r\n"
            "MIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\n\r\n" + MOTABILITY_EMAIL).encode()
    out, read_by = cb.read_invoice("confirmation.eml", data, NAMES, cb.DEFAULT_FEE_WORDS, [{"text": "mfldirect", "supplier": "Motability"}])
    assert (read_by, out["invoice_date"], out["vehicles"][0]["vrm"]) == ("text", "2026-11-04", "HJ73LYV")


def test_margin_car_has_net_zero_vat_and_the_same_gross():
    for given in ({"vehicle_net": "9000.00"}, {"vehicle_gross": "9000.00"}, {"vehicle_net": "9000.00", "vehicle_vat": "0.00"}):
        v = cb.normalise_extract({"vehicles": [{"vrm": "NL73RMY", **given}]})["vehicles"][0]
        assert (v["vehicle_net"], v["vehicle_vat"], v["vehicle_gross"]) == (9000.0, 0.0, 9000.0)
    v = cb.normalise_extract({"vehicles": [{"vrm": "NL73RMY"}]})["vehicles"][0]
    assert v["vehicle_net"] is None and v["vehicle_vat"] is None


def test_form_labels_never_match_a_table_column():
    assert cb.match_field("Make", NAMES) is None
    assert cb.match_field("Registration", NAMES) == "vrm"


def test_clean_custom_value_fits_the_field_type():
    assert cb.clean_custom_value({"type": "yesno"}, "Yes") == "Yes"
    assert cb.clean_custom_value({"type": "yesno"}, "") is None
    assert cb.clean_custom_value({"type": "money"}, "£1,234.50") == 1234.5
    assert cb.clean_custom_value({"type": "text"}, " V5 To Follow ") == "V5 To Follow"
    for field, value in (({"type": "yesno", "label": "A"}, "Maybe"), ({"type": "money", "label": "B"}, "abc")):
        with pytest.raises(ValueError):
            cb.clean_custom_value(field, value)


FIELDS = cb.add_sheet_fields(list(cb.DEFAULT_CUSTOM_FIELDS))


def test_default_table_columns_follow_the_buying_sheet():
    cols = cb.read_table_columns([], FIELDS)
    assert [c["column"] for c in cols] == cb.DEFAULT_TABLE_ORDER
    assert next(c for c in cols if c["column"] == "PURCHASE_TOTAL")["label"] == "Purchase Price"
    assert next(c for c in cols if c["column"] == "CUSTOM:RRP")["format"] == "gbp"


def test_clean_table_columns_drops_unknown_and_repeated_columns():
    out = cb.clean_table_columns([{"column": "VRM", "label": "", "format": "bad"}, {"column": "VRM"},
                                  {"column": "PASSWORD"}, {"column": "CUSTOM:GONE"}, {"column": "CUSTOM:SPEC", "label": "My Spec"}], FIELDS)
    assert [(c["column"], c["label"], c["format"]) for c in out] == [("VRM", "Reg", "text"), ("CUSTOM:SPEC", "My Spec", "text")]


def test_a_new_custom_field_goes_on_the_table():
    new = FIELDS + [{"key": "keys", "label": "Keys", "type": "number", "required": True}]
    out = cb.add_new_field_columns([{"column": "VRM"}], FIELDS, new)
    assert [c["column"] for c in out] == ["VRM", "CUSTOM:KEYS"]
    assert cb.add_new_field_columns([{"column": "VRM"}], new, new) == [{"column": "VRM"}]


def test_table_rows_adds_the_two_worked_out_figures():
    row = cb.table_rows([{"vehicle_vat": None, "vehicle_gross": 100, "fees_gross": 20, "fees_detail": []}])[0]
    assert (row["vat_status"], row["purchase_total"]) == ("Margin", 120.0)
    assert "fees_detail" not in row


def test_invoice_date_is_not_a_cars_registration_date():
    assert cb._INVOICE_DATE.search("Registration Date: 31/05/2023") is None
    assert cb._INVOICE_DATE.search("Reg. Date: 15/08/2022") is None
    assert cb._INVOICE_DATE.search("Date: 29/10/2026").group(1) == "29/10/2026"


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
