"""Trade Report: what Settings > View Vehicles is allowed to save."""
import pytest

from routers import trade_report as tr


def _col(column="vrm", **extra):
    return {"column": column, "label": "", "format": "text", "decimals": 0, "summary": "none", **extra}


def test_keeps_order_and_trims_labels():
    view = tr._clean_view({
        "columns": [_col("net_margin", label=" Net ", format="gbp", summary="sum"), _col("vrm")],
        "filters": [{"column": "supplier", "label": " Who "}],
    })
    assert [c["column"] for c in view["columns"]] == ["net_margin", "vrm"]
    assert view["columns"][0]["label"] == "Net"
    assert view["filters"] == [{"column": "supplier", "label": "Who"}]


@pytest.mark.parametrize("view", [
    None,
    {"columns": []},
    {"columns": [_col("not_a_column")]},
    {"columns": [_col(), _col()]},
    {"columns": [_col(format="bad")]},
    {"columns": [_col(decimals=5)]},
    {"columns": [_col(decimals=True)]},
    {"columns": [_col(summary="bad")]},
    {"columns": [_col(label="x" * 61)]},
    {"columns": [_col()], "filters": [{"column": "", "label": ""}]},
])
def test_rejects_bad_settings(view):
    with pytest.raises(ValueError):
        tr._clean_view(view)


def test_every_column_the_page_can_pick_is_sent_by_the_server():
    """trade_vehicles.js lists the columns by hand; each must be in the payload."""
    import pathlib
    import re
    js = (pathlib.Path(tr.__file__).parent.parent / "static" / "js" / "trade_vehicles.js").read_text(encoding="utf-8")
    picked = set(re.findall(r"\{ column: '(\w+)', label:", js))
    assert picked == {key for _, key in tr._COLUMNS}
