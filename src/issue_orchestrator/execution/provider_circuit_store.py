"""SQLite-backed provider circuit breaker store."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..infra.sqlite_connection import open_sqlite
from ..ports.provider_resilience import (
    ProviderCircuitState,
    ProviderEvidenceWatermarks,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_circuit (
    provider TEXT PRIMARY KEY,
    transient_open_until TEXT,
    transient_observed_at TEXT,
    consecutive_outages INTEGER NOT NULL,
    last_error_summary TEXT,
    updated_at TEXT NOT NULL,
    consecutive_auth_failures INTEGER NOT NULL DEFAULT 0,
    auth_open_until TEXT,
    last_auth_sample_id TEXT NOT NULL DEFAULT '',
    quota_open_until TEXT,
    consecutive_quota_failures INTEGER NOT NULL DEFAULT 0,
    quota_observed_at TEXT
);

CREATE TABLE IF NOT EXISTS provider_evidence (
    provider TEXT PRIMARY KEY,
    success_observed_at TEXT,
    transient_failure_observed_at TEXT,
    quota_failure_observed_at TEXT
);
"""

_SELECT_ONE = """
SELECT provider, transient_open_until, transient_observed_at,
       consecutive_outages, last_error_summary,
       updated_at, consecutive_auth_failures, auth_open_until,
       last_auth_sample_id, quota_open_until, consecutive_quota_failures,
       quota_observed_at
FROM provider_circuit WHERE provider = ?
"""

_SELECT_ALL = """
SELECT provider, transient_open_until, transient_observed_at,
       consecutive_outages, last_error_summary,
       updated_at, consecutive_auth_failures, auth_open_until,
       last_auth_sample_id, quota_open_until, consecutive_quota_failures,
       quota_observed_at
FROM provider_circuit
"""

