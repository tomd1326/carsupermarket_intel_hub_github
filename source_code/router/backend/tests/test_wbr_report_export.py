"""Export To Google Slides from the Weekly Business Review Report: what the page may
send, and the requests that lay out a slide. Nothing here calls Google."""
import pytest

from clients.google_slides_client import report_slide_requests
from routers import wbr_comments as wc
from routers import wbr_report as wr


def _table(rows=1, columns=3):
    return {"head": [f"H{c}" for c in range(columns)],
            "rows": [{"cells": [f"R{r}C{c}" for c in range(columns)], "bold": r == 0} for r in range(rows)]}


def _of(requests, name):
    return [r[name] for r in requests if name in r]


# ── What the page may send ───────────────────────────────────────────────────

def test_a_table_slide_is_kept_and_cut_to_the_limits():
    out = wr.clean_export_slide({"title": " P&L ", "subtitle": "s" * 400, "note": "n", "table": _table(), "extra": 1})
    assert out["title"] == "P&L"
    assert len(out["subtitle"]) == 300
    assert out["note"] == "n"
    assert out["table"]["rows"][0] == {"cells": ["R0C0", "R0C1", "R0C2"], "bold": True}
    assert "extra" not in out


def test_a_chart_slide_keeps_each_line():
    out = wr.clean_export_slide({"title": "T", "lines": [[{"title": "A", "subtitle": "", "image": "x"}, {"title": "B", "image": "y"}]]})
    assert [c["title"] for c in out["lines"][0]] == ["A", "B"]
    assert out["lines"][0][1]["image"] == "y"


@pytest.mark.parametrize("raw", [
    None,
    "x",
    {"title": "No table or charts"},
    {"lines": []},
    {"lines": [[]]},
    {"lines": ["x"]},
    {"lines": [[{"image": "a"}, {"image": "b"}, {"image": "c"}]]},
    {"lines": [[{"image": "a"}]] * 4},
    {"table": "x"},
    {"table": {"head": [], "rows": []}},
    {"table": {"head": ["A"], "rows": []}},
    {"table": {"head": ["A", "B"], "rows": [{"cells": ["only one"]}]}},
    {"table": _table(rows=41)},
    {"table": _table(columns=31)},
])
def test_bad_slides_are_rejected(raw):
    with pytest.raises(ValueError):
        wr.clean_export_slide(raw)


# ── The slide's requests ─────────────────────────────────────────────────────

def test_a_table_slide_makes_a_table_with_its_text_and_skips_blank_cells():
    table = {"head": ["Line", "Aug"], "rows": [{"cells": ["GPU-M", "£700"], "bold": True}, {"cells": ["GPU-S", ""], "bold": False}]}
    requests = report_slide_requests({"title": "T", "table": table}, "s1")
    made = _of(requests, "createTable")[0]
    assert (made["rows"], made["columns"]) == (3, 2)
    inserted = [r for r in _of(requests, "insertText") if "cellLocation" in r]
    assert [r["text"] for r in inserted] == ["Line", "Aug", "GPU-M", "£700", "GPU-S"]
    styled = [r for r in _of(requests, "updateTextStyle") if "cellLocation" in r]
    assert len(styled) == len(inserted)
    assert [r["style"]["bold"] for r in styled] == [True, True, True, True, False]


def test_figures_are_right_aligned_and_labels_are_not():
    requests = report_slide_requests({"table": {"head": ["Line", "Aug"], "rows": [{"cells": ["GPU-M", "£700"]}]}}, "s1")
    assert [r["cellLocation"]["columnIndex"] for r in _of(requests, "updateParagraphStyle")] == [1, 1]


def test_a_table_of_words_is_not_right_aligned():
    requests = report_slide_requests({"table": {"head": ["Term", "Means"], "rows": [{"cells": ["GPU", "Margin"]}], "text": True}}, "s1")
    assert _of(requests, "updateParagraphStyle") == []


def test_no_table_column_is_narrower_than_google_allows():
    requests = report_slide_requests({"table": _table(columns=22)}, "s1")
    widths = [r["tableColumnProperties"]["columnWidth"]["magnitude"] for r in _of(requests, "updateTableColumnProperties")]
    assert min(widths) >= 406400


def test_a_one_column_table_has_no_empty_width_request():
    requests = report_slide_requests({"table": _table(columns=1)}, "s1")
    assert len(_of(requests, "updateTableColumnProperties")) == 1


def test_blank_title_subtitle_and_note_get_no_box():
    requests = report_slide_requests({"title": "", "subtitle": "", "note": "", "table": _table()}, "s1")
    assert _of(requests, "createShape") == []


def test_chart_lines_stack_and_sit_side_by_side_above_the_note():
    chart = {"title": "GPU-M - Month", "subtitle": "£", "image_url": "https://x/y.png"}
    requests = report_slide_requests({"title": "T", "note": "A note", "lines": [[chart, chart], [chart, chart]]}, "s1")
    images = _of(requests, "createImage")
    assert len(images) == 4
    place = [i["elementProperties"]["transform"] for i in images]
    assert place[0]["translateY"] == place[1]["translateY"] < place[2]["translateY"]
    assert place[0]["translateX"] < place[1]["translateX"]
    bottom = place[3]["translateY"] + images[3]["elementProperties"]["size"]["height"]["magnitude"]
    note = next(s for s in _of(requests, "createShape") if s["objectId"] == "s1_n")
    assert bottom <= note["elementProperties"]["transform"]["translateY"]
    captions = [r["text"] for r in _of(requests, "insertText") if r["objectId"].startswith("s1_c")]
    assert captions == ["GPU-M - Month - £"] * 4


def test_object_ids_are_unique_on_a_slide():
    chart = {"title": "A", "image_url": "u"}
    requests = report_slide_requests({"title": "T", "subtitle": "S", "note": "N", "lines": [[chart, chart], [chart]]})
    made = [r["objectId"] for name in ("createSlide", "createShape", "createImage", "createTable") for r in _of(requests, name)]
    assert len(made) == len(set(made))
    assert all(5 <= len(i) <= 50 for i in made)


# ── Comments on a Report table ───────────────────────────────────────────────

@pytest.mark.parametrize("target,ok", [
    ("table:stock_b2c", True),
    ("table:glossary_1", True),
    ("table:", False),
    ("table:a b", False),
    ("table:x|m", False),
    ("chart:r1|m", True),
])
def test_comment_targets(target, ok):
    assert bool(wc._TARGET_RE.fullmatch(target)) is ok
