"""Settings > AI knowledge (utils/ai_knowledge.py): which entries reach a
page's Ask AI, the size limit, and the page settings' defaults and limits.

The risks covered: a proposed or switched-off entry reaching the model, one
page's entry reaching another page, an entry silently cut off by the size
limit, and a typed setting outside its range.

Run from source_code/router/backend:
    poetry run pytest tests/test_ai_knowledge.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from utils import ai_knowledge as ak


def entry(id, title, kind="document", scope="all", content="text", enabled=True, status="active"):
    return {"id": id, "title": title, "kind": kind, "scope": scope, "content": content,
            "enabled": enabled, "status": status}


# ── Which entries are sent ───────────────────────────────────────────────────

def test_only_active_enabled_entries_for_the_page():
    entries = [
        entry("a", "Shared"),
        entry("b", "KPI only", scope="kpi"),
        entry("c", "Orders only", scope="orders_deliveries"),
        entry("d", "Proposed", status="proposed"),
        entry("e", "Switched off", enabled=False),
    ]
    k = ak.assemble_knowledge(entries, "kpi", 10000)
    assert sorted(k["used"]) == ["a", "b"]
    assert "Orders only" not in k["text"] and "Proposed" not in k["text"]


def test_instructions_come_before_documents():
    entries = [entry("doc", "A document"), entry("rule", "Z rule", kind="instruction")]
    k = ak.assemble_knowledge(entries, "kpi", 10000)
    assert k["text"].index("Z rule") < k["text"].index("A document")


def test_column_notes_grouped_under_their_table():
    entries = [
        entry("c1", "PRODUCT_X.SALES.GPU_M", kind="column", content="metal margin"),
        entry("c2", "PRODUCT_X.SALES.VRM", kind="column", content="number plate"),
    ]
    k = ak.assemble_knowledge(entries, "kpi", 10000)
    assert k["text"].count("Column Notes: PRODUCT_X.SALES") == 1
    assert "- GPU_M: metal margin" in k["text"] and "- VRM: number plate" in k["text"]


def test_size_limit_reports_what_was_left_out():
    entries = [entry("small", "Small", content="x" * 10), entry("big", "Big", content="y" * 5000)]
    k = ak.assemble_knowledge(entries, "kpi", 200)
    assert k["used"] == ["small"]
    assert [s["id"] for s in k["skipped"]] == ["big"]
    assert k["chars"] <= 200


# ── Entry checks ─────────────────────────────────────────────────────────────

def test_validate_entry():
    good = entry("x", "Title")
    assert ak.validate_entry(good) is None
    assert ak.validate_entry({**good, "title": " "}) is not None
    assert ak.validate_entry({**good, "kind": "nonsense"}) is not None
    assert ak.validate_entry({**good, "scope": "nowhere"}) is not None
    assert ak.validate_entry({**good, "content": ""}) is not None
    assert ak.validate_entry({**good, "kind": "column", "title": "SALES.GPU_M"}) is not None
    assert ak.validate_entry({**good, "kind": "column", "title": "PRODUCT_X.SALES.GPU_M"}) is None


# ── Page settings ────────────────────────────────────────────────────────────

def test_page_settings_defaults_and_limits():
    s = ak.clean_page_settings("kpi", None)
    assert s == ak.PAGE_DEFAULTS["kpi"]
    s = ak.clean_page_settings("kpi", {"max_tokens": 999999, "temperature": "-1", "history_turns": "abc",
                                       "instructions": "   "})
    assert s["max_tokens"] == ak.PAGE_LIMITS["max_tokens"][1]
    assert s["temperature"] == 0.0
    assert s["history_turns"] == ak.PAGE_DEFAULTS["kpi"]["history_turns"]
    assert s["instructions"] == ak.PAGE_DEFAULTS["kpi"]["instructions"]


def test_system_prompt_adds_knowledge_only_when_there_is_some():
    s = ak.clean_page_settings("kpi", None)
    empty = ak.assemble_knowledge([], "kpi", 1000)
    assert ak.system_prompt(s, empty) == s["instructions"]
    k = ak.assemble_knowledge([entry("a", "Rule", content="sold means paid")], "kpi", 1000)
    assert "=== KNOWLEDGE ===" in ak.system_prompt(s, k) and "sold means paid" in ak.system_prompt(s, k)


def test_starting_entry_files_exist():
    for _, _, kind, scope, filename in ak.SEEDS:
        assert kind in ak.KIND_KEYS and scope in ak.SCOPES
        assert os.path.getsize(os.path.join(ak._SEED_DIR, filename)) > 0


# ── Semantic & Context Layer ─────────────────────────────────────────────────

def test_semantic_layer_starting_entries_are_valid():
    seeds = ak._reference_seeds()
    assert len(seeds) >= 10
    ids = [s["id"] for s in seeds]
    assert len(ids) == len(set(ids))
    field_keys = {f["key"] for f in ak.REFERENCE_DEFAULTS["fields"]}
    for s in seeds:
        assert ak.validate_entry({**s, "scope": s.get("scope", "all")}) is None, s["id"]
        assert set(s.get("fields", {})) <= field_keys, s["id"]


def test_reference_settings_defaults_and_bad_values():
    assert ak.clean_reference_settings(None)["categories"] == ak.REFERENCE_DEFAULTS["categories"]
    s = ak.clean_reference_settings({
        "title_font_px": 999, "group_by": "nonsense", "kinds_shown": ["metric", "nonsense"],
        "categories": ["Sales", " sales ", "", 5, "Stock"],
        "kind_layers": {"metric": "context", "term": "nowhere"},
        "fields": [{"label": "Owner"}, {"label": "Owner", "key": "owner"}, {"label": ""}, "bad"],
    })
    assert s["title_font_px"] == ak.REFERENCE_LIMITS["title_font_px"][1]
    assert s["group_by"] == ak.REFERENCE_DEFAULTS["group_by"]
    assert s["kinds_shown"] == ["metric"]
    assert s["categories"] == ["Sales", "Stock"]
    assert s["kind_layers"]["metric"] == "context" and s["kind_layers"]["term"] == "context"
    assert s["kind_layers"]["dimension"] == "semantic"
    keys = [f["key"] for f in s["fields"]]
    assert keys == ["owner", "owner_2", "report_schema"]
    assert len(set(keys)) == 3
    definition = next(f for f in s["fields"] if f["key"] == "report_schema")
    assert definition["type"] == "code" and definition["kinds"] == ["table"]


def test_entry_with_fields_only_is_valid_and_reaches_ask_ai():
    e = {**entry("m", "GPU M", kind="metric", content=""), "fields": {"definition": "metal margin", "owner": "Tom"}}
    assert ak.validate_entry(e) is None
    fields = ak.ai_fields(ak.clean_reference_settings(None))
    k = ak.assemble_knowledge([e], "kpi", 10000, fields)
    assert "Definition: metal margin" in k["text"]
    assert "Tom" not in k["text"]  # Owner is not sent to Ask AI by default


def test_export_splits_layers_and_yaml_is_safe():
    settings = ak.clean_reference_settings(None)
    entries = [
        {**entry("m", "GPU M", kind="metric"), "fields": {"calculation": "a + b\nline two"}, "category": "Margin"},
        {**entry("t", 'Say "sold"', kind="term"), "fields": {}},
        {**entry("p", "Proposed", kind="metric", status="proposed"), "fields": {}},
    ]
    data = ak.build_export(entries, settings, "2026-11-06 12:00", "Semantic & Context Layer")
    metrics = data["semantic_layer"]["kinds"]["metric"]["entries"]
    assert [m["name"] for m in metrics] == ["GPU M"]
    assert "term" in data["context_layer"]["kinds"]
    text = ak.to_yaml(data)
    assert 'name: "Say \\"sold\\""' in text
    assert "calculation: |-" in text and "line two" in text
