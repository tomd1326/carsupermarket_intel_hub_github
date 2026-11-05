"""Weekly Business Review Report view: reading its data (routers/wbr_report_data.py)
and adding its SharePoint files (utils/sharepoint_sources.py).

The risks covered: the IFRS lines the wrong way round, a repeated volume label
landing on the wrong row, a missing recon column, a first download breaking the
page, and the new files never reaching PROD because the seed list only runs on an
empty table.

Run from source_code/router/backend:
    poetry run pytest tests/test_wbr_report_data.py
"""

import datetime
import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from routers import wbr_report_data as d
from utils import sharepoint_sources as sps

# 2026-10 in MANAGEMENT_ACCOUNTS, checked against the Sheet's data_accounts column.
OCT_2026 = {
    "b2c_units": 1800, "gpu_m": 760.30, "gpu_s": 770.94, "gpu_b": 22.44, "revenue": 30482.52,
    "marketing_costs": -510, "sga_costs": -1574.05, "hq_costs": -408.59, "depreciation": -106.44,
    "ifrs_16_ebit": -149, "ifrs_16_ebitda": 183, "interest_tax": -50.12, "ebit": 231.55, "ebitda": 486.99,
}

# Columns A and B of the Performance tab, as read from the file on 2026-11-02.
PERFORMANCE = """Enquiry|Web Sessions
|Web Leads
|Classifieds
|Valuations
|Calls
|
Orders|Orders
|Cancelled
|Cancellation %
|Net Orders
|
Delivered|Delivered
|Delivered GPU
|Delivered DIS
| Time to Delivery
|True DIS
|
MTD DOC Orders|DOC Current Month
|DOC Previous Month
|DOC Previous Year
|
Judge Service| Delivered
| Responses
|
Stock New|Retail (B2C)
|Stock in # of vehicles
|Trade hold
|B2B
|C2B
|PX
|Consignment
|Upstream stock Total
|Sutton Fields
|Goole
|Sites
|Subcontractor offsite
|In prep Total
|B2B
|C2B
|PX
|Consignment
|To price
|On sale stock Total
|Ordered
|Sleeping stock (#)
|Trade hold
|B2B
|C2B
|PX
|Consignment
|Upstream stock
|Sutton Fields
|Goole
|Sites
|Subcontractor offsite
|In prep
|B2B
|C2B
|PX
|Consignment
|On sale stock
|Ordered
|Trade (B2B)
|Stock in # of vehicles
|Courtesy Car
|On sale stock
|Other stock
|Sleeping stock
|Courtesy Car
|On sale stock
|Other stock
|
Recon Vehicles| Goole
|Sutton Fields
|Sites
|Total
|In Prep %
|
Cars Bought|B2B
|C2B
|PX
|Consigment
|Total
|In Prep %
|
Marketing & Reviews|Marketing Spend
|NPS
|Google Score
|Marketing Target
|
B2B|Delivered
|Total Margin
|PPU
|
Stock|For sale
|For sale %
|To Price
|Sold not Delivered
|In Prep
|Awaiting Arrival
|Trade
|Held
|Courtesy
|Total
|Avg DIS
|
Old Stock (not including Courtesy)|Older than 60 days
|Old %
|Older than 60 RETAIL
|Old Retail %"""


def performance_rows():
    """The tab with each value row's number in both week columns."""
    rows = [["Weekly Performance (w/e)", "", "2026-11-01", "2026-10-25"]]
    for n, line in enumerate(PERFORMANCE.splitlines(), 2):
        a, b = line.split("|")
        rows.append([a, b, n, n * 10])
    return rows


def accounts(**changes):
    rows = [(k, "2026-10-01", v) for k, v in OCT_2026.items()]
    rows.append(("b2c_units", "2025-10-01", 1638))
    rows.append(("not_a_line", "2026-10-01", 1))
    return d.accounts_lines(rows)


