"""Cars Bought: supplier invoices picked up from a SharePoint folder.

Invoices are saved into one folder (Cars Bought Settings). Opening the page, or
pressing Check Folder, lists that folder and each new file gets a row here. The
file is read in its own Lambda invocation, the same way as an upload, and waits
as pending until a buyer reviews and submits it. A pending invoice is not a
purchase: nothing goes in purchase_batches, the CSV extract or Already Bought
until it is submitted.

A file that was read is moved to a subfolder, so the folder only holds what is
still to do. The row here, keyed on the file's SharePoint ID (which a move does
not change), is what stops a file being read and paid for twice, so a file that
cannot be moved is still only read once.

Statuses: queued, reading, pending, failed, submitted, ignored.
"""
import datetime
import json

import psycopg2.extras

from utils import cars_bought as cb
from utils.ddl_once import once_per_database

DEFAULT_FOLDER = "Department/Buying/Invoices"
DEFAULT_DONE_FOLDER = "Processed"
DEFAULT_MAX_FILES = 20
# A read still going after this long has died (Lambda stops at 15 minutes).
STALE_MINUTES = 16


def clean_folder(text: str) -> str:
    """A folder path inside the site's Documents library. Raises ValueError for
    a pasted link, which holds no usable path."""
    path = str(text or "").strip().replace("\\", "/").strip("/")
    if "://" in path:
        raise ValueError("Invoice Folder must be a path such as Department/Buying/Invoices, not a link")
    return path


def clean_done_folder(text: str) -> str:
    name = str(text or "").strip()
    if not name or "/" in name or "\\" in name:
        raise ValueError("Processed Subfolder must be one folder name")
    return name


def wanted_files(files: list, known: set, since: str, max_files: int) -> list:
    """The files to read now, oldest first: not seen before, and saved on or
    after `since` (yyyy-mm-dd, blank for any date). Excel's "~$" lock files are
    not invoices. max_files caps what one check can spend on Textract."""
    out = [f for f in files
           if f["id"] not in known and not f["name"].startswith("~$")
           and (not since or (f.get("lastModifiedDateTime") or "")[:10] >= since)]
    out.sort(key=lambda f: f.get("lastModifiedDateTime") or "")
    return out[:max_files]


def folder_counts(files: list, known: set, since: str, wanted: list) -> dict:
    """What one look in the folder found, for the page's status line."""
    invoices = [f for f in files if not f["name"].startswith("~$")]
    new = [f for f in invoices if f["id"] not in known]
    too_old = [f for f in new if since and (f.get("lastModifiedDateTime") or "")[:10] < since]
    return {"in_folder": len(invoices), "already_read": len(invoices) - len(new), "too_old": len(too_old),
            "next_check": len(new) - len(too_old) - len(wanted)}


@once_per_database
def ensure_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS purchase_invoice_files (
                file_id VARCHAR(100) PRIMARY KEY,
                drive_id VARCHAR(200) NOT NULL,
                filename VARCHAR(255) NOT NULL,
                size_bytes BIGINT,
                status VARCHAR(20) NOT NULL,
                error TEXT,
                result JSONB,
                moved_to VARCHAR(255),
                move_error TEXT,
                batch_id INTEGER,
                found_at TIMESTAMP NOT NULL,
                started_at TIMESTAMP NOT NULL,
                done_by VARCHAR(100),
                done_at TIMESTAMP
            )""")
        cur.execute("CREATE INDEX IF NOT EXISTS purchase_invoice_files_status ON purchase_invoice_files (status)")
        # The step a read is on (download, read, scan, match, save), for the page's progress bars.
        cur.execute("ALTER TABLE purchase_invoice_files ADD COLUMN IF NOT EXISTS stage VARCHAR(20)")
    conn.commit()


def add_new(conn, drive_id: str, files: list, since: str, max_files: int, now: datetime.datetime) -> tuple:
    """Add a row for each new file. Returns (IDs added, folder_counts). Two
    checks at once cannot both add a file: the second finds the row already there."""
    ensure_table(conn)
    added = []
    with conn.cursor() as cur:
        cur.execute("SELECT file_id FROM purchase_invoice_files WHERE file_id = ANY(%s)", ([f["id"] for f in files],))
        known = {r[0] for r in cur.fetchall()}
        wanted = wanted_files(files, known, since, max_files)
        for f in wanted:
            cur.execute("""
                INSERT INTO purchase_invoice_files (file_id, drive_id, filename, size_bytes, status, found_at, started_at)
                VALUES (%s, %s, %s, %s, 'queued', %s, %s)
                ON CONFLICT (file_id) DO NOTHING RETURNING file_id""",
                (f["id"], drive_id, f["name"][:255], f.get("size"), now, now))
            row = cur.fetchone()
            if row:
                added.append(row[0])
    conn.commit()
    return added, folder_counts(files, known, since, wanted)


def claim(conn, file_id: str, now: datetime.datetime) -> dict | None:
    """Queued -> reading, in one UPDATE, so a Lambda retry finds it already
    taken and reads nothing."""
    ensure_table(conn)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            UPDATE purchase_invoice_files SET status = 'reading', stage = 'download', started_at = %s
            WHERE file_id = %s AND status = 'queued'
            RETURNING file_id, drive_id, filename, size_bytes, moved_to""", (now, file_id))
        row = cur.fetchone()
    conn.commit()
    return dict(row) if row else None


