"""The Semantic & Context Layer entries (metrics, dimensions, tables, glossary
terms, business rules...) that people read on /semantic-layer and every Ask AI
reads, and each page's Ask AI settings in Settings > AI (instructions, answer
length, temperature, history, knowledge size). Everything here is edited on the
website; the values in this file are only the defaults a page starts with and
goes back to on Reset To Default.

Both tables live in the prod pricer DB (get_prod_pricer_connection), like
kpi_layout, so local edits and prod use the same entries. See
db/20_ai_knowledge.sql."""

import datetime
import json
import logging
import os
import uuid

from utils.db_utils import get_prod_pricer_connection

logger = logging.getLogger(__name__)

# Pages with an Ask AI. A new page's bot is added here (its code has to exist
# before it can be switched on), then everything else is set in Settings > AI.
AI_PAGES = [("kpi", "KPI"), ("orders_deliveries", "Orders & Deliveries")]
PAGE_KEYS = [key for key, _ in AI_PAGES]
SCOPES = ["all"] + PAGE_KEYS + ["self_analytics"]

# Kinds of entry, in the order they are sent to the model: rules and
# instructions first, so they survive when the size limit cuts entries off.
# Labels and which ones show on the Semantic & Context Layer page are set on
# that page; the keys here are stored on each row, so never rename one.
KINDS = [
    ("instruction", "AI Instruction"),
    ("rule", "Business Rule"),
    ("metric", "Metric"),
    ("dimension", "Dimension"),
    ("term", "Glossary Term"),
    ("learned", "Learned Fact"),
    ("table", "Table"),
    ("column", "Column"),
    ("yaml", "YAML"),
    ("document", "Document"),
]
KIND_KEYS = [key for key, _ in KINDS]
STATUSES = ["active", "proposed"]

# Alex's two layers. Semantic: what can be measured and sliced, and where it
# lives in Snowflake. Context: what the numbers mean in the business and the
# rules for using them.
LAYERS = [("semantic", "Semantic Layer"), ("context", "Context Layer")]
DEFAULT_KIND_LAYERS = {
    "metric": "semantic", "dimension": "semantic", "table": "semantic", "column": "semantic", "yaml": "semantic",
    "term": "context", "rule": "context", "instruction": "context", "learned": "context", "document": "context",
}

TITLE_MAX = 200
CONTENT_MAX = 100_000

# The starting entries, loaded when the table is first created and again by
# Add Starting Entries (only those not already there). Saved as proposed, so
# none reaches the model until Tom has checked and approved it.
_SEED_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ai_knowledge_seed")
SEEDS = [
    ("seed_sales_performance", "PRODUCT_SALES_PERFORMANCE.SALES_PERFORMANCE", "table", "all", "sales_performance.md"),
    ("seed_daily_summary", "PRODUCT_SLT_KPIS.DAILY_SUMMARY", "table", "kpi", "daily_summary.md"),
]
# Starting entries for the Semantic & Context Layer page: one metric, term or
# rule each, with category, tags and fields. Loaded the same way, as proposed.
REFERENCE_SEED_FILE = "semantic_context_entries.json"

CATEGORY_MAX = 100
TAGS_MAX = 500
FIELD_VALUE_MAX = 20_000


# ── Semantic & Context Layer page settings ───────────────────────────────────
# Everything about the page is set on the page itself (Settings popup): title,
# intro, categories, kind labels and layers, which kinds show, the extra fields
# each kind has, and text sizes. Stored as one JSON value in ai_page_settings
# under REFERENCE_KEY, next to the Ask AI page settings.

REFERENCE_KEY = "semantic_layer_page"
FIELD_TYPES = ["text", "textarea", "code"]
GROUP_BY = ["category", "kind", "none"]