def test_accounts_lines_match_the_sheet():
    line = accounts()["2026-10"]
    assert round(line["GPU-M k£"]) == 1369
    assert round(line["GPU-S k£"]) == 1388
    assert round(line["GPU-B k£"]) == 40
    assert round(line["GPU-MSB k£"]) == 2797
    assert round(line["GPU-MS (£/unit)"]) == 1531
    assert round(line["GPU-MSB (£/unit)"]) == 1554
    assert round(line["Revenue (£/unit)"]) == 16935
    assert round(line["COCA £/u"]) == -283
    assert round(line["EBITDA %"], 3) == 0.016
    assert round(line["YoY growth B2C %"], 3) == 0.099


def test_ifrs_lines_give_finances_ebitda_and_ebit():
    line = accounts()["2026-10"]
    assert line["IFRS impacts to EBIT k£"] == 183
    assert line["IFRS impacts to EBIT/EBITDA k£"] == -149
    ebitda = (line["GPU-MSB k£"] + line["Sales costs k£"] + line["HQ costs k£"]
              + line["IFRS impacts to EBIT k£"] + line["COCA k£"])
    ebit = ebitda + line["Depreciation k£"] + line["IFRS impacts to EBIT/EBITDA k£"]
    assert round(ebitda) == round(line["EBITDA k£"])
    assert round(ebit) == round(line["EBIT k£"])


def test_accounts_lines_with_missing_figures_are_blank():
    line = d.accounts_lines([("gpu_m", "2026-10-01", 760)])["2026-10"]
    assert line["GPU-M k£"] is None and line["GPU-MSB (£/unit)"] is None and line["YoY growth B2C %"] is None


RECON_HEAD = ["Date", "Sites Live", "Hull Sent to Trans.", "Goole Sent to Trans.", "Total Live", "Cars Bought", "DOC Orders"]


def test_recon_frame_reads_by_header_and_works_out_the_week():
    values = [RECON_HEAD, ["2025-10-13", 0, 66, 27, 93, 97, 55], ["", "", "", "", "", "", ""]]
    rows, warnings = d.recon_frame(values, sps.DEFAULT_HEADERS["wbr_recon"])
    assert warnings == []
    assert len(rows) == 1
    assert rows[0]["year_month"] == "2025-10"
    assert rows[0]["week_end"] == "2025-10-19"
    assert rows[0]["Hull Sent to Trans."] == 66
    assert rows[0]["Goole Sent to Trans."] == 27


def test_recon_frame_copes_with_moved_columns_and_case():
    values = [["goole sent  to trans.", "DATE"], [27, "2025-10-13"]]
    rows, _ = d.recon_frame(values, {"Date": ["Date"], "Goole Sent to Trans.": ["Goole Sent to Trans."]})
    assert rows[0]["Goole Sent to Trans."] == 27


def test_recon_frame_warns_about_a_missing_header():
    rows, warnings = d.recon_frame([RECON_HEAD[:-1], ["2025-10-13", 0, 66, 27, 93, 97]], sps.DEFAULT_HEADERS["wbr_recon"])
    assert len(rows) == 1
    assert any("DOC Orders" in w for w in warnings)


def test_recon_frame_without_a_date_column_is_empty():
    rows, warnings = d.recon_frame([["Sites Live"], [1]], sps.DEFAULT_HEADERS["wbr_recon"])
    assert rows == []
    assert any("Date" in w for w in warnings)


def test_a_first_download_is_empty_not_an_error():
    assert d.recon_frame(None, sps.DEFAULT_HEADERS["wbr_recon"]) == ([], [])
    assert d.volume_frame(None) == ({"weeks": [], "rows": {}}, [])


def test_volume_every_row_has_its_own_key():
    frame, warnings = d.volume_frame(performance_rows())
    assert warnings == []
    assert frame["weeks"] == ["2026-10-25", "2026-11-01"]
    # Every labelled row bar the six section and measure headings under Stock New.
    labelled = sum(1 for line in PERFORMANCE.splitlines() if line.split("|")[1].strip())
    assert len(frame["rows"]) == labelled - 6


