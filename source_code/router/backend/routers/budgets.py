"""Weekly Business Review > Settings > Budgets: the monthly budget figures behind the Weekly Business
Review (P&L, volume and cash targets), until now kept only on the Budget tab
of the old review spreadsheet.

The figures come in from a CSV saved from that tab (one row per budget line,
one column per month) and can then be edited in the page. Each line can be
mapped to a KPI chart (map_source 'chart', map_column the chart's id): on the
review only, that chart then draws the line as its target, in place of the KPI
target (kpi.js wbrBudgetData). The KPI page is not changed. Mappings made
before this, to a data column, are ignored.

Two tables in the PROD cluster, like kpi_layout, so every environment shows
the same budget. db/22_budgets.sql is the same change for running by hand.
Importing a file adds or updates lines and months; it never deletes anything.
"""
import csv
import io
import logging
import re

from fastapi import APIRouter, Depends, File, UploadFile
from psycopg2.extras import execute_values

from utils.db_utils import get_prod_pricer_connection
from utils.ddl_once import once_per_database
from .core import get_current_user, is_admin, require_page_api

logger = logging.getLogger(__name__)
router = APIRouter()

CHART_SOURCE = "chart"

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
# The first heading row of the sheet is labelled 'Month'; it starts the P&L block.
_FIRST_SECTION = "P&L"
LABEL_MAX = 200