# The page's name is set in Settings > Page Names like every other page.
REFERENCE_DEFAULTS = {
    "intro": ("The semantic layer says what can be measured and how: each metric's calculation, the "
              "dimensions it can be split by, and the tables and columns it comes from. The context "
              "layer says what it means in the business: glossary terms and the rules for using the "
              "numbers. Ask AI reads these entries, and Download YAML gives the same entries to other "
              "analytics tools."),
    "content_label": "Detail",
    "group_by": "category",
    "export_name": "carsupermarket_uk",
    "title_font_px": 17,
    "text_font_px": 14,
    "badge_font_px": 11,
    "categories": ["Sales", "Pricing", "Margin & Costs", "Stock", "Marketing & Enquiries", "Data Tables"],
    "kinds_shown": ["metric", "dimension", "table", "column", "term", "rule", "document"],
    "kind_labels": {},
    "kind_layers": dict(DEFAULT_KIND_LAYERS),
    "layer_labels": {},
    # key never changes once made, so values stay attached when a label is renamed.
    # kinds: which kinds show the field ([] = every kind). send_to_ai: added to
    # the text Ask AI reads.
    "fields": [
        {"key": "definition", "label": "Definition", "type": "textarea", "kinds": [], "send_to_ai": True},
        {"key": "calculation", "label": "Calculation", "type": "code", "kinds": ["metric"], "send_to_ai": True},
        {"key": "source_table", "label": "Source Table", "type": "text",
         "kinds": ["metric", "dimension", "term", "rule", "table"], "send_to_ai": True},
        {"key": "source_columns", "label": "Source Columns", "type": "text",
         "kinds": ["metric", "dimension", "term", "rule"], "send_to_ai": True},
        {"key": "grain", "label": "Grain (One Row Is)", "type": "text", "kinds": ["metric", "table"], "send_to_ai": True},
        {"key": "dimensions", "label": "Can Be Split By", "type": "text", "kinds": ["metric"], "send_to_ai": True},
        {"key": "values", "label": "Allowed Values", "type": "textarea", "kinds": ["dimension"], "send_to_ai": True},
        {"key": "exclusions", "label": "Included And Left Out", "type": "textarea",
         "kinds": ["metric", "rule"], "send_to_ai": True},
        {"key": "synonyms", "label": "Also Called", "type": "text",
         "kinds": ["metric", "dimension", "term"], "send_to_ai": True},
        {"key": "owner", "label": "Owner", "type": "text", "kinds": [], "send_to_ai": False},
        {"key": "report_schema", "label": "Guided Report Source Definition (JSON)", "type": "code", "kinds": ["table"], "send_to_ai": False},
    ],
}

REFERENCE_LIMITS = {"title_font_px": (10, 40), "text_font_px": (10, 30), "badge_font_px": (8, 24)}


# ── Page settings ────────────────────────────────────────────────────────────

_KPI_INSTRUCTIONS = """You are an analyst for Carsupermarket, a UK used-car retailer, answering questions from the KPI dashboard.

Use the knowledge below for what tables, columns and business terms mean. Never invent a definition, table or column that is not there.

If the answer depends on something the knowledge does not settle - for example whether "sold" means ordered or delivered, whether cancelled orders or credited invoices are included, which date to use, the date range, or whether a table or a chart is wanted - ask short clarifying questions first. Give the likely options for each and say which you would assume, so the answer can be just "go".

If the data you were given cannot answer the question, say so and say what would be needed.

Money is GBP. Write in plain, direct language: say what happened in ordinary words, with per-unit figures first. Be concise."""