@pytest.mark.parametrize("key", [
    ("Enquiry", "Web Sessions"), ("Orders", "Orders"), ("Orders", "Cancelled"),
    ("Marketing & Reviews", "NPS"), ("Marketing & Reviews", "Google Score"),
    ("Stock", "For sale %"), ("Old Stock (not including Courtesy)", "Older than 60 days"),
    ("Recon Vehicles", "Goole"), ("Recon Vehicles", "Sutton Fields"), ("Recon Vehicles", "Sites"),
    ("Recon Vehicles", "Total"), ("Cars Bought", "B2B"), ("Cars Bought", "Consignment"), ("Cars Bought", "Total"),
])
def test_volume_lookups_used_by_the_report_resolve(key):
    frame, _ = d.volume_frame(performance_rows())
    assert d.volume_key(*key) in frame["rows"]


def test_volume_repeated_labels_land_in_the_right_block():
    frame, _ = d.volume_frame(performance_rows())
    rows = frame["rows"]
    value = lambda *key, **parts: rows[d.volume_key("Stock New", *key, section="retail", **parts)]["2026-11-01"]
    assert value("B2B", measure="units", block="upstream") == 29
    assert value("B2B", measure="units", block="on_sale") == 39
    assert value("Sutton Fields", measure="units", block="in_prep") == 34
    assert value("Upstream stock Total", measure="units", block="upstream") == 33
    assert value("B2B", measure="sleeping", block="upstream") == 48
    assert value("B2B", measure="sleeping", block="on_sale") == 58
    assert value("Trade hold", measure="units") == 28
    assert value("Ordered", measure="units") == 45
    assert value("Ordered", measure="sleeping") == 63


def test_volume_trade_rows_are_kept_apart():
    frame, _ = d.volume_frame(performance_rows())
    rows = frame["rows"]
    assert rows[d.volume_key("Stock New", "On sale stock", section="trade", measure="units")]["2026-11-01"] == 67
    assert rows[d.volume_key("Stock New", "On sale stock", section="trade", measure="sleeping")]["2026-11-01"] == 71


def test_volume_without_its_header_warns():
    frame, warnings = d.volume_frame([["Something else", "", "2026-11-01"]])
    assert frame["rows"] == {}
    assert warnings


def test_month_end_proxy():
    weeks = ["2026-09-27", "2026-10-04", "2026-10-25", "2026-11-01"]
    assert d.month_end_proxy(weeks, "2026-10") == "2026-11-01"   # 1 day after 31 Oct
    assert d.month_end_proxy(weeks, "2026-09") == "2026-10-04"   # 4 days after 30 Sep
    assert d.month_end_proxy(["2026-09-27"], "2026-09") == "2026-09-27"   # 3 days before
    assert d.month_end_proxy(["2026-01-04"], "2026-09") is None


META = {"update_date": "2026-11-01", "current_month": "2026-11", "n1_months": {"2026-08": "2025-08", "2026-11": "2025-11"}}


def test_source_summary_warns_about_what_the_dates_need():
    recon = [{"date": "2025-10-13", "Total Live": 93}, {"date": "2026-10-30", "Total Live": 80},
             {"date": "2026-11-10", "Total Live": 0}]   # zeros for dates still to come
    found, warnings = d.source_summary(META, {"2026-10": {}}, recon, {"weeks": ["2026-10-25"], "rows": {}})
    assert found["accounts_months"] == ["2026-10", "2026-10"]
    assert found["recon_dates"] == ["2025-10-13", "2026-10-30"]
    text = " ".join(warnings)
    assert "no figures for 2026-11" in text
    assert "stops at 2026-10-30" in text
    assert "starts at 2025-10-13" in text
    assert "week ending 2026-11-01" in text


def test_add_sources_turns_a_failed_read_into_a_warning(monkeypatch):
    def broken(*args):
        raise RuntimeError("no access")
    monkeypatch.setattr(d, "load_accounts", broken)
    monkeypatch.setattr(d, "load_margin", broken)
    monkeypatch.setattr(d, "load_purchases", lambda *a: pd.DataFrame())
    monkeypatch.setattr(d, "load_daily_summary", lambda *a: pd.DataFrame())
    monkeypatch.setattr(d, "load_recon", lambda: ([], ["Recon is still downloading. Reload shortly."]))
    monkeypatch.setattr(d, "load_volume", lambda: ({"weeks": [], "rows": {}}, []))
    report = {"meta": {**META, "warnings": []}}
    d.add_sources(report)
    assert any("Accounts could not be read: no access" in w for w in report["meta"]["warnings"])
    assert any("Sales data could not be read" in w for w in report["meta"]["warnings"])
    assert any("still downloading" in w for w in report["meta"]["warnings"])


