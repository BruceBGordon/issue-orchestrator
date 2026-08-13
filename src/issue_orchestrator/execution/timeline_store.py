"""SQLite-backed timeline store."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .timeline_artifact_expectations import RUN_SCOPED_TIMELINE_EVENTS, event_requires_run_dir
from ..infra.sqlite_connection import open_sqlite
from ..infra.timeline_trace import is_timeline_trace_enabled
from ..ports.timeline_store import TimelineRecord, TimelineStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TimelineStoreConfig:
    max_records: int = 5000
    max_total_records: int = 250000


_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS timeline_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_number INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    source_event TEXT NOT NULL DEFAULT '',
    timestamp TEXT NOT NULL,
    event TEXT NOT NULL,
    run_dir TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL,
    instance_id TEXT NOT NULL DEFAULT '',
    CHECK (
        source_event NOT IN ({run_scoped_events})
        OR length(trim(run_dir)) > 0
    )
);

CREATE INDEX IF NOT EXISTS idx_timeline_issue_sequence
    ON timeline_events(issue_number, sequence DESC);

CREATE INDEX IF NOT EXISTS idx_timeline_issue_event_run_dir
    ON timeline_events(issue_number, event, run_dir);

CREATE INDEX IF NOT EXISTS idx_timeline_issue_run_dir
    ON timeline_events(issue_number, run_dir);

CREATE INDEX IF NOT EXISTS idx_timeline_issue_source_event
    ON timeline_events(issue_number, source_event);

CREATE INDEX IF NOT EXISTS idx_timeline_instance_id
    ON timeline_events(instance_id);
"""

_SQLITE_SCHEMA_VERSION = 4


def _quoted_csv(values: Iterable[str]) -> str:
    return ", ".join(f"'{value}'" for value in sorted(values))


def _sqlite_schema_sql() -> str:
    return _SQLITE_SCHEMA.format(run_scoped_events=_quoted_csv(RUN_SCOPED_TIMELINE_EVENTS))