# Moved here unchanged from routers/orders_deliveries.py. The browser sends a
# compact table of per-slice figures, never the vehicle rows: the model is
# reliable at narrating a table that already has the totals worked out, and
# unreliable at arithmetic over thousands of rows.
_ORDERS_DELIVERIES_INSTRUCTIONS = """You are a motor retail analyst for a UK used-car supermarket.

You are given per-slice figures for a set of deal metrics, already aggregated.
Each row gives a slice (one dimension value, or a pair of them), the number of
vehicles, the mean per vehicle for each period, the change against the previous
period, and `impact` = change x volume, which is that slice's contribution to
the overall movement in currency.

Answer as a buyer would want it: what moved, why, and which supplier or sales
type it's concentrated in. Write it as plain prose and bullets, not fixed
headed sections — let the answer's shape follow the finding rather than
forcing every answer through the same four boxes.

Cover, in whatever order and depth the data actually supports:
- What moved: the metric, the per-unit change, the period pair.
- Why: the causal chain through the component metrics.
  For GPU-M: chassis margin after subsidy, vehicle costs and write-downs. Say
  which moved and by how much per unit. "GPU-M fell because chassis margin fell
  £180/unit, while costs were flat" is the answer; "GPU-M fell because GPU-M
  fell" is not.
  For GPU-S: finance penetration, finance commission per financed deal, and
  total product margin. Penetration and commission rate are different problems
  with different fixes — always say which one moved. A GPU-S fall with flat
  commission per financed deal is a penetration problem; a fall with flat
  penetration is a rate or product-mix problem.
- Where: the supplier channel and supplier, then sales type or price band
  within it if that sharpens the picture. Name the specific combination, not
  each dimension separately: "B2B2C via Enterprise", not "B2B2C was down and
  Enterprise was down".
- Which selection on the page would isolate it, only if that adds something
  beyond what "where" already said — do not restate the same slice as a
  separate instruction.

On figures:
- Lead with per-unit changes — that is how the business thinks about GPU. Use
  the total impact only to establish that a slice is material enough to mention,
  or when the reader needs the cash number. Do not open with total impact and do
  not quote it for every slice. Quote it at most twice in an answer; a slice's
  per-unit change and its vehicle count together already say how much it matters.
- Do not repeat the same slice list twice. If the causal explanation already
  named the slices, do not list them again when saying where the problem sits.
- A per-unit swing on a handful of vehicles is not an explanation. Check the
  volume before calling something a driver, and say when a finding rests on a
  small number of vehicles.
- NEVER present a change in purchase price or sell price as a cause. Those move
  whenever the profile of vehicles sold changes — a week with pricier cars has a
  higher purchase price and a higher sell price, and neither says anything about
  performance. Margin is what matters: chassis margin and GPU-M on the metal
  side, finance and product margin on the back end. Use the price columns only
  as evidence that the mix changed ("purchase price up £3,900/unit, so these are
  materially different cars"), never as the explanation itself.
- Mix matters: a metric can fall because the mix shifted toward lower-margin
  vehicles even when no slice got worse. Check for that and say so when it is
  what happened, because the fix is a different one. If a slice's margin fell
  while its purchase and sell prices both moved a lot, suspect mix first and say
  so rather than calling it a margin problem.
- If the figures do not support a clear cause, say so plainly rather than
  picking the largest number and calling it the reason.

Money is GBP. Metric names: GPU-M is metal gross profit per unit, GPU-S is
service/product gross profit per unit, GPU-MS is the two combined. Chassis
margin after subsidy is the vehicle's own margin before costs.

Be concise — aim for under 250 words unless the question asks for more. No
preamble, no restating the question, no summary paragraph at the end. Prefer
short bullets over long sentences.

Write in plain, direct language. Say exactly what happened in ordinary words:
"GPU-M fell £180 a unit", not "GPU-M softened" or "a deterioration in GPU-M".
Specifically:
- Never soften a fall. Use fell, dropped, down. Not softened, eased, came under
  pressure, headwind, deteriorated.
- Never describe a number as a miss, a beat, a drag or a tailwind. Say it was
  below or above the previous period, and by how much.
- Never use the words slice, dimension, metric, cohort, segment or bucket in
  the answer. Those describe how the data is arranged, not what happened in the
  business. Name the actual thing instead: "Enterprise B2B2C", "the 12 cars
  bought from Motability", "0% deals".
- No jargon for ordinary things. Say "the cars sold were more expensive", not
  "an adverse mix effect"; "only 4 cars", not "a small sample".
Short everyday words over analyst vocabulary, every time.

Follow-up questions continue this conversation. The data table above is the
only data you get for the whole thread, so answer follow-ups from it and from
what you have already said. If a follow-up asks for a figure the table does not
contain — a dimension that was not broken out, or a slice listed as omitted —
say so and name what would need to be selected on the page to get it, rather
than estimating."""

PAGE_DEFAULTS = {
    "kpi": {"instructions": _KPI_INSTRUCTIONS, "max_tokens": 4096, "temperature": 0.3,
            "history_turns": 11, "knowledge_max_chars": 40000},
    "orders_deliveries": {"instructions": _ORDERS_DELIVERIES_INSTRUCTIONS, "max_tokens": 16384,
                          "temperature": 0.2, "history_turns": 11, "knowledge_max_chars": 40000},
}

# (min, max) for the number settings.
PAGE_LIMITS = {
    "max_tokens": (256, 32000),
    "temperature": (0.0, 1.0),
    "history_turns": (1, 41),
    "knowledge_max_chars": (0, 200000),
}


# ── Pure helpers (no database) ───────────────────────────────────────────────

def clean_page_settings(page: str, stored) -> dict:
    """A page's settings with defaults filled in and numbers kept in range."""
    result = dict(PAGE_DEFAULTS[page])
    stored = stored if isinstance(stored, dict) else {}
    instructions = stored.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        result["instructions"] = instructions
    for key, (low, high) in PAGE_LIMITS.items():
        if key not in stored:
            continue
        try:
            value = float(stored[key]) if key == "temperature" else int(stored[key])
        except (TypeError, ValueError):
            continue
        result[key] = min(max(value, low), high)
    return result


def _clean_text(value, default: str, max_len: int) -> str:
    return value.strip()[:max_len] if isinstance(value, str) and value.strip() else default


def _field_key(label: str, taken: set) -> str:
    base = "".join(c if c.isalnum() else "_" for c in label.lower()).strip("_")[:40] or "field"
    key, n = base, 2
    while key in taken:
        key, n = f"{base}_{n}", n + 1
    return key