@once_per_database
def _ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS budget_lines (
                id         SERIAL       PRIMARY KEY,
                section    VARCHAR(50)  NOT NULL,
                label      VARCHAR(200) NOT NULL,
                note       VARCHAR(100) NOT NULL DEFAULT '',
                is_percent BOOLEAN      NOT NULL DEFAULT FALSE,
                row_order  INTEGER      NOT NULL DEFAULT 0,
                map_source VARCHAR(100),
                map_column VARCHAR(200),
                UNIQUE (section, label)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS budget_values (
                line_id    INTEGER      NOT NULL REFERENCES budget_lines(id) ON DELETE CASCADE,
                month      CHAR(7)      NOT NULL,
                value      NUMERIC,
                updated_by VARCHAR(100) NOT NULL,
                updated_at TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                PRIMARY KEY (line_id, month)
            )
            """
        )
    conn.commit()


# ── CSV parsing (no database) ────────────────────────────────────────────────

def parse_number(text: str):
    """A sheet value as shown ('£1,700', '-£281', '"-1,616"', '60%', '2.10%',
    '(45)') -> (number or None, is_percent). Raises ValueError if it is not a number."""
    s = (text or "").strip()
    if s in ("", "-"):
        return None, False
    is_percent = s.endswith("%")
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("£", "").replace(",", "").replace("%", "").replace(" ", "")
    if s.startswith("-"):
        negative, s = not negative, s[1:]
    value = float(s)
    return (-value if negative else value), is_percent


def parse_budget_csv(text: str) -> dict:
    """The Budget tab saved as CSV -> {"lines": [...], "errors": [...]}.

    A row whose value cells are all YYYY-MM is a heading: it starts a section
    (the first is 'Month', named P&L) and sets the month of each column. Rows
    with no label are skipped. Each line is {section, label, note, is_percent,
    row_order, values: {month: number or None}}."""
    lines, errors = [], []
    months, section = None, None
    seen = set()
    for row_no, cells in enumerate(csv.reader(io.StringIO(text.lstrip("﻿"))), start=1):
        if not cells:
            continue
        label = cells[0].strip()
        rest = [c.strip() for c in cells[2:]]
        filled = [c for c in rest if c]
        if filled and all(_MONTH_RE.match(c) for c in filled):
            months = rest
            section = _FIRST_SECTION if label.lower() == "month" else label.title()
            continue
        if not label:
            continue
        if months is None:
            errors.append(f"Row {row_no} ({label}): comes before any row of months")
            continue
        if len(label) > LABEL_MAX:
            errors.append(f"Row {row_no}: label longer than {LABEL_MAX} characters")
            continue
        if (section, label) in seen:
            errors.append(f"Row {row_no} ({label}): appears twice in {section}, the second is skipped")
            continue
        seen.add((section, label))
        values, any_percent = {}, False
        for i, month in enumerate(months):
            if not month:
                continue
            raw = rest[i] if i < len(rest) else ""
            try:
                value, is_percent = parse_number(raw)
            except ValueError:
                errors.append(f"Row {row_no} ({label}) {month}: '{raw}' is not a number, left blank")
                value, is_percent = None, False
            any_percent = any_percent or is_percent
            values[month] = value
        lines.append({"section": section, "label": label, "note": (cells[1].strip() if len(cells) > 1 else "")[:100],
                      "is_percent": any_percent, "row_order": len(lines), "values": values})
    return {"lines": lines, "errors": errors}


def check_edit(edit) -> tuple:
    """One edited cell {line_id, month, value} -> (line_id, month, value or None).
    Raises ValueError with a message for the page."""
    if not isinstance(edit, dict):
        raise ValueError("Bad edit")
    try:
        line_id = int(edit.get("line_id"))
    except (TypeError, ValueError):
        raise ValueError("Bad line id")
    month = str(edit.get("month") or "")
    if not _MONTH_RE.match(month):
        raise ValueError(f"Bad month '{month}'")
    raw = edit.get("value")
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return line_id, month, None
    try:
        value, _ = parse_number(str(raw))
    except ValueError:
        raise ValueError(f"'{raw}' is not a number")
    return line_id, month, value


# ── Endpoints (admin only) ───────────────────────────────────────────────────

def _admin_or_error(user):
    if not is_admin(user):
        return {"status": "error", "message": "Only the admin can change the budgets"}
    return None


@router.get("/weekly-business-review/budgets/data")
def budgets_data(user: str = Depends(get_current_user)):
    if (err := _admin_or_error(user)):
        return err
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT id, section, label, note, is_percent, map_source, map_column
                           FROM budget_lines ORDER BY row_order, id""")
            lines = [{"id": r[0], "section": r[1], "label": r[2], "note": r[3], "is_percent": r[4],
                      "map_source": r[5], "map_column": r[6], "values": {}} for r in cur.fetchall()]
            cur.execute("SELECT line_id, month, value FROM budget_values")
            by_id = {line["id"]: line for line in lines}
            months = set()
            for line_id, month, value in cur.fetchall():
                if line_id in by_id:
                    by_id[line_id]["values"][month] = None if value is None else float(value)
                    months.add(month)
        conn.commit()
    return {"status": "ok", "lines": lines, "months": sorted(months)}


_UPSERT_VALUES = """INSERT INTO budget_values (line_id, month, value, updated_by) VALUES %s
                     ON CONFLICT (line_id, month) DO UPDATE
                     SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = NOW()"""


# Plain def, not async: the database calls block, so FastAPI must run this in
# a worker thread rather than on the event loop every other request shares.
@router.post("/weekly-business-review/budgets/import")
def budgets_import(file: UploadFile = File(...), user: str = Depends(get_current_user)):
    if (err := _admin_or_error(user)):
        return err
    raw = file.file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    parsed = parse_budget_csv(text)
    if not parsed["lines"]:
        return {"status": "error", "message": "No budget lines found in the file", "errors": parsed["errors"]}
    # One statement for the lines and one for the figures: one at a time was
    # ~1,000 round trips to the PROD cluster and took 30 seconds.
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            # A line already there keeps its place and mapping (so lines added by
            # hand stay where they are); its unit follows the file. New lines go
            # after everything already there, in the file's order.
            cur.execute("SELECT COALESCE(MAX(row_order), -1) + 1 FROM budget_lines")
            start = cur.fetchone()[0]
            ids = execute_values(
                cur,
                """INSERT INTO budget_lines (section, label, note, is_percent, row_order) VALUES %s
                   ON CONFLICT (section, label) DO UPDATE
                   SET note = EXCLUDED.note, is_percent = EXCLUDED.is_percent
                   RETURNING id, section, label""",
                [(l["section"], l["label"], l["note"], l["is_percent"], start + l["row_order"]) for l in parsed["lines"]],
                page_size=1000, fetch=True)
            id_of = {(section, label): line_id for line_id, section, label in ids}
            rows = [(id_of[(l["section"], l["label"])], month, value, user)
                    for l in parsed["lines"] for month, value in l["values"].items()]
            execute_values(cur, _UPSERT_VALUES, rows, page_size=5000)
        conn.commit()
    logger.info("Budgets import by %s: %d lines, %d cells", user, len(parsed["lines"]), len(rows))
    return {"status": "ok", "lines": len(parsed["lines"]), "cells": len(rows), "errors": parsed["errors"]}


