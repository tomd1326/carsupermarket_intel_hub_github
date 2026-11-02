"""The one supplier list: the `suppliers` table, used by the Supplier Stock
Evaluator and Cars Bought alike.

has_stock_list says whether the supplier sends a stock list. Only those show in
the evaluator's supplier choices; a supplier we only get invoices from does not.
supplier_invoice_names holds the text that identifies a supplier on its invoices.
"""
import psycopg2.extras

from utils.ddl_once import once_per_database

# Any fixed number: makes two people adding the same new supplier take turns.
_ADD_SUPPLIER_LOCK = 720011


@once_per_database
def ensure_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS has_stock_list BOOLEAN NOT NULL DEFAULT TRUE")
        # The same supplier in Snowflake's contacts_suppliers__s. The id is what
        # identifies it, as a few names there are used twice.
        cur.execute("ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS contact_id BIGINT, ADD COLUMN IF NOT EXISTS contact_name VARCHAR(250)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS supplier_invoice_names (
                id SERIAL PRIMARY KEY,
                invoice_text VARCHAR(100) NOT NULL,
                supplier_id INTEGER NOT NULL REFERENCES suppliers (supplier_id) ON DELETE CASCADE
            )""")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS supplier_invoice_names_text ON supplier_invoice_names (LOWER(invoice_text))")
    conn.commit()


def list_suppliers(conn) -> list:
    ensure_tables(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT supplier_id, supplier_name, has_stock_list, contact_id, contact_name FROM suppliers ORDER BY supplier_name")
        rows = cur.fetchall()
    conn.commit()
    return [{"supplier_id": r[0], "supplier_name": r[1], "has_stock_list": r[2], "contact_id": r[3], "contact_name": r[4]} for r in rows]


def save_contacts(conn, rows: list) -> None:
    """Rows of {supplier_id, contact_id, contact_name}. No contact_id clears the mapping."""
    if not rows:
        return
    ensure_tables(conn)
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur, "UPDATE suppliers SET contact_id = %s, contact_name = %s WHERE supplier_id = %s",
            [(r["contact_id"], r["contact_name"], r["supplier_id"]) for r in rows])
    conn.commit()


def _find_or_add(cur, name: str) -> int:
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (_ADD_SUPPLIER_LOCK,))
    cur.execute("SELECT supplier_id FROM suppliers WHERE LOWER(TRIM(supplier_name)) = LOWER(TRIM(%s)) ORDER BY supplier_id LIMIT 1", (name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("INSERT INTO suppliers (supplier_name, has_stock_list) VALUES (%s, FALSE) RETURNING supplier_id", (name.strip(),))
    return cur.fetchone()[0]


def find_or_add_supplier(conn, name: str) -> int:
    """The supplier with this name, whatever its capitals, added if it is new."""
    ensure_tables(conn)
    with conn.cursor() as cur:
        supplier_id = _find_or_add(cur, name)
    conn.commit()
    return supplier_id


def read_invoice_names(conn) -> list:
    """[{text, supplier}]: the text on an invoice and the supplier it means."""
    ensure_tables(conn)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT n.invoice_text, s.supplier_name
            FROM supplier_invoice_names n JOIN suppliers s ON s.supplier_id = n.supplier_id
            ORDER BY s.supplier_name, n.invoice_text""")
        rows = cur.fetchall()
    conn.commit()
    return [{"text": r[0], "supplier": r[1]} for r in rows]


def save_invoice_names(conn, rows: list) -> None:
    """Replace the whole list. A supplier named for the first time is added."""
    ensure_tables(conn)
    with conn.cursor() as cur:
        ids = {}
        for r in rows:
            key = r["supplier"].strip().lower()
            if key not in ids:
                ids[key] = _find_or_add(cur, r["supplier"])
        cur.execute("DELETE FROM supplier_invoice_names")
        if rows:
            psycopg2.extras.execute_values(
                cur, "INSERT INTO supplier_invoice_names (invoice_text, supplier_id) VALUES %s",
                [(r["text"], ids[r["supplier"].strip().lower()]) for r in rows])
    conn.commit()