def clean_reference_settings(stored) -> dict:
    """The Knowledge page settings with defaults filled in and every value
    checked, so a bad save can never break the page."""
    d = REFERENCE_DEFAULTS
    stored = stored if isinstance(stored, dict) else {}
    result = {
        "intro": stored["intro"][:5000] if isinstance(stored.get("intro"), str) else d["intro"],
        "content_label": _clean_text(stored.get("content_label"), d["content_label"], 60),
        "group_by": stored.get("group_by") if stored.get("group_by") in GROUP_BY else d["group_by"],
        "export_name": "".join(c for c in _clean_text(stored.get("export_name"), d["export_name"], 60)
                               if c.isalnum() or c in "_-") or d["export_name"],
    }
    for key, (low, high) in REFERENCE_LIMITS.items():
        try:
            result[key] = min(max(int(stored[key]), low), high)
        except (KeyError, TypeError, ValueError):
            result[key] = d[key]

    categories = stored.get("categories")
    if isinstance(categories, list):
        seen, clean = set(), []
        for c in categories:
            c = c.strip()[:CATEGORY_MAX] if isinstance(c, str) else ""
            if c and c.lower() not in seen:
                seen.add(c.lower())
                clean.append(c)
        result["categories"] = clean
    else:
        result["categories"] = list(d["categories"])

    shown = stored.get("kinds_shown")
    result["kinds_shown"] = [k for k in KIND_KEYS if k in shown] if isinstance(shown, list) else list(d["kinds_shown"])
    labels = stored.get("kind_labels") if isinstance(stored.get("kind_labels"), dict) else {}
    result["kind_labels"] = {k: v.strip()[:60] for k, v in labels.items()
                             if k in KIND_KEYS and isinstance(v, str) and v.strip()}
    layers = stored.get("kind_layers") if isinstance(stored.get("kind_layers"), dict) else {}
    layer_keys = [key for key, _ in LAYERS]
    result["kind_layers"] = {k: layers.get(k) if layers.get(k) in layer_keys else DEFAULT_KIND_LAYERS[k]
                             for k in KIND_KEYS}
    layer_labels = stored.get("layer_labels") if isinstance(stored.get("layer_labels"), dict) else {}
    result["layer_labels"] = {k: v.strip()[:60] for k, v in layer_labels.items()
                              if k in layer_keys and isinstance(v, str) and v.strip()}

    fields = stored.get("fields")
    if not isinstance(fields, list):
        result["fields"] = [dict(f) for f in d["fields"]]
    else:
        clean, taken = [], set()
        for f in fields:
            if not isinstance(f, dict):
                continue
            label = _clean_text(f.get("label"), "", 80)
            if not label:
                continue
            key = f.get("key") if isinstance(f.get("key"), str) and f["key"] and f["key"] not in taken else None
            key = key or _field_key(label, taken)
            taken.add(key)
            clean.append({
                "key": key, "label": label,
                "type": f.get("type") if f.get("type") in FIELD_TYPES else "text",
                "kinds": [k for k in KIND_KEYS if k in (f.get("kinds") or [])],
                "send_to_ai": bool(f.get("send_to_ai")),
            })
        result["fields"] = clean
    if not any(f["key"] == "report_schema" for f in result["fields"]):
        result["fields"].append(dict(next(f for f in d["fields"] if f["key"] == "report_schema")))
    return result


def kind_label(settings: dict, kind: str) -> str:
    return settings["kind_labels"].get(kind) or dict(KINDS).get(kind, kind)


def layer_label(settings: dict, layer: str) -> str:
    return settings["layer_labels"].get(layer) or dict(LAYERS).get(layer, layer)


def field_applies(field: dict, kind: str) -> bool:
    return not field["kinds"] or kind in field["kinds"]


def _entry_fields(entry: dict) -> dict:
    fields = entry.get("fields")
    return fields if isinstance(fields, dict) else {}


def validate_entry(entry: dict) -> str | None:
    """Why an entry cannot be saved, or None."""
    if not isinstance(entry, dict):
        return "Bad entry"
    title = (entry.get("title") or "").strip()
    if not title or len(title) > TITLE_MAX:
        return f"Title is needed and must be under {TITLE_MAX} characters"
    if entry.get("kind") not in KIND_KEYS:
        return "Unknown kind"
    if entry.get("scope") not in SCOPES:
        return "Unknown page"
    if entry.get("status", "active") not in STATUSES:
        return "Unknown status"
    content = entry.get("content") or ""
    if not isinstance(content, str):
        return "Text is needed"
    if len(content) > CONTENT_MAX:
        return f"Text must be under {CONTENT_MAX:,} characters"
    if "fields" in entry and not isinstance(entry["fields"], dict):
        return "Bad fields"
    fields = _entry_fields(entry)
    for value in fields.values():
        if not isinstance(value, str) or len(value) > FIELD_VALUE_MAX:
            return f"Each field must be text under {FIELD_VALUE_MAX:,} characters"
    if not content.strip() and not any(v.strip() for v in fields.values()):
        return "Text is needed"
    if len(str(entry.get("category") or "")) > CATEGORY_MAX:
        return f"Category must be under {CATEGORY_MAX} characters"
    if len(str(entry.get("tags") or "")) > TAGS_MAX:
        return f"Tags must be under {TAGS_MAX} characters"
    if entry["kind"] == "column" and title.count(".") != 2:
        return "A column note's title must be SCHEMA.TABLE.COLUMN"
    if fields.get("report_schema"):
        if entry["kind"] != "table":
            return "A Guided Report Source Definition Belongs To A Table Entry"
        from utils.self_analytics_builder import schema
        try:
            schema({**entry, "id": entry.get("id") or "new_source"})
        except ValueError as exc:
            return str(exc)
    return None