class SqliteTimelineStore(TimelineStore):
    """SQLite-backed timeline store."""

    def __init__(
        self,
        db_path: Path,
        config: TimelineStoreConfig | None = None,
        instance_id: str = "",
    ) -> None:
        self._db_path = db_path
        self._config = config or TimelineStoreConfig()
        self._instance_id = instance_id
        self._connection: sqlite3.Connection | None = None
        self._connection_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._db_inode: int | None = None
        self.initialize()

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def instance_id(self) -> str:
        return self._instance_id

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection_lock:
            conn = self._get_connection()
            self._ensure_schema(conn)
            self._db_inode = self._capture_db_inode()
        logger.info(
            "Timeline store initialized: db=%s inode=%s pid=%d",
            self._db_path,
            self._db_inode,
            os.getpid(),
        )

    def _get_connection(self) -> sqlite3.Connection:
        with self._connection_lock:
            self._assert_db_file_unchanged()
            if self._connection is None:
                self._connection = open_sqlite(
                    self._db_path,
                    row_factory=sqlite3.Row,
                    check_same_thread=False,
                )
                self._ensure_schema(self._connection)
                if self._db_inode is None:
                    self._db_inode = self._capture_db_inode()
            return self._connection

    def close(self) -> None:
        """Close the owned SQLite connection."""
        with self._connection_lock:
            conn = self._connection
            self._connection = None
            if conn is not None:
                conn.close()

    def __enter__(self) -> "SqliteTimelineStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        version_row = conn.execute("PRAGMA user_version").fetchone()
        user_version = int(version_row[0]) if version_row else 0
        if user_version != _SQLITE_SCHEMA_VERSION:
            # No backwards-compatibility path: rebuild timeline table to enforce invariants.
            conn.executescript(
                """
                DROP TABLE IF EXISTS timeline_events;
                DROP INDEX IF EXISTS idx_timeline_issue_sequence;
                DROP INDEX IF EXISTS idx_timeline_issue_event_run_dir;
                """
            )
            conn.executescript(_sqlite_schema_sql())
            conn.execute(f"PRAGMA user_version = {_SQLITE_SCHEMA_VERSION}")
            conn.commit()
            return
        conn.executescript(_sqlite_schema_sql())

    def _capture_db_inode(self) -> int | None:
        try:
            return os.stat(self._db_path).st_ino
        except FileNotFoundError:
            return None

    def check_health(self) -> None:
        """Verify the DB file still exists with the expected inode.

        Raises RuntimeError if the file was deleted or replaced.
        Call this at tick boundaries to detect state file tampering early.
        """
        self._assert_db_file_unchanged()

    def _assert_db_file_unchanged(self) -> None:
        expected = self._db_inode
        if expected is None:
            return
        current = self._capture_db_inode()
        if current is None:
            raise RuntimeError(
                f"Timeline DB missing on disk: {self._db_path}. "
                "Database file was removed while store is active."
            )
        if current != expected:
            raise RuntimeError(
                f"Timeline DB replaced on disk: {self._db_path} "
                f"(expected inode={expected}, current inode={current}). "
                "Failing fast to avoid split-brain timeline writes."
            )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            with self._connection_lock:
                conn = self._get_connection()
                try:
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

    def append(self, issue_number: int, record: TimelineRecord) -> None:
        source = record.source_event or record.event
        run_dir_value = record.data.get("run_dir")
        run_dir = run_dir_value.strip() if isinstance(run_dir_value, str) else ""
        if event_requires_run_dir(source) and not run_dir:
            raise RuntimeError(
                f"timeline DB invariant failed: event={source} requires non-empty run_dir"
            )
        payload = json.dumps(record.data, sort_keys=True, default=str)
        with self._transaction() as tx:
            tx.execute(
                """
                INSERT INTO timeline_events
                    (issue_number, event_id, source_event, timestamp, event, run_dir, data_json, instance_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (issue_number, record.event_id, source, record.timestamp, record.event, run_dir, payload, self._instance_id),
            )
            self._trim_if_needed(tx, issue_number)
            self._trim_total_if_needed(tx)
        if _timeline_trace_enabled():
            logger.info(
                "[TIMELINE] append db=%s issue=%s event=%s source=%s event_id=%s",
                self._db_path,
                issue_number,
                record.event,
                source,
                record.event_id,
            )

    def read(self, issue_number: int, limit: int | None = None) -> list[TimelineRecord]:
        with self._connection_lock:
            conn = self._get_connection()
            if limit is not None and limit > 0:
                rows = conn.execute(
                    """
                    SELECT event_id, source_event, timestamp, event, data_json, instance_id
                    FROM timeline_events
                    WHERE issue_number = ?
                    ORDER BY sequence DESC
                    LIMIT ?
                    """,
                    (issue_number, limit),
                ).fetchall()
                rows = list(reversed(rows))
            else:
                rows = conn.execute(
                    """
                    SELECT event_id, source_event, timestamp, event, data_json, instance_id
                    FROM timeline_events
                    WHERE issue_number = ?
                    ORDER BY sequence ASC
                    """,
                    (issue_number,),
                ).fetchall()

        records: list[TimelineRecord] = []
        for row in rows:
            data_json = row["data_json"] or "{}"
            try:
                data = json.loads(data_json)
            except json.JSONDecodeError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            records.append(
                TimelineRecord(
                    event_id=str(row["event_id"]),
                    timestamp=str(row["timestamp"]),
                    event=str(row["event"]),
                    data=data,
                    source_event=str(row["source_event"] or ""),
                    instance_id=str(row["instance_id"] or ""),
                )
            )
        if _timeline_trace_enabled():
            logger.info(
                "[TIMELINE] read db=%s issue=%s count=%s limit=%s",
                self._db_path,
                issue_number,
                len(records),
                limit,
            )
        return records

    def delete(self, issue_number: int) -> int:
        with self._transaction() as tx:
            tx.execute(
                "DELETE FROM timeline_events WHERE issue_number = ?",
                (issue_number,),
            )
            deleted = tx.execute("SELECT changes()").fetchone()[0]
        logger.info("[TIMELINE] delete db=%s issue=%s deleted=%s", self._db_path, issue_number, deleted)
        return int(deleted)

    def references_run(self, issue_number: int, run_dir: Path) -> bool:
        """Return whether the durable Timeline owns this exact issue/run pair."""
        with self._connection_lock:
            row = (
                self._get_connection()
                .execute(
                    """
                SELECT 1
                FROM timeline_events
                WHERE issue_number = ? AND run_dir = ?
                LIMIT 1
                """,
                    (issue_number, str(run_dir)),
                )
                .fetchone()
            )
        return row is not None

    def relocate_run(
        self,
        issue_number: int,
        old_run_dir: Path,
        new_run_dir: Path,
    ) -> int:
        """Atomically rewrite every reference to one archived run directory."""
        old = str(old_run_dir)
        new = str(new_run_dir)
        if not old or not new or old == new:
            raise ValueError("Timeline run relocation requires two distinct paths")

        def rewrite(value: object) -> object:
            if isinstance(value, str):
                if value == old:
                    return new
                prefix = old + os.sep
                if value.startswith(prefix):
                    return new + value[len(old) :]
                return value
            if isinstance(value, list):
                return [rewrite(item) for item in value]
            if isinstance(value, dict):
                return {key: rewrite(item) for key, item in value.items()}
            return value

        relocated = 0
        with self._transaction() as tx:
            rows = tx.execute(
                """
                SELECT sequence, data_json
                FROM timeline_events
                WHERE issue_number = ? AND run_dir = ?
                """,
                (issue_number, old),
            ).fetchall()
            for row in rows:
                payload = json.loads(row["data_json"] or "{}")
                rewritten = rewrite(payload)
                tx.execute(
                    """
                    UPDATE timeline_events
                    SET run_dir = ?, data_json = ?
                    WHERE sequence = ?
                    """,
                    (
                        new,
                        json.dumps(rewritten, sort_keys=True, default=str),
                        int(row["sequence"]),
                    ),
                )
                relocated += 1
        logger.info(
            "[TIMELINE] relocate_run issue=%s old=%s new=%s records=%s",
            issue_number,
            old,
            new,
            relocated,
        )
        return relocated

    def _trim_if_needed(self, conn: sqlite3.Connection, issue_number: int) -> None:
        max_records = self._config.max_records
        if max_records <= 0:
            return
        before_count = 0
        if _timeline_trace_enabled():
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM timeline_events WHERE issue_number = ?",
                (issue_number,),
            ).fetchone()
            before_count = int(row["count"]) if row else 0

        conn.execute(
            """
            DELETE FROM timeline_events
            WHERE issue_number = ?
              AND sequence NOT IN (
                SELECT sequence
                FROM timeline_events
                WHERE issue_number = ?
                ORDER BY sequence DESC
                LIMIT ?
              )
            """,
            (issue_number, issue_number, max_records),
        )
        if _timeline_trace_enabled():
            after_row = conn.execute(
                "SELECT COUNT(*) AS count FROM timeline_events WHERE issue_number = ?",
                (issue_number,),
            ).fetchone()
            after_count = int(after_row["count"]) if after_row else 0
            deleted = max(0, before_count - after_count)
            logger.info(
                "[TIMELINE] trim_issue db=%s issue=%s max=%s before=%s after=%s deleted=%s",
                self._db_path,
                issue_number,
                max_records,
                before_count,
                after_count,
                deleted,
            )

    def _trim_total_if_needed(self, conn: sqlite3.Connection) -> None:
        max_total_records = self._config.max_total_records
        if max_total_records <= 0:
            return
        before_count = 0
        if _timeline_trace_enabled():
            row = conn.execute("SELECT COUNT(*) AS count FROM timeline_events").fetchone()
            before_count = int(row["count"]) if row else 0

        conn.execute(
            """
            DELETE FROM timeline_events
            WHERE sequence NOT IN (
                SELECT sequence
                FROM timeline_events
                ORDER BY sequence DESC
                LIMIT ?
            )
            """,
            (max_total_records,),
        )
        if _timeline_trace_enabled():
            after_row = conn.execute("SELECT COUNT(*) AS count FROM timeline_events").fetchone()
            after_count = int(after_row["count"]) if after_row else 0
            deleted = max(0, before_count - after_count)
            logger.info(
                "[TIMELINE] trim_total db=%s max=%s before=%s after=%s deleted=%s",
                self._db_path,
                max_total_records,
                before_count,
                after_count,
                deleted,
            )


def _timeline_trace_enabled() -> bool:
    return is_timeline_trace_enabled()


def read_timeline_records(
    db_path: Path,
    issue_number: int,
    limit: int | None = None,
) -> list[TimelineRecord]:
    """Read a timeline from a short-lived SQLite store and close it."""
    with SqliteTimelineStore(db_path=db_path) as store:
        return store.read(issue_number, limit=limit)