@router.post("/weekly-business-review/budgets/save")
def budgets_save(payload: dict, user: str = Depends(get_current_user)):
    """Edited cells and mappings, all saved together or not at all."""
    if (err := _admin_or_error(user)):
        return err
    try:
        edits = [check_edit(e) for e in (payload.get("values") or [])]
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    maps = []
    for m in payload.get("maps") or []:
        try:
            line_id = int(m.get("line_id"))
        except (TypeError, ValueError, AttributeError):
            return {"status": "error", "message": "Bad line id"}
        source = (str(m.get("source") or "").strip() or None)
        column = (str(m.get("column") or "").strip() or None)
        # Only a review chart can be picked now; clearing (both blank) is always allowed.
        if (source is None) != (column is None) or source not in (None, CHART_SOURCE) or len(column or "") > 200:
            return {"status": "error", "message": "Bad mapping"}
        maps.append((source, column, line_id))
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            # Keyed so the same cell sent twice is saved once (one statement cannot update a row twice).
            latest = {(line_id, month): value for line_id, month, value in edits}
            if latest:
                execute_values(cur, _UPSERT_VALUES,
                               [(line_id, month, value, user) for (line_id, month), value in latest.items()],
                               page_size=5000)
            for source, column, line_id in maps:
                cur.execute("UPDATE budget_lines SET map_source = %s, map_column = %s WHERE id = %s",
                            (source, column, line_id))
        conn.commit()
    return {"status": "ok", "values": len(edits), "maps": len(maps)}


def check_new_line(payload) -> tuple:
    """A line typed in the page {section, label, note} -> (section, label, note).
    Raises ValueError with a message for the page."""
    if not isinstance(payload, dict):
        raise ValueError("Bad line")
    section = str(payload.get("section") or "").strip()
    label = str(payload.get("label") or "").strip()
    note = str(payload.get("note") or "").strip()
    if not section or not label:
        raise ValueError("A line needs a section and a name")
    if len(section) > 50 or len(label) > LABEL_MAX or len(note) > 100:
        raise ValueError("Section, name or unit is too long")
    return section, label, note


@router.post("/weekly-business-review/budgets/line")
def budgets_add_line(payload: dict, user: str = Depends(get_current_user)):
    """A line added by hand, placed at the end of its section (or at the end
    of the list for a new section). Saved straight away, with no figures."""
    if (err := _admin_or_error(user)):
        return err
    try:
        section, label, note = check_new_line(payload)
    except ValueError as e:
        return {"status": "error", "message": str(e)}
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM budget_lines WHERE LOWER(section) = LOWER(%s) AND LOWER(label) = LOWER(%s)",
                        (section, label))
            if cur.fetchone():
                return {"status": "error", "message": f"{section} already has a line called {label}"}
            # Use the section's name as already stored, whatever case it was typed in.
            cur.execute("SELECT section, MAX(row_order) FROM budget_lines WHERE LOWER(section) = LOWER(%s) GROUP BY section",
                        (section,))
            found = cur.fetchone()
            if found:
                section, after = found
                cur.execute("UPDATE budget_lines SET row_order = row_order + 1 WHERE row_order > %s", (after,))
            else:
                cur.execute("SELECT COALESCE(MAX(row_order), -1) FROM budget_lines")
                after = cur.fetchone()[0]
            cur.execute("""INSERT INTO budget_lines (section, label, note, row_order) VALUES (%s, %s, %s, %s)
                           RETURNING id""", (section, label, note, after + 1))
            line_id = cur.fetchone()[0]
        conn.commit()
    logger.info("Budget line added by %s: %s / %s", user, section, label)
    return {"status": "ok", "id": line_id}


