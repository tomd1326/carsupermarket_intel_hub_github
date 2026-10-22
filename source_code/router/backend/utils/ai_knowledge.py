"""Settings > AI: the knowledge entries every Ask AI reads, and each page's
Ask AI settings (instructions, answer length, temperature, history, knowledge
size). Everything here is edited on the website; the values in this file are
only the defaults a page starts with and goes back to on Reset To Default.

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
SCOPES = ["all"] + PAGE_KEYS

# Kinds of entry, in the order they are sent to the model: rules and
# instructions first, so they survive when the size limit cuts entries off.
KINDS = [
    ("instruction", "Instruction"),
    ("learned", "Learned Fact"),
    ("table", "Table Description"),
    ("column", "Column Note"),
    ("yaml", "YAML"),
    ("document", "Document"),
]
KIND_KEYS = [key for key, _ in KINDS]
STATUSES = ["active", "proposed"]

TITLE_MAX = 200
CONTENT_MAX = 100_000

# The starting entries, loaded when the table is first created and again by
# Add Starting Entries (only those not already there). Saved as proposed, so
# none reaches the model until Tom has checked and approved it.
_SEED_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ai_knowledge_seed")
SEEDS = [
    ("seed_business_rules", "Business Rules", "instruction", "all", "business_rules.md"),
    ("seed_sales_performance", "PRODUCT_SALES_PERFORMANCE.SALES_PERFORMANCE", "table", "all", "sales_performance.md"),
    ("seed_daily_summary", "PRODUCT_SLT_KPIS.DAILY_SUMMARY", "table", "kpi", "daily_summary.md"),
]


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
    if not isinstance(content, str) or not content.strip():
        return "Text is needed"
    if len(content) > CONTENT_MAX:
        return f"Text must be under {CONTENT_MAX:,} characters"
    if entry["kind"] == "column" and title.count(".") != 2:
        return "A column note's title must be SCHEMA.TABLE.COLUMN"
    return None


def _entry_text(entry: dict) -> str:
    return f"### {entry['title']}\n{entry['content'].strip()}\n"


def assemble_knowledge(entries: list, page: str, max_chars: int) -> dict:
    """The knowledge text sent to a page's Ask AI, built from active, enabled
    entries for every page or for this one. Entries go in kind order, then
    title; any that would take the text over max_chars are left out and listed
    in 'skipped' so Settings can show them. Column notes are grouped under
    their table in one section."""
    usable = [e for e in entries
              if e.get("enabled") and e.get("status") == "active" and e.get("scope") in ("all", page)]
    usable.sort(key=lambda e: e["title"].lower())
    parts, used, skipped, total = [], [], [], 0

    def add(text: str, members: list) -> None:
        nonlocal total
        if total + len(text) > max_chars:
            skipped.extend({"id": e["id"], "title": e["title"], "chars": len(_entry_text(e))} for e in members)
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
                add(_entry_text(entry), [entry])

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
        lines += [f"- {column}: {entry['content'].strip()}" for column, entry in items]
        add("\n".join(lines) + "\n", [entry for _, entry in items])


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
    conn.commit()
    if is_new:
        _insert_seeds(conn, "setup")
    _tables_ready = True


def _insert_seeds(conn, user: str) -> int:
    added = 0
    with conn.cursor() as cur:
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


_COLUMNS = "id, title, kind, scope, content, enabled, status, updated_by, updated_at"


def _row_to_entry(row) -> dict:
    keys = [c.strip() for c in _COLUMNS.split(",")]
    entry = dict(zip(keys, row))
    if isinstance(entry["updated_at"], datetime.datetime):
        entry["updated_at"] = entry["updated_at"].isoformat()
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
    values = (entry["title"].strip(), entry["kind"], entry["scope"], entry["content"],
              bool(entry.get("enabled", True)), entry.get("status", "active"), user)
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            if entry.get("id"):
                cur.execute(
                    "UPDATE ai_knowledge SET title = %s, kind = %s, scope = %s, content = %s, "
                    "enabled = %s, status = %s, updated_by = %s, updated_at = NOW() "
                    f"WHERE id = %s RETURNING {_COLUMNS}", values + (entry["id"],))
            else:
                cur.execute(
                    "INSERT INTO ai_knowledge (id, title, kind, scope, content, enabled, status, updated_by) "
                    f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
                    ("k_" + uuid.uuid4().hex[:16],) + values)
            row = cur.fetchone()
        conn.commit()
    if row is None:
        raise ValueError("Entry not found")
    return _row_to_entry(row)


def propose_entry(title: str, content: str, kind: str, scope: str, user: str) -> dict:
    """Save an entry suggested by Ask AI or drafted by Cortex. It waits as
    proposed until Tom approves it in Settings > AI."""
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
        knowledge = assemble_knowledge(list_entries(), page, settings["knowledge_max_chars"])
    except Exception as e:
        logger.warning("AI knowledge for %s not read: %s", page, e)
        knowledge = assemble_knowledge([], page, 0)
    if knowledge["skipped"]:
        logger.warning("AI knowledge for %s over its size limit, left out: %s",
                       page, ", ".join(s["title"] for s in knowledge["skipped"]))
    return settings, knowledge, system_prompt(settings, knowledge)