def save_stage(conn, file_id: str, stage: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE purchase_invoice_files SET stage = %s WHERE file_id = %s", (stage, file_id))
    conn.commit()


def save_read(conn, file_id: str, result: dict, now: datetime.datetime) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE purchase_invoice_files SET status = 'pending', result = %s, error = NULL, done_at = %s
            WHERE file_id = %s""", (json.dumps(result), now, file_id))
    conn.commit()


def save_failed(conn, file_id: str, message: str, now: datetime.datetime) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE purchase_invoice_files SET status = 'failed', error = %s, done_at = %s
            WHERE file_id = %s""", (message, now, file_id))
    conn.commit()


def save_move(conn, file_id: str, moved_to: str | None, move_error: str | None) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE purchase_invoice_files SET moved_to = %s, move_error = %s WHERE file_id = %s",
                    (moved_to, move_error, file_id))
    conn.commit()


def list_open(conn, now: datetime.datetime) -> list:
    """Everything still needing someone, newest first. A read that never
    finished is failed here, so it can be retried and the page stops waiting."""
    ensure_table(conn)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            UPDATE purchase_invoice_files SET status = 'failed', error = 'Reading the file did not finish', done_at = %s
            WHERE status IN ('queued', 'reading') AND started_at < %s""",
            (now, now - datetime.timedelta(minutes=STALE_MINUTES)))
        cur.execute("""
            SELECT file_id, filename, status, stage, error, moved_to, move_error, found_at,
                   result->>'supplier_name' AS supplier_name, result->>'supplier_text' AS supplier_text,
                   COALESCE((result->>'supplier_matched')::boolean, FALSE) AS supplier_matched,
                   result->>'invoice_number' AS invoice_number,
                   result->>'invoice_date' AS invoice_date, result->'invoice_gross' AS invoice_gross,
                   jsonb_array_length(result->'vehicles') AS vehicles
            FROM purchase_invoice_files
            WHERE status IN ('queued', 'reading', 'pending', 'failed')
            ORDER BY found_at DESC, filename LIMIT 200""")
        rows = [dict(r) for r in cur.fetchall()]
    conn.commit()
    return rows


def read_one(conn, file_id: str) -> dict | None:
    ensure_table(conn)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT file_id, drive_id, filename, status, result FROM purchase_invoice_files WHERE file_id = %s", (file_id,))
        row = cur.fetchone()
    conn.commit()
    return dict(row) if row else None


def retry(conn, file_id: str, now: datetime.datetime) -> bool:
    ensure_table(conn)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE purchase_invoice_files SET status = 'queued', stage = NULL, error = NULL, started_at = %s, done_at = NULL
            WHERE file_id = %s AND status IN ('failed', 'pending')""", (now, file_id))
        changed = cur.rowcount > 0
    conn.commit()
    return changed


def apply_supplier_names(result: dict, supplier_names: list, supplier_ids: dict) -> bool:
    """Look a pending invoice's supplier up again in the Supplier Names list.
    Returns True if it changed. Left alone: an invoice read before its text was
    kept, a supplier named on the form itself (a sale agreement), and a name
    Textract read that is still not on the list."""
    if not result.get("found_text") or (result.get("supplier_matched") and not result.get("supplier_text")):
        return False
    supplier, text = cb.match_supplier(result["found_text"], supplier_names)
    if text == result.get("supplier_text") and supplier == result.get("supplier_name"):
        return False
    if supplier is None and not result.get("supplier_text"):
        return False
    result.update(supplier_name=supplier, supplier_text=text, supplier_matched=bool(supplier),
                  supplier_id=supplier_ids.get(str(supplier or "").lower()))
    return True


def rematch_suppliers(conn, supplier_names: list, supplier_ids: dict) -> int:
    """After the Supplier Names list changes: update the pending invoices.
    Returns how many changed."""
    ensure_table(conn)
    changed = 0
    with conn.cursor() as cur:
        cur.execute("SELECT file_id, result FROM purchase_invoice_files WHERE status = 'pending'")
        for file_id, result in cur.fetchall():
            if result and apply_supplier_names(result, supplier_names, supplier_ids):
                cur.execute("UPDATE purchase_invoice_files SET result = %s WHERE file_id = %s", (json.dumps(result), file_id))
                changed += 1
    conn.commit()
    return changed


def ignore(conn, file_id: str, user: str, now: datetime.datetime) -> bool:
    ensure_table(conn)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE purchase_invoice_files SET status = 'ignored', done_by = %s, done_at = %s
            WHERE file_id = %s AND status IN ('pending', 'failed')""", (user, now, file_id))
        changed = cur.rowcount > 0
    conn.commit()
    return changed


def mark_submitted(conn, file_id: str, batch_id: int, user: str, now: datetime.datetime) -> None:
    ensure_table(conn)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE purchase_invoice_files SET status = 'submitted', batch_id = %s, done_by = %s, done_at = %s
            WHERE file_id = %s""", (batch_id, user, now, file_id))
    conn.commit()
