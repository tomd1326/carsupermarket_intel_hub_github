"""Run a table or column setup function once per database, per process.

Many helpers start with CREATE TABLE IF NOT EXISTS / ALTER TABLE ... ADD COLUMN
IF NOT EXISTS so a table appears the first time it is needed. Running that on
every call costs a round trip to Aurora (about 250ms from a desk) and an ALTER
takes a lock on the table, so it is worth doing only once.

The record is kept per database (host, port, name), not as one flag, because the
same helper can be handed a connection to the dev cluster or the prod one, and
setting up one says nothing about the other.

The wrapped function must commit its own work. The database is only recorded as
done once the function has returned, i.e. after that commit, so a failure is
tried again on the next call rather than remembered as done.

A connection that does not report a real host and database name (a test double,
for instance) is never recorded, so the setup simply runs every time as before.
"""
import functools
import threading

_done: set = set()
_lock = threading.Lock()


def _database_key(conn_or_cursor):
    # A cursor carries its connection; a connection is used as it is.
    conn = getattr(conn_or_cursor, 'connection', conn_or_cursor)
    info = getattr(conn, 'info', None)
    host = getattr(info, 'host', None)
    dbname = getattr(info, 'dbname', None)
    port = getattr(info, 'port', None)
    if not isinstance(host, str) or not isinstance(dbname, str):
        return None
    return (host, port, dbname)


def once_per_database(fn):
    """Skip fn after it has once completed for the database of its first
    argument (a connection or a cursor)."""

    @functools.wraps(fn)
    def wrapper(conn_or_cursor, *args, **kwargs):
        db = _database_key(conn_or_cursor)
        key = None if db is None else (fn.__module__, fn.__qualname__, db)
        if key is not None and key in _done:
            return None
        result = fn(conn_or_cursor, *args, **kwargs)
        if key is not None:
            with _lock:
                _done.add(key)
        return result

    return wrapper
