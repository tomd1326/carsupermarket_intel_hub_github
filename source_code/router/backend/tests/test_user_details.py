"""read_user_details, used by the account menu on every page.

The risk this covers is the menu's read running ALTER TABLE: load_user_details
does, and that locks the users table on each call.

Run from source_code/router/backend:
    poetry run pytest tests/test_user_details.py
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from utils import db_utils


def _fake_conn(rows):
    cur = mock.MagicMock()
    cur.fetchall.return_value = rows
    conn = mock.MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


def _sql_run(cur):
    return [c.args[0] for c in cur.execute.call_args_list]


def test_read_runs_no_alter_table():
    conn, cur = _fake_conn([('buyer', 'Jo', 'Bloggs', 'jo@example.com')])
    db_utils.read_user_details(conn)
    assert not any('ALTER' in sql.upper() for sql in _sql_run(cur))


def test_read_returns_details_with_empty_strings_for_missing():
    conn, _ = _fake_conn([('buyer', 'Jo', None, None)])
    details = db_utils.read_user_details(conn)
    assert details['buyer'] == {'first_name': 'Jo', 'last_name': '', 'email': ''}


def test_read_lays_admin_details_over_stored_row():
    conn, _ = _fake_conn([('tdrayson', None, None, None)])
    details = db_utils.read_user_details(conn)
    assert details['tdrayson'] == db_utils.ADMIN_DETAILS['tdrayson']


def test_load_user_details_still_checks_columns():
    conn, cur = _fake_conn([])
    db_utils.load_user_details(conn)
    assert any('ALTER' in sql.upper() for sql in _sql_run(cur))
