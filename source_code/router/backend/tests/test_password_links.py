"""Password rules and the set-password link functions in db_utils.

Run from source_code/router/backend:
    poetry run pytest tests/test_password_links.py
"""

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from utils import db_utils
from utils.password_rules import password_error, failed_rules


def _fake_conn(fetchone=None):
    cur = mock.MagicMock()
    cur.fetchone.side_effect = fetchone if isinstance(fetchone, list) else [fetchone] * 10
    conn = mock.MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


def test_good_password_passes():
    assert password_error("Abcde1!") is None


def test_each_rule_is_checked():
    assert failed_rules("abcde1!") == ['upper']
    assert failed_rules("Abcdef!") == ['number']
    assert failed_rules("Abcde12") == ['special']
    assert failed_rules("Ab1!") == ['length']
    assert failed_rules("") == ['length', 'number', 'upper', 'special']


def test_error_names_what_is_missing():
    assert "a number" in password_error("Abcdef!")


def test_peek_returns_none_without_a_token():
    conn, cur = _fake_conn()
    assert db_utils.peek_password_link(conn, "") is None
    cur.execute.assert_not_called()


def test_peek_returns_none_when_table_missing():
    conn, _ = _fake_conn(fetchone=(0, 0))
    assert db_utils.peek_password_link(conn, "abc") is None


def test_peek_never_updates():
    # present check, then the link row
    conn, cur = _fake_conn(fetchone=[(1, 1), ('jo', 'reset')])
    assert db_utils.peek_password_link(conn, "abc") == ('jo', 'reset')
    sql = [c.args[0].upper() for c in cur.execute.call_args_list]
    assert not any(s.lstrip().startswith(('UPDATE', 'INSERT', 'DELETE')) for s in sql)


def test_redeem_with_invalid_link_changes_nothing():
    conn, cur = _fake_conn(fetchone=[(1, 1), None])
    assert db_utils.redeem_password_link(conn, "abc", "hash") is None
    conn.rollback.assert_called()
    conn.commit.assert_not_called()


def test_redeem_rolls_back_when_user_update_misses():
    conn, cur = _fake_conn(fetchone=[(1, 1), ('jo', 'activate')])
    cur.rowcount = 0
    assert db_utils.redeem_password_link(conn, "abc", "hash") is None
    conn.rollback.assert_called()
    conn.commit.assert_not_called()


def test_redeem_commits_once_when_everything_worked():
    conn, cur = _fake_conn(fetchone=[(1, 1), ('jo', 'activate')])
    cur.rowcount = 1
    assert db_utils.redeem_password_link(conn, "abc", "hash") == ('jo', 'activate')
    conn.commit.assert_called_once()


def test_token_is_stored_only_as_a_hash():
    conn, cur = _fake_conn(fetchone=[(1, 1), ('2099-01-01',)])
    token, _ = db_utils.create_password_link(conn, "jo", "reset", 48, "admin")
    inserted = [c.args[1] for c in cur.execute.call_args_list if 'INSERT' in c.args[0].upper()][0]
    assert token not in inserted
    assert inserted[0] == db_utils._hash_link_token(token)
    assert len(inserted[0]) == 64


def test_pending_user_hash_can_never_match_a_login():
    conn, _ = _fake_conn(fetchone=(1, 1))
    pwd_hash = db_utils.add_pending_user(conn, "jo")
    assert pwd_hash.startswith('!')