_SELECT_EVIDENCE = """
SELECT provider, success_observed_at, transient_failure_observed_at,
       quota_failure_observed_at
FROM provider_evidence WHERE provider = ?
"""


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_evidence_dt(value: str | None) -> datetime | None:
    """Parse a load-bearing evidence timestamp without silent degradation."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid provider evidence timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"provider evidence timestamp must be aware: {value!r}")
    return parsed


def _migration_statements(columns: set[str]) -> list[str]:
    """Plan additive/rename migrations for the schema currently on disk."""
    migrations = []
    if "transient_open_until" not in columns and "open_until" in columns:
        migrations.append(
            "ALTER TABLE provider_circuit "
            "RENAME COLUMN open_until TO transient_open_until"
        )
    if "transient_observed_at" not in columns:
        migrations.append(
            "ALTER TABLE provider_circuit ADD COLUMN transient_observed_at TEXT"
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
    if "quota_open_until" not in columns:
        migrations.append(
            "ALTER TABLE provider_circuit ADD COLUMN quota_open_until TEXT"
        )
    if "consecutive_quota_failures" not in columns:
        migrations.append(
            "ALTER TABLE provider_circuit "
            "ADD COLUMN consecutive_quota_failures INTEGER NOT NULL DEFAULT 0"
        )
    if "quota_observed_at" not in columns:
        migrations.append(
            "ALTER TABLE provider_circuit ADD COLUMN quota_observed_at TEXT"
        )
    return migrations


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
        self._backfill_evidence(conn)

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
        add_transient_observed_at = "transient_observed_at" not in columns
        add_quota_observed_at = "quota_observed_at" not in columns
        migrations = _migration_statements(columns)
        if not migrations:
            return
        for statement in migrations:
            conn.execute(statement)
        if add_transient_observed_at:
            conn.execute(
                "UPDATE provider_circuit SET transient_observed_at = updated_at "
                "WHERE transient_open_until IS NOT NULL"
            )
        if add_quota_observed_at:
            # Existing active quota rows predate the explicit per-cause
            # watermark. Their aggregate update time is the safest conservative
            # substitute: recovery must be newer than it before retiring them.
            conn.execute(
                "UPDATE provider_circuit SET quota_observed_at = updated_at "
                "WHERE quota_open_until IS NOT NULL"
            )
        conn.commit()

    @staticmethod
    def _backfill_evidence(conn: sqlite3.Connection) -> None:
        """Seed the durable ledger from active pre-ledger circuit rows."""
        conn.execute(
            """
            INSERT OR IGNORE INTO provider_evidence (
                provider, transient_failure_observed_at,
                quota_failure_observed_at
            )
            SELECT provider, transient_observed_at, quota_observed_at
            FROM provider_circuit
            WHERE transient_observed_at IS NOT NULL
               OR quota_observed_at IS NOT NULL
            """
        )
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
            transient_observed_at=_parse_dt(row["transient_observed_at"]),
            auth_open_until=_parse_dt(row["auth_open_until"]),
            consecutive_outages=int(row["consecutive_outages"]),
            last_error_summary=row["last_error_summary"],
            updated_at=_parse_dt(row["updated_at"]) or datetime.now(timezone.utc),
            consecutive_auth_failures=int(row["consecutive_auth_failures"] or 0),
            last_auth_sample_id=row["last_auth_sample_id"] or "",
            quota_open_until=_parse_dt(row["quota_open_until"]),
            consecutive_quota_failures=int(row["consecutive_quota_failures"] or 0),
            quota_observed_at=_parse_dt(row["quota_observed_at"]),
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

    @staticmethod
    def _save_state(tx: sqlite3.Connection, state: ProviderCircuitState) -> None:
        tx.execute(
            """
            INSERT INTO provider_circuit (
                provider, transient_open_until, transient_observed_at,
                consecutive_outages,
                last_error_summary, updated_at, consecutive_auth_failures,
                auth_open_until, last_auth_sample_id, quota_open_until,
                consecutive_quota_failures, quota_observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
                transient_open_until=excluded.transient_open_until,
                transient_observed_at=excluded.transient_observed_at,
                consecutive_outages=excluded.consecutive_outages,
                last_error_summary=excluded.last_error_summary,
                updated_at=excluded.updated_at,
                consecutive_auth_failures=excluded.consecutive_auth_failures,
                auth_open_until=excluded.auth_open_until,
                last_auth_sample_id=excluded.last_auth_sample_id,
                quota_open_until=excluded.quota_open_until,
                consecutive_quota_failures=excluded.consecutive_quota_failures,
                quota_observed_at=excluded.quota_observed_at
            """,
            (
                state.provider,
                state.transient_open_until.isoformat()
                if state.transient_open_until
                else None,
                state.transient_observed_at.isoformat()
                if state.transient_observed_at
                else None,
                int(state.consecutive_outages),
                state.last_error_summary,
                state.updated_at.isoformat(),
                int(state.consecutive_auth_failures),
                state.auth_open_until.isoformat()
                if state.auth_open_until
                else None,
                state.last_auth_sample_id,
                state.quota_open_until.isoformat()
                if state.quota_open_until
                else None,
                int(state.consecutive_quota_failures),
                state.quota_observed_at.isoformat()
                if state.quota_observed_at
                else None,
            ),
        )

    @staticmethod
    def _save_evidence(
        tx: sqlite3.Connection, evidence: ProviderEvidenceWatermarks
    ) -> None:
        tx.execute(
            """
            INSERT INTO provider_evidence (
                provider, success_observed_at,
                transient_failure_observed_at, quota_failure_observed_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(provider) DO UPDATE SET
                success_observed_at=excluded.success_observed_at,
                transient_failure_observed_at=(
                    excluded.transient_failure_observed_at
                ),
                quota_failure_observed_at=excluded.quota_failure_observed_at
            """,
            (
                evidence.provider,
                evidence.success_observed_at.isoformat()
                if evidence.success_observed_at
                else None,
                evidence.transient_failure_observed_at.isoformat()
                if evidence.transient_failure_observed_at
                else None,
                evidence.quota_failure_observed_at.isoformat()
                if evidence.quota_failure_observed_at
                else None,
            ),
        )

    def save(self, state: ProviderCircuitState) -> None:
        with self._transaction() as tx:
            self._save_state(tx, state)

    def delete(self, provider: str) -> None:
        with self._transaction() as tx:
            tx.execute("DELETE FROM provider_circuit WHERE provider = ?", (provider,))

    def get_evidence(self, provider: str) -> ProviderEvidenceWatermarks | None:
        row = self._get_connection().execute(_SELECT_EVIDENCE, (provider,)).fetchone()
        if row is None:
            return None
        return ProviderEvidenceWatermarks(
            provider=row["provider"],
            success_observed_at=_parse_evidence_dt(row["success_observed_at"]),
            transient_failure_observed_at=_parse_evidence_dt(
                row["transient_failure_observed_at"]
            ),
            quota_failure_observed_at=_parse_evidence_dt(
                row["quota_failure_observed_at"]
            ),
        )

    def save_reduction(
        self,
        evidence: ProviderEvidenceWatermarks,
        state: ProviderCircuitState | None,
    ) -> None:
        with self._transaction() as tx:
            self._save_evidence(tx, evidence)
            if state is None:
                tx.execute(
                    "DELETE FROM provider_circuit WHERE provider = ?",
                    (evidence.provider,),
                )
            else:
                self._save_state(tx, state)