def reorder_ids(ids: list, line_id: int, to: str) -> list:
    """The ids of one section in order -> the same ids with line_id moved to
    'top', 'up', 'down' or 'bottom'. A line already at that end stays put.
    Raises ValueError for an unknown move or id."""
    if line_id not in ids or to not in ("top", "up", "down", "bottom"):
        raise ValueError("Bad move")
    i = ids.index(line_id)
    j = {"top": 0, "up": max(i - 1, 0), "down": min(i + 1, len(ids) - 1), "bottom": len(ids) - 1}[to]
    out = [x for x in ids if x != line_id]
    out.insert(j, line_id)
    return out


@router.post("/weekly-business-review/budgets/line/{line_id}/move")
def budgets_move_line(line_id: int, payload: dict, user: str = Depends(get_current_user)):
    """Moves a line within its section. The section keeps the same places in
    the list, so the other sections do not move."""
    if (err := _admin_or_error(user)):
        return err
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT section FROM budget_lines WHERE id = %s", (line_id,))
            found = cur.fetchone()
            if not found:
                return {"status": "error", "message": "That line no longer exists"}
            cur.execute("SELECT id, row_order FROM budget_lines WHERE section = %s ORDER BY row_order, id", (found[0],))
            rows = cur.fetchall()
            try:
                new_ids = reorder_ids([r[0] for r in rows], line_id, str(payload.get("to") or ""))
            except ValueError as e:
                return {"status": "error", "message": str(e)}
            places = sorted(r[1] for r in rows)
            # Equal places (possible after hand edits) are made distinct so the order sticks.
            places = [max(p, (places[k - 1] + 1) if k else p) for k, p in enumerate(places)]
            changed = [(place, i) for place, i in zip(places, new_ids)]
            if changed:
                execute_values(cur, "UPDATE budget_lines AS b SET row_order = v.o FROM (VALUES %s) AS v(o, i) WHERE b.id = v.i",
                               changed)
        conn.commit()
    return {"status": "ok"}


@router.post("/weekly-business-review/budgets/line/{line_id}/delete")
def budgets_delete_line(line_id: int, user: str = Depends(get_current_user)):
    if (err := _admin_or_error(user)):
        return err
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM budget_lines WHERE id = %s", (line_id,))
        conn.commit()
    return {"status": "ok"}


@router.get("/weekly-business-review/budget-targets")
def budget_targets(user: str = Depends(require_page_api('weekly_business_review'))):
    """The budget lines mapped to a review chart, for everyone who can open the
    review: {chart id: {line_id, label, is_percent, values: {month: number}}}.
    Where two lines name the same chart, the first in the list wins."""
    with get_prod_pricer_connection() as conn:
        _ensure_tables(conn)
        with conn.cursor() as cur:
            cur.execute("""SELECT l.id, l.label, l.is_percent, l.map_column, v.month, v.value
                           FROM budget_lines l LEFT JOIN budget_values v ON v.line_id = l.id
                           WHERE l.map_source = %s AND l.map_column IS NOT NULL
                           ORDER BY l.row_order, l.id""", (CHART_SOURCE,))
            rows = cur.fetchall()
        conn.commit()
    targets = {}
    for line_id, label, is_percent, chart_id, month, value in rows:
        t = targets.setdefault(chart_id, {"line_id": line_id, "label": label, "is_percent": is_percent, "values": {}})
        if t["line_id"] == line_id and month and value is not None:
            t["values"][month] = float(value)
    return {"status": "ok", "targets": targets}
