"""Centralized SQLite connection helpers."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable, Literal, TypedDict


SqliteIsolationLevel = Literal["DEFERRED", "IMMEDIATE", "EXCLUSIVE"]


class _SqliteConnectOptions(TypedDict, total=False):
    timeout: float
    check_same_thread: bool
    isolation_level: SqliteIsolationLevel | None



def open_sqlite(
    path: Path | str,
    *,
    timeout: float | None = None,
    check_same_thread: bool | None = None,
    isolation_level: SqliteIsolationLevel | None = None,
    row_factory: Callable[[sqlite3.Cursor, tuple[object, ...]], object] | None = None,
    pragmas: bool = True,
) -> sqlite3.Connection:
    """Open a SQLite connection with consistent pragmas and options."""
    kwargs = _SqliteConnectOptions()
    if timeout is not None:
        kwargs["timeout"] = timeout
    if check_same_thread is not None:
        kwargs["check_same_thread"] = check_same_thread
    if isolation_level is not None:
        kwargs["isolation_level"] = isolation_level

    conn = sqlite3.connect(str(path), **kwargs)
    if row_factory is not None:
        conn.row_factory = row_factory
    if pragmas:
        _apply_pragmas(conn)
    return conn


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Apply durability pragmas."""
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    for attempt in range(5):
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            break
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 4:
                raise
            time.sleep(0.1 * (2 ** attempt))
    conn.execute("PRAGMA synchronous = FULL")
