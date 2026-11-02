"""Cars Bought invoices from SharePoint. Run from source_code/router/backend:
    poetry run pytest tests/test_cars_bought_inbox.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import pytest

from utils import cars_bought_inbox as inbox
from utils import sharepoint_client as sp


def _file(id, name="a.pdf", saved="2026-11-02T09:00:00Z"):
    return {"id": id, "name": name, "lastModifiedDateTime": saved}


def test_wanted_files_skips_seen_and_lock_files():
    files = [_file("1"), _file("2"), _file("3", "~$sheet.xlsx")]
    assert [f["id"] for f in inbox.wanted_files(files, {"1"}, "", 20)] == ["2"]


def test_wanted_files_skips_files_saved_before_the_date():
    files = [_file("old", saved="2026-10-31T23:00:00Z"), _file("same", saved="2026-11-01T00:00:00Z")]
    assert [f["id"] for f in inbox.wanted_files(files, set(), "2026-11-01", 20)] == ["same"]


def test_wanted_files_takes_the_oldest_up_to_the_limit():
    files = [_file("c", saved="2026-11-03T00:00:00Z"), _file("a", saved="2026-11-01T00:00:00Z"),
             _file("b", saved="2026-11-02T00:00:00Z")]
    assert [f["id"] for f in inbox.wanted_files(files, set(), "", 2)] == ["a", "b"]


def test_folder_counts_says_what_happened_to_every_file():
    files = [_file("seen"), _file("old", saved="2026-10-01T00:00:00Z"), _file("a"), _file("b"), _file("lock", "~$a.xlsx")]
    wanted = inbox.wanted_files(files, {"seen"}, "2026-11-01", 1)
    assert inbox.folder_counts(files, {"seen"}, "2026-11-01", wanted) == {
        "in_folder": 4, "already_read": 1, "too_old": 1, "next_check": 1}


NAMES = [{"text": "Scott Group Ltd", "supplier": "Scott Group"}]


def test_apply_supplier_names_fills_in_a_new_match():
    r = {"found_text": "SCOTT GROUP LTD invoice", "supplier_name": None, "supplier_matched": False}
    assert inbox.apply_supplier_names(r, NAMES, {"scott group": 9}) is True
    assert (r["supplier_name"], r["supplier_text"], r["supplier_matched"], r["supplier_id"]) == ("Scott Group", "Scott Group Ltd", True, 9)
    assert inbox.apply_supplier_names(r, NAMES, {"scott group": 9}) is False


def test_apply_supplier_names_clears_a_match_that_was_deleted():
    r = {"found_text": "SCOTT GROUP LTD", "supplier_name": "Scott Group", "supplier_text": "Scott Group Ltd", "supplier_matched": True}
    assert inbox.apply_supplier_names(r, [], {}) is True
    assert (r["supplier_name"], r["supplier_matched"]) == (None, False)


def test_apply_supplier_names_leaves_a_sale_agreement_and_old_rows_alone():
    agreement = {"found_text": "Scott Group Ltd", "supplier_name": "Cromford Cars", "supplier_matched": True}
    assert inbox.apply_supplier_names(agreement, NAMES, {}) is False
    assert inbox.apply_supplier_names({"supplier_name": None}, NAMES, {}) is False


def test_apply_supplier_names_keeps_a_textract_name_that_is_still_not_on_the_list():
    r = {"found_text": "Other Motors", "supplier_name": "Other Motors", "supplier_matched": False}
    assert inbox.apply_supplier_names(r, NAMES, {}) is False


def test_clean_folder_tidies_a_path_and_refuses_a_link():
    assert inbox.clean_folder(" /Department\\Buying/Invoices/ ") == "Department/Buying/Invoices"
    with pytest.raises(ValueError):
        inbox.clean_folder("https://carsmkt.sharepoint.com/:f:/r/sites/BuyingDrive/Documents/Department")


def test_clean_done_folder_is_one_name():
    assert inbox.clean_done_folder(" Processed ") == "Processed"
    for bad in ("", "a/b", "a\\b"):
        with pytest.raises(ValueError):
            inbox.clean_done_folder(bad)


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise sp.requests.HTTPError(f"{self.status_code}", response=self)


def _client():
    # No secret lookup or sign-in: only the Graph calls below are used.
    client = sp.SharePointClient.__new__(sp.SharePointClient)
    client._headers = {}
    return client


def test_folder_url_is_the_folder_in_the_documents_library():
    assert sp.SharePointClient.folder_url("/Department/Buying/My Invoices/") == (
        "https://carsmkt.sharepoint.com/sites/BuyingDrive/Documents/Department/Buying/My%20Invoices")


def test_move_creates_the_subfolder_when_missing(monkeypatch):
    posted, patched = [], []
    monkeypatch.setattr(sp.requests, "get", lambda url, **k: _Resp(404))
    monkeypatch.setattr(sp.requests, "post", lambda url, **k: posted.append(k["json"]) or _Resp(201, {"id": "F1"}))
    monkeypatch.setattr(sp.requests, "patch", lambda url, **k: patched.append(k["json"]) or _Resp(200))
    _client().move_to_subfolder("D", "ITEM123456", "Department/Buying/Invoices", "Processed", "inv.pdf")
    assert posted[0]["name"] == "Processed"
    assert patched == [{"parentReference": {"id": "F1"}}]


def test_move_renames_when_the_name_is_taken(monkeypatch):
    replies = [_Resp(409), _Resp(200)]
    patched = []
    monkeypatch.setattr(sp.requests, "get", lambda url, **k: _Resp(200, {"id": "F1"}))
    monkeypatch.setattr(sp.requests, "patch", lambda url, **k: patched.append(k["json"]) or replies.pop(0))
    _client().move_to_subfolder("D", "ITEM123456", "Department/Buying/Invoices", "Processed", "inv.pdf")
    assert patched[1]["name"] == "inv (123456).pdf"


def test_move_raises_when_write_access_is_refused(monkeypatch):
    monkeypatch.setattr(sp.requests, "get", lambda url, **k: _Resp(200, {"id": "F1"}))
    monkeypatch.setattr(sp.requests, "patch", lambda url, **k: _Resp(403))
    with pytest.raises(sp.requests.HTTPError):
        _client().move_to_subfolder("D", "ITEM123456", "Department/Buying/Invoices", "Processed", "inv.pdf")