def _entry_text(entry: dict, ai_fields: list | None = None) -> str:
    """One entry as Ask AI reads it: title, then the fields marked Ask AI that
    apply to its kind and have a value, then its main text."""
    lines = [f"### {entry['title']}"]
    values = _entry_fields(entry)
    for f in ai_fields or []:
        value = (values.get(f["key"]) or "").strip()
        if value and field_applies(f, entry["kind"]):
            lines.append(f"{f['label']}: {value}")
    if (entry.get("content") or "").strip():
        lines.append(entry["content"].strip())
    return "\n".join(lines) + "\n"


def ai_fields(settings: dict) -> list:
    return [f for f in settings["fields"] if f["send_to_ai"]]


def assemble_knowledge(entries: list, page: str, max_chars: int, fields: list | None = None) -> dict:
    """The knowledge text sent to a page's Ask AI, built from active, enabled
    entries for every page or for this one. Entries go in kind order, then
    title; any that would take the text over max_chars are left out and listed
    in 'skipped' so Settings can show them. Column notes are grouped under
    their table in one section. fields: the Knowledge page fields marked Ask AI."""
    usable = [e for e in entries
              if e.get("enabled") and e.get("status") == "active" and e.get("scope") in ("all", page)]
    usable.sort(key=lambda e: e["title"].lower())
    parts, used, skipped, total = [], [], [], 0

    def add(text: str, members: list) -> None:
        nonlocal total
        if total + len(text) > max_chars:
            skipped.extend({"id": e["id"], "title": e["title"], "chars": len(_entry_text(e, fields))}
                           for e in members)
            return
        parts.append(text)
        used.extend(e["id"] for e in members)
        total += len(text)

    for kind in KIND_KEYS:
        of_kind = [e for e in usable if e["kind"] == kind]
        if kind == "column":
            _add_columns(of_kind, add)
        else:
            for entry in of_kind:
                add(_entry_text(entry, fields), [entry])

    text = "\n".join(parts)
    return {"text": text, "chars": len(text), "max_chars": max_chars,
            "used": used, "skipped": skipped}


def _add_columns(columns: list, add) -> None:
    """One block per table of column notes, so a table's notes stay together."""
    by_table = {}
    for entry in columns:
        table, column = entry["title"].rsplit(".", 1)
        by_table.setdefault(table, []).append((column, entry))
    for table, items in by_table.items():
        lines = [f"### Column Notes: {table}"]
        lines += [f"- {column}: {_column_text(entry)}" for column, entry in items]
        add("\n".join(lines) + "\n", [entry for _, entry in items])


def _column_text(entry: dict) -> str:
    """A column note's text, or its field values when it has no main text."""
    content = (entry.get("content") or "").strip()
    return content or "; ".join(v.strip() for v in _entry_fields(entry).values() if v.strip())


# ── Export (YAML / JSON) ─────────────────────────────────────────────────────
# For analytics tools and an MCP server to read the same definitions people
# read on the page. Only active entries; split into the two layers, then one
# list per kind.

def build_export(entries: list, settings: dict, generated_at: str, title: str = "") -> dict:
    out = {"name": settings["export_name"], "title": title, "generated_at": generated_at}
    for layer, _ in LAYERS:
        out[f"{layer}_layer"] = {
            "label": layer_label(settings, layer),
            "kinds": _export_kinds([e for e in entries if settings["kind_layers"].get(e["kind"]) == layer],
                                   settings),
        }
    return out


def _export_kinds(entries: list, settings: dict) -> dict:
    groups = {}
    for kind in KIND_KEYS:
        rows = sorted((e for e in entries if e["kind"] == kind and e.get("status") == "active"),
                      key=lambda e: e["title"].lower())
        if not rows:
            continue
        items = []
        for e in rows:
            item = {"name": e["title"]}
            if e.get("category"):
                item["category"] = e["category"]
            tags = [t.strip() for t in (e.get("tags") or "").split(",") if t.strip()]
            if tags:
                item["tags"] = tags
            values = _entry_fields(e)
            for f in settings["fields"]:
                value = (values.get(f["key"]) or "").strip()
                if value and field_applies(f, kind):
                    item[f["key"]] = value
            if (e.get("content") or "").strip():
                item["detail"] = e["content"].strip()
            item["used_by_ask_ai"] = bool(e.get("enabled"))
            item["updated_at"] = e.get("updated_at")
            item["updated_by"] = e.get("updated_by")
            items.append(item)
        groups[kind] = {"label": kind_label(settings, kind), "entries": items}
    return groups


