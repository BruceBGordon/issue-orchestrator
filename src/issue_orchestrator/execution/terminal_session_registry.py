# pyright: strict
"""SQLite adapter for durable terminal-session launch ownership."""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from ..domain.executor import ExecutorInteractiveSessionCancellation
from ..domain.process_group import ProcessBirthIdentity
from ..domain.terminal_session_registry import (
    PendingTerminalSessionRecord,
    TerminalSessionRecord,
)
from ..domain.terminal_session_termination import (
    TerminalSessionOwnerCancellation,
    TerminalSessionProcess,
)
from ..infra.repo_identity import state_dir
from ..infra.sqlite_connection import open_sqlite


logger = logging.getLogger(__name__)


class TerminalSessionRegistryError(RuntimeError):
    """Persisted terminal ownership cannot be interpreted safely."""


class SqliteTerminalSessionRegistry:
    """Deep SQLite owner for pending-to-identified session publication."""

    def __init__(self, repo_root: Path) -> None:
        if not repo_root.is_absolute():
            raise ValueError(
                "SqliteTerminalSessionRegistry.repo_root must be absolute"
            )
        self._state_dir = state_dir(repo_root)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._state_dir / "session_registry.sqlite"
        self._legacy_db_path = self._state_dir / "subprocess_sessions.sqlite"
        self._legacy_dir = self._state_dir / "subprocess_sessions"
        self._legacy_index = self._state_dir / "subprocess_sessions.json"
        self._legacy_backup = self._legacy_index.with_suffix(".json.bak")
        self._migrate_legacy_db_if_needed()
        self._ensure_db()
        self._migrate_legacy_if_needed()
        self.load()

    def _connect(self) -> sqlite3.Connection:
        return open_sqlite(self._db_path)

    def _ensure_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_name TEXT PRIMARY KEY,
                    issue_number INTEGER NOT NULL,
                    worktree_path TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    log_path TEXT NOT NULL,
                    tab_name TEXT NOT NULL,
                    is_review INTEGER NOT NULL CHECK (is_review IN (0, 1)),
                    run_dir TEXT NOT NULL,
                    process_birth_identity TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_sessions (
                    session_name TEXT PRIMARY KEY,
                    issue_number INTEGER NOT NULL,
                    worktree_path TEXT NOT NULL,
                    registered_at TEXT NOT NULL,
                    log_path TEXT NOT NULL,
                    tab_name TEXT NOT NULL,
                    is_review INTEGER NOT NULL CHECK (is_review IN (0, 1)),
                    run_dir TEXT NOT NULL
                )
                """
            )
            existing_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if "run_dir" not in existing_columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN run_dir TEXT")
            if "process_birth_identity" not in existing_columns:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN process_birth_identity TEXT"
                )
            conn.commit()
        try:
            inode: int | None = os.stat(self._db_path).st_ino
        except FileNotFoundError:
            inode = None
        logger.info(
            "Session registry initialized: db=%s inode=%s pid=%d",
            self._db_path,
            inode,
            os.getpid(),
        )

    def _migrate_legacy_db_if_needed(self) -> None:
        if not self._legacy_db_path.exists() or self._db_path.exists():
            return
        try:
            self._legacy_db_path.replace(self._db_path)
        except OSError as exc:
            raise TerminalSessionRegistryError(
                "could not move the legacy subprocess registry into place"
            ) from exc

    def _migrate_legacy_if_needed(self) -> None:
        if self._db_path.exists() and self._has_rows():
            return
        legacy_sources = tuple(
            path
            for path in (
                self._legacy_index,
                self._legacy_backup,
                self._legacy_dir,
            )
            if path.exists()
        )
        if not legacy_sources:
            return
        raise TerminalSessionRegistryError(
            "legacy subprocess registry cannot be migrated safely because it "
            "does not contain process birth identities: "
            + ", ".join(str(path) for path in legacy_sources)
        )

    def _has_rows(self) -> bool:
        try:
            with self._connect() as conn:
                return conn.execute("SELECT 1 FROM sessions LIMIT 1").fetchone() is not None
        except sqlite3.DatabaseError as exc:
            raise TerminalSessionRegistryError(
                f"could not inspect subprocess registry {self._db_path}"
            ) from exc

    def load(self) -> dict[str, TerminalSessionRecord]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT session_name, issue_number, worktree_path, pid, "
                    "started_at, log_path, tab_name, is_review, run_dir, "
                    "process_birth_identity FROM sessions"
                ).fetchall()
            records: dict[str, TerminalSessionRecord] = {}
            for row in rows:
                session_name = _required_text(row[0], "sessions.session_name")
                run_dir = Path(_required_text(row[8], "sessions.run_dir"))
                records[session_name] = TerminalSessionRecord(
                    session_name=session_name,
                    issue_number=_required_integer(
                        row[1], "sessions.issue_number", minimum=0
                    ),
                    worktree_path=Path(
                        _required_text(row[2], "sessions.worktree_path")
                    ),
                    process=TerminalSessionProcess(
                        process_id=_required_integer(
                            row[3], "sessions.pid", minimum=2
                        ),
                        birth_identity=ProcessBirthIdentity(
                            _required_text(
                                row[9], "sessions.process_birth_identity"
                            )
                        ),
                        terminal_cancellation=(
                            TerminalSessionOwnerCancellation.for_run_dir(run_dir)
                        ),
                        executor_cancellation=(
                            ExecutorInteractiveSessionCancellation.for_run_dir(
                                run_dir
                            )
                        ),
                    ),
                    registered_at=_required_datetime(
                        row[4], "sessions.started_at"
                    ),
                    recording_path=Path(
                        _required_text(row[5], "sessions.log_path")
                    ),
                    tab_name=_required_text(row[6], "sessions.tab_name"),
                    is_review=_required_boolean(row[7], "sessions.is_review"),
                )
            return records
        except sqlite3.DatabaseError as exc:
            raise TerminalSessionRegistryError(
                f"could not read subprocess registry {self._db_path}"
            ) from exc

    def upsert(self, record: TerminalSessionRecord) -> None:
        """Persist an already identified session, primarily for restoration."""
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO sessions (
                        session_name, issue_number, worktree_path, pid, started_at,
                        log_path, tab_name, is_review, run_dir,
                        process_birth_identity
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_name) DO UPDATE SET
                        issue_number=excluded.issue_number,
                        worktree_path=excluded.worktree_path,
                        pid=excluded.pid,
                        started_at=excluded.started_at,
                        log_path=excluded.log_path,
                        tab_name=excluded.tab_name,
                        is_review=excluded.is_review,
                        run_dir=excluded.run_dir,
                        process_birth_identity=excluded.process_birth_identity
                    """,
                    _session_values(record),
                )
                conn.commit()
        except sqlite3.DatabaseError as exc:
            raise TerminalSessionRegistryError(
                f"could not persist subprocess session {record.session_name!r}"
            ) from exc

    def load_pending(self) -> tuple[PendingTerminalSessionRecord, ...]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT session_name, issue_number, worktree_path, "
                    "registered_at, log_path, tab_name, is_review, run_dir "
                    "FROM pending_sessions ORDER BY session_name"
                ).fetchall()
            return tuple(
                PendingTerminalSessionRecord(
                    session_name=_required_text(
                        row[0], "pending_sessions.session_name"
                    ),
                    issue_number=_required_integer(
                        row[1], "pending_sessions.issue_number", minimum=0
                    ),
                    worktree_path=Path(
                        _required_text(row[2], "pending_sessions.worktree_path")
                    ),
                    registered_at=_required_datetime(
                        row[3], "pending_sessions.registered_at"
                    ),
                    recording_path=Path(
                        _required_text(row[4], "pending_sessions.log_path")
                    ),
                    tab_name=_required_text(
                        row[5], "pending_sessions.tab_name"
                    ),
                    is_review=_required_boolean(
                        row[6], "pending_sessions.is_review"
                    ),
                    run_dir=Path(
                        _required_text(row[7], "pending_sessions.run_dir")
                    ),
                )
                for row in rows
            )
        except sqlite3.DatabaseError as exc:
            raise TerminalSessionRegistryError(
                f"could not read pending subprocess launches {self._db_path}"
            ) from exc

    def begin_launch(self, record: PendingTerminalSessionRecord) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO pending_sessions (session_name, issue_number, "
                    "worktree_path, registered_at, log_path, tab_name, is_review, "
                    "run_dir) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.session_name,
                        record.issue_number,
                        str(record.worktree_path),
                        record.registered_at.isoformat(),
                        str(record.recording_path),
                        record.tab_name,
                        1 if record.is_review else 0,
                        str(record.run_dir),
                    ),
                )
                conn.commit()
        except sqlite3.DatabaseError as exc:
            raise TerminalSessionRegistryError(
                f"could not persist terminal launch intent {record.session_name!r}"
            ) from exc

    def commit_launch(
        self,
        pending: PendingTerminalSessionRecord,
        record: TerminalSessionRecord,
    ) -> None:
        if pending.session_name != record.session_name:
            raise ValueError("pending and committed session names must match")
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO sessions (
                        session_name, issue_number, worktree_path, pid, started_at,
                        log_path, tab_name, is_review, run_dir,
                        process_birth_identity
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _session_values(record),
                )
                removed = conn.execute(
                    "DELETE FROM pending_sessions WHERE session_name = ?",
                    (pending.session_name,),
                ).rowcount
                if removed != 1:
                    raise TerminalSessionRegistryError(
                        "terminal launch intent disappeared before commit: "
                        f"{pending.session_name!r}"
                    )
                conn.commit()
        except sqlite3.DatabaseError as exc:
            raise TerminalSessionRegistryError(
                f"could not commit terminal launch {record.session_name!r}"
            ) from exc

    def remove_pending(self, session_name: str) -> None:
        self._delete_named("pending_sessions", session_name)

    def remove(self, session_name: str) -> None:
        self._delete_named("sessions", session_name)

    def _delete_named(self, table: str, session_name: str) -> None:
        if table not in {"pending_sessions", "sessions"}:
            raise ValueError("terminal registry table is a closed set")
        try:
            with self._connect() as conn:
                conn.execute(
                    f"DELETE FROM {table} WHERE session_name = ?",  # noqa: S608
                    (session_name,),
                )
                conn.commit()
        except sqlite3.DatabaseError as exc:
            raise TerminalSessionRegistryError(
                f"could not remove terminal registry record {session_name!r}"
            ) from exc


def _session_values(record: TerminalSessionRecord) -> tuple[object, ...]:
    return (
        record.session_name,
        record.issue_number,
        str(record.worktree_path),
        record.pid,
        record.registered_at.isoformat(),
        str(record.recording_path),
        record.tab_name,
        1 if record.is_review else 0,
        str(record.run_dir),
        record.process.birth_identity.kernel_token,
    )


def _required_text(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise TerminalSessionRegistryError(
            f"terminal registry {field_name} must be non-empty text"
        )
    return value


def _required_integer(value: object, field_name: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise TerminalSessionRegistryError(
            f"terminal registry {field_name} must be an integer >= {minimum}"
        )
    return value


def _required_boolean(value: object, field_name: str) -> bool:
    if type(value) is not int or value not in (0, 1):
        raise TerminalSessionRegistryError(
            f"terminal registry {field_name} must be exactly 0 or 1"
        )
    return value == 1


def _required_datetime(value: object, field_name: str) -> datetime:
    raw = _required_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise TerminalSessionRegistryError(
            f"terminal registry {field_name} must be an ISO datetime"
        ) from exc
    if parsed.tzinfo is None:
        raise TerminalSessionRegistryError(
            f"terminal registry {field_name} must include a timezone"
        )
    return parsed