# ── Margin ──

def margin_row(**changes):
    base = {c: 0 for c in d._MARGIN_NUMBERS}
    base.update({
        "invoice_status": "STATUS_PAID", "order_acknowledged_month": "2026-10",
        "order_acknowledged_date": "2026-10-20", "invoice_posted_month": "2026-10",
        "invoice_posted_date": "2026-10-22", "supplier_name": "A", "supplier_channel": "B2B2C",
        "finance_company": "F", "deal_site_clean": "Hessle", "payment_type": "Rate",
        "delivery_volume": 1, "purchase_price_ex_vat": 12000, "discount_ex_vat": 50, "margin_vat_cost": 100,
        "finance_subsidy": 200, "chassis_margin_after_subsidy": 1500, "recon_cost": 600, "other_costs": 40,
        "total_product_margin": 300, "finance_commission": 500, "std_late_costs": -10, "std_write_downs": -5,
        "std_finance_debit_backs": -20, "gpu_b": 70, "product_margin_warranty": 90,
        "gpu_m": 845, "gpu_s": 780, "gpu_ms": 1625, "gpu_msb": 1695,
    })
    return {**base, **changes}


def margin(*rows, cutoff="2026-10-25"):
    return d.margin_frame(pd.DataFrame(rows), cutoff)


def test_margin_splits_the_snowflake_gpu_m_into_its_costs():
    row = margin(margin_row()).iloc[0]
    assert row["recon_cost"] == -600
    assert row["other_costs"] == -40
    assert row["purchase_price_ex_vat"] == -12000
    assert row["finance_subsidy"] == -200
    assert row["total_cos"] == -655          # 845 - 1500
    assert row["costs_unmatched"] == 0       # -655 against -600 - 40 - 10 - 5
    assert row["gpu_m"] == 845
    assert row["gpu_s_cost"] == -20
    assert row["gpu_s"] == 780
    assert row["gpu_ms"] == 1625
    assert row["gpu_msb"] == 1695
    assert row["volume_warranty"] == 1


@pytest.mark.parametrize("recon, shown, unmatched", [(None, 0, -600), (0, 0, -600), (99, -99, -501)])
def test_no_guess_is_made_for_a_missing_or_small_recon_cost(recon, shown, unmatched):
    row = margin(margin_row(recon_cost=recon)).iloc[0]
    assert row["recon_cost"] == shown
    assert row["costs_unmatched"] == unmatched
    assert row["gpu_m"] == 845


def test_blank_costs_are_treated_as_zero():
    row = margin(margin_row(other_costs=None, std_late_costs=None)).iloc[0]
    assert row["other_costs"] == 0
    assert row["total_cos"] == -655
    assert row["costs_unmatched"] == -50


def test_apex_costs_not_in_autoview_show_as_the_difference():
    row = margin(margin_row(gpu_m=800)).iloc[0]
    assert row["total_cos"] == -700
    assert row["costs_unmatched"] == -45


def test_a_blank_chassis_margin_leaves_gpu_m_and_total_costs_blank():
    row = margin(margin_row(chassis_margin_after_subsidy=None, gpu_m=None)).iloc[0]
    assert pd.isna(row["gpu_m"]) and pd.isna(row["total_cos"])


@pytest.mark.parametrize("price", [None, 0])
def test_cars_with_no_purchase_price_are_dropped(price):
    assert len(margin(margin_row(purchase_price_ex_vat=price))) == 0