def _yaml_scalar(value, indent: str) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\r\n", "\n")
    if "\n" in text:
        # A literal block keeps line breaks exactly as typed.
        body = "\n".join((indent + "  " + line) if line else "" for line in text.split("\n"))
        return "|-\n" + body
    # A JSON string is a valid YAML double-quoted string, so nothing typed can
    # break the file.
    return json.dumps(text, ensure_ascii=False)


def to_yaml(value, indent: str = "") -> str:
    """Plain YAML for dicts, lists and scalars (PyYAML is not installed)."""
    lines = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{indent}{key}:")
                lines.append(to_yaml(item, indent + "  "))
            elif isinstance(item, (dict, list)):
                lines.append(f"{indent}{key}: {'{}' if isinstance(item, dict) else '[]'}")
            else:
                lines.append(f"{indent}{key}: {_yaml_scalar(item, indent)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and item:
                inner = to_yaml(item, indent + "  ").split("\n")
                lines.append(f"{indent}- {inner[0].lstrip()}")
                lines.extend(inner[1:])
            else:
                lines.append(f"{indent}- {_yaml_scalar(item, indent + '  ')}")
    return "\n".join(lines)


def system_prompt(settings: dict, knowledge: dict) -> str:
    """The page's instructions followed by its knowledge text."""
    if not knowledge["text"]:
        return settings["instructions"]
    return f"{settings['instructions']}\n\n=== KNOWLEDGE ===\n{knowledge['text']}"


# ── Database ─────────────────────────────────────────────────────────────────

_tables_ready = False


def _ensure_tables(conn) -> None:
    """Create both tables on first use; load the starting entries when the
    knowledge table is new."""
    global _tables_ready
    if _tables_ready:
        return
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('ai_knowledge')")
        is_new = cur.fetchone()[0] is None
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_knowledge (
                id VARCHAR(40) PRIMARY KEY,
                title VARCHAR(200) NOT NULL,
                kind VARCHAR(20) NOT NULL,
                scope VARCHAR(50) NOT NULL DEFAULT 'all',
                content TEXT NOT NULL DEFAULT '',
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                status VARCHAR(10) NOT NULL DEFAULT 'active',
                updated_by VARCHAR(100),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_page_settings (
                page_key VARCHAR(50) PRIMARY KEY,
                value TEXT NOT NULL,
                updated_by VARCHAR(100),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )""")
        # Added for the Semantic & Context Layer page. fields is a JSON object
        # of field key -> text (the fields themselves are set on the page).
        cur.execute("ALTER TABLE ai_knowledge ADD COLUMN IF NOT EXISTS category VARCHAR(100) NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE ai_knowledge ADD COLUMN IF NOT EXISTS tags VARCHAR(500) NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE ai_knowledge ADD COLUMN IF NOT EXISTS fields TEXT NOT NULL DEFAULT '{}'")
        cur.execute("ALTER TABLE ai_knowledge ADD COLUMN IF NOT EXISTS show_on_page BOOLEAN NOT NULL DEFAULT TRUE")
    conn.commit()
    if is_new:
        _insert_seeds(conn, "setup")
    _import_report_knowledge(conn)
    _tables_ready = True


def _reference_seeds() -> list:
    entries = []
    for filename in (REFERENCE_SEED_FILE, "report_builder_entries.json"):
        try:
            with open(os.path.join(_SEED_DIR, filename), encoding="utf-8") as f:
                entries.extend(json.load(f))
        except (OSError, ValueError) as e:
            logger.warning("Knowledge seeds %s not loaded: %s", filename, e)
    return entries


def _import_report_knowledge(conn) -> None:
    """One additive import per bundle, proposed for review. Never overwrite edits.

    A persistent marker means entries deleted afterwards stay deleted. The marker
    and entries commit together so interrupted deployments can retry safely.
    """
    key = "report_knowledge_import_20261106"
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM ai_page_settings WHERE page_key = %s", (key,))
        if cur.fetchone():
            return
        with open(os.path.join(_SEED_DIR, "report_builder_entries.json"), encoding="utf-8") as f:
            entries = json.load(f)
        for s in entries:
            cur.execute(
                "INSERT INTO ai_knowledge (id, title, kind, scope, content, enabled, status, updated_by, category, tags, fields) "
                "VALUES (%s, %s, %s, %s, %s, TRUE, 'proposed', 'project_knowledge_import', %s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (s["id"], s["title"], s["kind"], s.get("scope", "all"), s.get("content", ""),
                 s.get("category", ""), s.get("tags", ""), json.dumps(s.get("fields") or {})))
        cur.execute("INSERT INTO ai_page_settings (page_key, value, updated_by) VALUES (%s, %s, %s) "
                    "ON CONFLICT (page_key) DO NOTHING", (key, 'true', "project_knowledge_import"))
    conn.commit()


def _insert_seeds(conn, user: str) -> int:
    added = 0
    with conn.cursor() as cur:
        for s in _reference_seeds():
            cur.execute(
                "INSERT INTO ai_knowledge (id, title, kind, scope, content, enabled, status, updated_by, "
                "category, tags, fields) VALUES (%s, %s, %s, %s, %s, TRUE, 'proposed', %s, %s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (s["id"], s["title"], s["kind"], s.get("scope", "all"), s.get("content", ""), user,
                 s.get("category", ""), s.get("tags", ""), json.dumps(s.get("fields") or {})))
            added += cur.rowcount
        for entry_id, title, kind, scope, filename in SEEDS:
            try:
                with open(os.path.join(_SEED_DIR, filename), encoding="utf-8") as f:
                    content = f.read()
            except OSError as e:
                logger.warning("AI knowledge: starting entry %s not loaded: %s", filename, e)
                continue
            cur.execute(
                "INSERT INTO ai_knowledge (id, title, kind, scope, content, enabled, status, updated_by) "
                "VALUES (%s, %s, %s, %s, %s, TRUE, 'proposed', %s) ON CONFLICT (id) DO NOTHING",
                (entry_id, title, kind, scope, content, user))
            added += cur.rowcount
    conn.commit()
    return added


def add_starting_entries(user: str) -> int:
    """Add any starting entry that is not in the table (deleted ones come back)."""
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        return _insert_seeds(conn, user)


_COLUMNS = ("id, title, kind, scope, content, enabled, status, updated_by, updated_at, "
            "category, tags, fields, show_on_page")


def _row_to_entry(row) -> dict:
    keys = [c.strip() for c in _COLUMNS.split(",")]
    entry = dict(zip(keys, row))
    if isinstance(entry["updated_at"], datetime.datetime):
        entry["updated_at"] = entry["updated_at"].isoformat()
    try:
        fields = json.loads(entry["fields"] or "{}")
    except ValueError:
        fields = {}
    entry["fields"] = fields if isinstance(fields, dict) else {}
    return entry


def list_entries() -> list:
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_COLUMNS} FROM ai_knowledge ORDER BY title")
            rows = cur.fetchall()
        conn.commit()
    return [_row_to_entry(r) for r in rows]


def save_entry(entry: dict, user: str) -> dict:
    """Insert (no id) or update (with id) one entry. Raises ValueError with the
    reason when it cannot be saved."""
    error = validate_entry(entry)
    if error:
        raise ValueError(error)
    cols = {
        "title": entry["title"].strip(), "kind": entry["kind"], "scope": entry["scope"],
        "content": entry.get("content") or "", "enabled": bool(entry.get("enabled", True)),
        "status": entry.get("status", "active"), "updated_by": user,
    }
    # The page's own columns are only written when sent, so an Ask AI proposal
    # or an older caller never blanks them.
    if "category" in entry:
        cols["category"] = str(entry.get("category") or "").strip()
    if "tags" in entry:
        cols["tags"] = ", ".join(t.strip() for t in str(entry.get("tags") or "").split(",") if t.strip())
    if "fields" in entry:
        cols["fields"] = json.dumps({k: v for k, v in _entry_fields(entry).items() if v.strip()})
    if "show_on_page" in entry:
        cols["show_on_page"] = bool(entry["show_on_page"])
    names = list(cols)
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            if entry.get("id"):
                sets = ", ".join(f"{n} = %s" for n in names)
                cur.execute(f"UPDATE ai_knowledge SET {sets}, updated_at = NOW() WHERE id = %s RETURNING {_COLUMNS}",
                            [cols[n] for n in names] + [entry["id"]])
            else:
                cur.execute(
                    f"INSERT INTO ai_knowledge (id, {', '.join(names)}) VALUES (%s{', %s' * len(names)}) "
                    f"RETURNING {_COLUMNS}", ["k_" + uuid.uuid4().hex[:16]] + [cols[n] for n in names])
            row = cur.fetchone()
        conn.commit()
    if row is None:
        raise ValueError("Entry not found")
    return _row_to_entry(row)


def propose_entry(title: str, content: str, kind: str, scope: str, user: str) -> dict:
    """Save an entry suggested by Ask AI or drafted by Cortex. It waits as
    proposed until Tom approves it on the Semantic & Context Layer page."""
    return save_entry({"title": title, "content": content, "kind": kind, "scope": scope,
                       "enabled": True, "status": "proposed"}, user)


def set_status(entry_id: str, status: str, user: str) -> None:
    if status not in STATUSES:
        raise ValueError("Unknown status")
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE ai_knowledge SET status = %s, updated_by = %s, updated_at = NOW() WHERE id = %s",
                        (status, user, entry_id))
        conn.commit()


def approve_all(user: str) -> int:
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE ai_knowledge SET status = 'active', updated_by = %s, updated_at = NOW() "
                        "WHERE status = 'proposed'", (user,))
            count = cur.rowcount
        conn.commit()
    return count


def rename_category(old: str, new: str, user: str) -> None:
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE ai_knowledge SET category = %s, updated_by = %s, updated_at = NOW() "
                        "WHERE category = %s", (new, user, old))
        conn.commit()


def get_reference_settings() -> dict:
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM ai_page_settings WHERE page_key = %s", (REFERENCE_KEY,))
            row = cur.fetchone()
        conn.commit()
    try:
        stored = json.loads(row[0]) if row else None
    except ValueError:
        stored = None
    return clean_reference_settings(stored)


def save_reference_settings(values: dict, user: str) -> dict:
    """Save the page settings. values['renames'] ({old: new}) renames a
    category on every entry that has it."""
    cleaned = clean_reference_settings(values)
    renames = values.get("renames") if isinstance(values.get("renames"), dict) else {}
    for old, new in renames.items():
        new = new.strip()[:CATEGORY_MAX] if isinstance(new, str) else ""
        if isinstance(old, str) and old and new and old != new:
            rename_category(old, new, user)
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ai_page_settings (page_key, value, updated_by) VALUES (%s, %s, %s) "
                "ON CONFLICT (page_key) DO UPDATE SET value = EXCLUDED.value, "
                "updated_by = EXCLUDED.updated_by, updated_at = NOW()",
                (REFERENCE_KEY, json.dumps(cleaned), user))
        conn.commit()
    return cleaned


def reset_reference_settings() -> dict:
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_page_settings WHERE page_key = %s", (REFERENCE_KEY,))
        conn.commit()
    return clean_reference_settings(None)


def delete_entry(entry_id: str) -> None:
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_knowledge WHERE id = %s", (entry_id,))
        conn.commit()


def get_page_settings(page: str) -> dict:
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM ai_page_settings WHERE page_key = %s", (page,))
            row = cur.fetchone()
        conn.commit()
    try:
        stored = json.loads(row[0]) if row else None
    except ValueError:
        stored = None
    return clean_page_settings(page, stored)


def save_page_settings(page: str, values: dict, user: str) -> dict:
    """Save a page's settings; an empty instructions box goes back to the default."""
    if page not in PAGE_KEYS:
        raise ValueError("Unknown page")
    cleaned = clean_page_settings(page, values)
    stored = dict(cleaned)
    if stored["instructions"] == PAGE_DEFAULTS[page]["instructions"]:
        # Not stored, so a later change to the default in code still applies.
        stored.pop("instructions")
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ai_page_settings (page_key, value, updated_by) VALUES (%s, %s, %s) "
                "ON CONFLICT (page_key) DO UPDATE SET value = EXCLUDED.value, "
                "updated_by = EXCLUDED.updated_by, updated_at = NOW()",
                (page, json.dumps(stored), user))
        conn.commit()
    return cleaned


def reset_page_settings(page: str) -> dict:
    if page not in PAGE_KEYS:
        raise ValueError("Unknown page")
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ai_page_settings WHERE page_key = %s", (page,))
        conn.commit()
    return clean_page_settings(page, None)


def page_prompt(page: str) -> tuple[dict, dict, str]:
    """(settings, knowledge, system prompt) for a page's Ask AI. If the
    knowledge cannot be read the page still answers, from its instructions
    alone, and the failure is logged."""
    try:
        settings = get_page_settings(page)
    except Exception as e:
        logger.warning("AI settings for %s not read, using defaults: %s", page, e)
        settings = clean_page_settings(page, None)
    try:
        fields = ai_fields(get_reference_settings())
    except Exception as e:
        logger.warning("Semantic & Context Layer settings not read, using defaults: %s", e)
        fields = ai_fields(clean_reference_settings(None))
    try:
        knowledge = assemble_knowledge(list_entries(), page, settings["knowledge_max_chars"], fields)
    except Exception as e:
        logger.warning("AI knowledge for %s not read: %s", page, e)
        knowledge = assemble_knowledge([], page, 0)
    if knowledge["skipped"]:
        logger.warning("AI knowledge for %s over its size limit, left out: %s",
                       page, ", ".join(s["title"] for s in knowledge["skipped"]))
    return settings, knowledge, system_prompt(settings, knowledge)
