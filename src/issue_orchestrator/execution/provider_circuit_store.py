"""SQLite-backed provider circuit breaker store."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..infra.sqlite_connection import open_sqlite
from ..ports.provider_resilience import ProviderCircuitState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_circuit (
    provider TEXT PRIMARY KEY,
    transient_open_until TEXT,
    consecutive_outages INTEGER NOT NULL,
    last_error_summary TEXT,
    updated_at TEXT NOT NULL,
    consecutive_auth_failures INTEGER NOT NULL DEFAULT 0,
    auth_open_until TEXT,
    last_auth_sample_id TEXT NOT NULL DEFAULT ''
);
"""

_COLUMNS = """
provider, transient_open_until, consecutive_outages, last_error_summary,
updated_at, consecutive_auth_failures, auth_open_until, last_auth_sample_id
"""

_SELECT_ONE = f"SELECT {_COLUMNS} FROM provider_circuit WHERE provider = ?"

_SELECT_ALL = f"SELECT {_COLUMNS} FROM provider_circuit"


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class SQLiteProviderCircuitStore:
    """SQLite-backed ProviderCircuitStore."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.initialize()

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_connection()
        conn.executescript(_SCHEMA)
        self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Reshape a table written before the per-cause deadlines existed.

        ``CREATE TABLE IF NOT EXISTS`` leaves an existing table untouched, so a
        database written against the single-``open_until`` schema needs both the
        rename and the added columns. The old column becomes the *transient*
        deadline: that is the only cause it could ever have recorded, since the
        auth dimension did not exist while it was being written.
        """
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(provider_circuit)")
        }
        migrations = []
        if "transient_open_until" not in columns and "open_until" in columns:
            migrations.append(
                "ALTER TABLE provider_circuit "
                "RENAME COLUMN open_until TO transient_open_until"
            )
        if "consecutive_auth_failures" not in columns:
            migrations.append(
                "ALTER TABLE provider_circuit "
                "ADD COLUMN consecutive_auth_failures INTEGER NOT NULL DEFAULT 0"
            )
        if "auth_open_until" not in columns:
            migrations.append(
                "ALTER TABLE provider_circuit ADD COLUMN auth_open_until TEXT"
            )
        if "last_auth_sample_id" not in columns:
            migrations.append(
                "ALTER TABLE provider_circuit "
                "ADD COLUMN last_auth_sample_id TEXT NOT NULL DEFAULT ''"
            )
        if not migrations:
            return
        for statement in migrations:
            conn.execute(statement)
        conn.commit()

    def _get_connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = open_sqlite(self._db_path, row_factory=sqlite3.Row)
            self._local.conn = conn
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            conn = self._get_connection()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _to_state(row: sqlite3.Row) -> ProviderCircuitState:
        return ProviderCircuitState(
            provider=row["provider"],
            transient_open_until=_parse_dt(row["transient_open_until"]),
            auth_open_until=_parse_dt(row["auth_open_until"]),
            consecutive_outages=int(row["consecutive_outages"]),
            last_error_summary=row["last_error_summary"],
            updated_at=_parse_dt(row["updated_at"]) or datetime.now(timezone.utc),
            consecutive_auth_failures=int(row["consecutive_auth_failures"] or 0),
            last_auth_sample_id=row["last_auth_sample_id"] or "",
        )

    def get(self, provider: str) -> ProviderCircuitState | None:
        conn = self._get_connection()
        row = conn.execute(_SELECT_ONE, (provider,)).fetchone()
        if row is None:
            return None
        return self._to_state(row)

    def list_all(self) -> list[ProviderCircuitState]:
        conn = self._get_connection()
        rows = conn.execute(_SELECT_ALL).fetchall()
        return [self._to_state(row) for row in rows]

    def save(self, state: ProviderCircuitState) -> None:
        with self._transaction() as tx:
            tx.execute(
                f"""
                INSERT INTO provider_circuit ({_COLUMNS})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    transient_open_until=excluded.transient_open_until,
                    consecutive_outages=excluded.consecutive_outages,
                    last_error_summary=excluded.last_error_summary,
                    updated_at=excluded.updated_at,
                    consecutive_auth_failures=excluded.consecutive_auth_failures,
                    auth_open_until=excluded.auth_open_until,
                    last_auth_sample_id=excluded.last_auth_sample_id
                """,
                (
                    state.provider,
                    state.transient_open_until.isoformat()
                    if state.transient_open_until
                    else None,
                    int(state.consecutive_outages),
                    state.last_error_summary,
                    state.updated_at.isoformat(),
                    int(state.consecutive_auth_failures),
                    state.auth_open_until.isoformat()
                    if state.auth_open_until
                    else None,
                    state.last_auth_sample_id,
                ),
            )

    def delete(self, provider: str) -> None:
        with self._transaction() as tx:
            tx.execute("DELETE FROM provider_circuit WHERE provider = ?", (provider,))