def test_a_delivery_after_the_review_counts_as_not_delivered():
    row = margin(margin_row(invoice_posted_date="2026-10-27", invoice_posted_month="2026-10")).iloc[0]
    assert row["deliveries"] == 0
    assert row["invoice_posted_date"] is None and row["invoice_posted_month"] is None
    assert row["delivery_week_end"] is None
    assert margin(margin_row()).iloc[0]["deliveries"] == 1


def test_an_order_after_the_review_is_dropped():
    assert len(margin(margin_row(order_acknowledged_date="2026-10-26"))) == 0


def test_an_order_from_before_the_window_delivered_inside_it_is_kept():
    row = margin(margin_row(order_acknowledged_date="2024-12-01", order_acknowledged_month="2024-12")).iloc[0]
    assert row["deliveries"] == 1
    assert row["delivery_week_end"] == "2026-10-25"


def test_undelivered_and_cancelled_orders_are_not_deliveries():
    row = margin(margin_row(invoice_status="STATUS_CANCELLED", invoice_posted_date=None, invoice_posted_month=None,
                            delivery_volume=0)).iloc[0]
    assert row["deliveries"] == 0
    assert row["order_week_end"] == "2026-10-25"


def test_week_end_groups_new_year_by_date():
    ends = d._week_end(pd.Series(["2025-12-29", "2026-01-04", "2026-01-05", None]))
    assert list(ends) == ["2026-01-04", "2026-01-04", "2026-01-11", None]


def test_gpu_s_lines_add_up():
    products = {"product_margin_warranty": 90, "product_margin_gap_rti_insurance": 10,
                "product_margin_premia_gap_rti_insurance": 20, "product_margin_smart_plus_insurance": 30,
                "product_margin_service_mot_plan": 40, "product_margin_services_mots_carried_over": 5,
                "product_margin_ceramic_vehicle_protection": 50, "product_margin_anti_bacterial_protection": 15,
                "product_margin_ev_charger": 25, "product_margin_mats": 15}
    row = margin(margin_row(**products, total_product_margin=sum(products.values()), product_margin_admin_fee=99)).iloc[0]
    assert sum(row[c] for c in products) == row["total_product_margin"]
    assert row["total_product_margin"] + row["finance_commission"] + row["gpu_s_cost"] == row["gpu_s"]


def test_autoview_costs_usable():
    assert not d.autoview_costs_usable("2025-10", "2025-11")
    assert d.autoview_costs_usable("2025-11", "2025-11")


def test_purchases_group_new_year_weeks_by_date():
    frame = pd.DataFrame([{"purchase_date": "2025-12-30", "date_first_advertised": None, "gpu_m": "12.5"}])
    out = d.purchases_frame(frame).iloc[0]
    assert out["purchase_week_end"] == "2026-01-04"
    assert out["advertised_week_end"] is None
    assert out["gpu_m"] == 12.5


def test_short_windows_warn():
    meta = {"n1_months": {"2026-08": "2025-08"}}
    margin_data = pd.DataFrame([{"order_acknowledged_date": "2026-08-05", "order_acknowledged_month": "2026-08"}])
    purchases = pd.DataFrame([{"purchase_month": "2025-09"}])
    out, warnings = d.snowflake_summary(meta, margin_data, purchases, pd.DataFrame())
    assert out["margin_rows"] == 1
    assert len(warnings) == 2


# ── Cache ──

class FakeS3:
    """One bucket, shared by every Lambda instance in the test."""

    def __init__(self):
        self.objects = {}
        self.version = 0
        self.gets = 0

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise RuntimeError("An error occurred (404) when calling the HeadObject operation")
        body, etag, modified = self.objects[Key]
        return {"ETag": etag, "LastModified": modified}

    def get_object(self, Bucket, Key):
        self.gets += 1
        body, etag, _ = self.objects[Key]

        class Stream:
            def read(self):
                return body
        return {"Body": Stream(), "ETag": etag}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.version += 1
        self.objects[Key] = (Body, f"v{self.version}", datetime.datetime.now(datetime.timezone.utc))
        return {"ETag": f"v{self.version}"}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(d, "_s3", lambda: fake)
    monkeypatch.setattr(d, "_memory", {})
    return fake


def builder(values):
    def build():
        values.append(len(values) + 1)
        return {"built": values[-1]}
    return build


def test_cache_builds_once_then_reuses(s3):
    built = []
    assert d._cached("x", builder(built)) == {"built": 1}
    assert d._cached("x", builder(built)) == {"built": 1}
    assert built == [1] and s3.gets == 0


def test_a_second_instance_reads_s3_not_snowflake(s3, monkeypatch):
    built = []
    d._cached("x", builder(built))
    monkeypatch.setattr(d, "_memory", {})   # another Lambda instance
    assert d._cached("x", builder(built)) == {"built": 1}
    assert built == [1] and s3.gets == 1


def test_reload_reaches_other_instances(s3, monkeypatch):
    built = []
    d._cached("x", builder(built))
    other = dict(d._memory)                 # another instance holding the old value
    assert d._cached("x", builder(built), refresh=True) == {"built": 2}
    monkeypatch.setattr(d, "_memory", other)
    assert d._cached("x", builder(built)) == {"built": 2}


def test_cache_older_than_an_hour_is_rebuilt(s3):
    built = []
    d._cached("x", builder(built))
    key = d._cache_key("x")
    body, etag, _ = s3.objects[key]
    s3.objects[key] = (body, etag, datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2))
    assert d._cached("x", builder(built)) == {"built": 2}


def test_s3_trouble_still_returns_the_data(monkeypatch):
    monkeypatch.setattr(d, "_memory", {})

    def broken():
        raise RuntimeError("no bucket")
    monkeypatch.setattr(d, "_s3", broken)
    assert d._cached("x", lambda: {"built": 1}) == {"built": 1}


def test_a_frame_survives_the_cache(s3, monkeypatch):
    frame = margin(margin_row(), margin_row(invoice_posted_date="2026-10-27"))
    build = lambda: frame
    d._cached("m", build, False, d._frame_to_body, d._frame_from_body)
    monkeypatch.setattr(d, "_memory", {})
    back = d._cached("m", build, False, d._frame_to_body, d._frame_from_body)
    assert list(back.columns) == list(frame.columns)
    assert back["gpu_m"].tolist() == frame["gpu_m"].tolist()
    assert back["deliveries"].sum() == 1
    assert back.loc[1, "invoice_posted_date"] is None


# ── The SharePoint files ──

class FakeCursor:
    def __init__(self, existing):
        self.existing = existing
        self.inserted = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        if "INSERT INTO sharepoint_sources" in sql:
            self.inserted.append(params[0])

    def fetchone(self):
        return (self.existing,)


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass


def ensure(monkeypatch, existing):
    monkeypatch.setattr(sps, "_tables_ready", False)
    cursor = FakeCursor(existing)
    sps._ensure_tables(FakeConn(cursor))
    return cursor.inserted


def test_new_files_are_added_to_a_table_that_already_has_rows(monkeypatch):
    assert ensure(monkeypatch, existing=4) == ["wbr_recon", "wbr_volume"]


def test_an_empty_table_gets_every_file(monkeypatch):
    inserted = ensure(monkeypatch, existing=0)
    assert inserted[-2:] == ["wbr_recon", "wbr_volume"]
    assert "cars_bought" in inserted


@pytest.mark.parametrize("key", ["wbr_recon", "wbr_volume"])
def test_new_files_can_be_saved_in_settings_but_not_deleted(key):
    src = next(s for s in sps._ADDED_SOURCES if s["source_key"] == key)
    clean = sps._clean({**src, "enabled": True}, existing=src)
    assert clean["file_type"] == key
    assert clean["max_col"] == src["max_col"]
    assert key in sps.FIXED_KEYS
    assert key in sps.FILE_TYPES
    with pytest.raises(ValueError):
        sps.delete_source(key, "someone")


def test_recon_headers_can_be_replaced_in_settings():
    src = next(s for s in sps._ADDED_SOURCES if s["source_key"] == "wbr_recon")
    clean = sps._clean({**src, "column_names": {"Sites Live": "Sites, Site Total"}}, existing=src)
    assert clean["column_names"]["Sites Live"] == ["Sites", "Site Total"]
