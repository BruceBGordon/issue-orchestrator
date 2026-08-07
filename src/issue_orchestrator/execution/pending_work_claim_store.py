"""SQLite-backed, orchestrator-owned pending-work ledger (#6999 F7/F8/F11/F12).

Lives in the repository's orchestrator state directory, NOT in the session
worktree. The worktree run directory is handed to the launched agent and is
writable by it, so a claim kept there could be edited by the very process whose
work it describes: an agent could change which queue its session is holding,
which PR a restored rework rewrites, or which paths a tech-lead investigation
admits as evidence roots. Restoration accepts this record as truth, so it has to
sit on the orchestrator's side of that boundary — the same reason tech-lead
launch authority is stored here rather than in the worktree (ADR-0031).

Two things follow from that boundary and are easy to get wrong:

* The row key is the run root **lexically normalised, never symlink-resolved**
  (#6999 F11). The run directory sits in the agent-writable worktree, so
  resolving it on every access would let an agent retarget the key with a
  symlink: the lookup would land on a different row, return "no claim", and the
  terminal would be admitted as claimless instead of quarantined. The lexical
  path is the one the orchestrator allocated and the terminal registry recorded.
* EVERY recorded identity field is validated on read, ``started_at`` included.
  Identity comes from the worktree manifest, and ``started_at`` later becomes
  trusted tech-lead evidence chronology, so accepting a rewritten one would
  launder agent-controlled data into orchestrator authority.

The quarantine table shares this database because it shares its trust boundary
and its lifetime, but not its lifecycle: a quarantine outlives the claim it
could not read and is cleared by a human, never by a session outcome.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..domain.pending_work import PendingWorkClaim
from ..domain.session_run import SessionRunAssets
from ..infra.repo_identity import state_dir
from ..infra.sqlite_connection import open_sqlite
from ..ports.pending_work_claim_store import (
    ConflictingPendingWorkClaimError,
    UnreadableClaim,
    UnresolvedClaim,
)
from .pending_work_codec import (
    PendingWorkClaimDecodeError,
    decode_claim,
    encode_claim,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_work_claim (
    run_key TEXT PRIMARY KEY,
    work_key TEXT NOT NULL,
    deferred INTEGER NOT NULL DEFAULT 0,
    session_name TEXT NOT NULL,
    run_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pending_work_claim_work
    ON pending_work_claim (work_key);
CREATE TABLE IF NOT EXISTS pending_work_claim_quarantine (
    run_key TEXT PRIMARY KEY,
    session_name TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    error TEXT NOT NULL,
    escalated INTEGER NOT NULL DEFAULT 0
);
"""

STORE_FILENAME = "pending_work_claims.sqlite"


class SqlitePendingWorkClaimStore:
    """Orchestrator-owned claim ledger and quarantine record for one repository."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.initialize()

    @classmethod
    def for_repo(cls, repo_root: Path) -> "SqlitePendingWorkClaimStore":
        """Store handle for a repository's orchestrator state directory.

        Called only by the composition root (and adapter tests); control code
        depends on the injected ports instead.
        """
        return cls(state_dir(repo_root) / STORE_FILENAME)

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_connection()
        self._migrate(conn)
        conn.executescript(_SCHEMA)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Reshape a table written before the durable states existed.

        ``CREATE TABLE IF NOT EXISTS`` leaves an existing table untouched, so a
        database written against the earlier run-keyed-only schema would keep a
        shape with no ``work_key`` or ``deferred`` column and every later
        statement would fail. The rows describe runs of a process that is no
        longer running, so they are dropped rather than migrated: a restart
        already re-derives live claims from discovery, and keeping a row whose
        state cannot be known would be worse than starting clean.
        """
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(pending_work_claim)")
        }
        if columns and not {"work_key", "deferred"} <= columns:
            conn.execute("DROP TABLE pending_work_claim")
            conn.commit()

    # -- claim lifecycle ---------------------------------------------------

    def hold_pending_work_claim(
        self, run: SessionRunAssets, claim: PendingWorkClaim
    ) -> None:
        key = self.run_key_for(run)
        payload = json.dumps(encode_claim(claim), sort_keys=True)
        identity = run.identity
        work_key = claim.work_key()
        with self._write_lock, self._transaction() as conn:
            # Relaunching the work is what resolves its earlier deferral, and it
            # resolves it whichever run deferred it. This runs BEFORE the
            # conflict check because run roots are named from a second-resolution
            # timestamp: a relaunch in the same second reuses the directory, and
            # the stale deferred row must not be mistaken for a rival claim.
            # Same transaction as the new hold, so the two can never both be
            # missing (#6999 F8).
            conn.execute(
                "DELETE FROM pending_work_claim WHERE work_key = ? AND deferred = 1",
                (work_key,),
            )
            row = conn.execute(
                "SELECT session_name, run_id, started_at, payload "
                "FROM pending_work_claim WHERE run_key = ?",
                (key,),
            ).fetchone()
            if row is not None:
                if self._identity_matches(row, identity) and row["payload"] == payload:
                    return
                raise ConflictingPendingWorkClaimError(
                    f"run {key} already holds a different pending-work claim "
                    f"(run {row['run_id']}, session {row['session_name']!r}); "
                    f"refusing to overwrite it with one for run "
                    f"{identity.run_id}, session {identity.session_name!r}"
                )
            conn.execute(
                "INSERT INTO pending_work_claim "
                "(run_key, work_key, deferred, session_name, run_id, started_at, "
                "payload) VALUES (?, ?, 0, ?, ?, ?, ?)",
                (
                    key,
                    work_key,
                    identity.session_name,
                    identity.run_id,
                    identity.started_at,
                    payload,
                ),
            )

    def defer_pending_work_claim(self, run: SessionRunAssets) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "UPDATE pending_work_claim SET deferred = 1 WHERE run_key = ?",
                (self.run_key_for(run),),
            )

    def consume_pending_work_claim(self, run: SessionRunAssets) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "DELETE FROM pending_work_claim WHERE run_key = ?",
                (self.run_key_for(run),),
            )

    def read_pending_work_claim(
        self, run: SessionRunAssets
    ) -> PendingWorkClaim | None:
        key = self.run_key_for(run)
        row = self._get_connection().execute(
            "SELECT session_name, run_id, started_at, deferred, payload "
            "FROM pending_work_claim WHERE run_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        identity = run.identity
        if not self._identity_matches(row, identity):
            # The run root matched but the identity recorded against it did not.
            # Identity comes from the worktree manifest, which the agent can
            # write; refusing here is what turns a rewritten manifest into a
            # quarantined terminal instead of a silently claimless one.
            raise PendingWorkClaimDecodeError(
                f"run {key} holds a claim recorded for run {row['run_id']}, "
                f"session {row['session_name']!r}, started {row['started_at']!r}; "
                f"asked for run {identity.run_id}, session "
                f"{identity.session_name!r}, started {identity.started_at!r}"
            )
        if row["deferred"]:
            # Deferred work belongs to the queue, not to this run. Reporting it
            # as held would let a stale terminal settle work already re-queued.
            return None
        return self._decode(row["payload"], key)

    # -- startup recovery --------------------------------------------------

    def list_unresolved_claims(self) -> tuple[UnresolvedClaim, ...]:
        unresolved: list[UnresolvedClaim] = []
        for row in self._all_rows():
            try:
                claim = self._decode(row["payload"], row["run_key"])
            except PendingWorkClaimDecodeError:
                continue  # reported by list_unreadable_claims
            unresolved.append(
                UnresolvedClaim(
                    run_key=row["run_key"],
                    session_name=row["session_name"],
                    deferred=bool(row["deferred"]),
                    claim=claim,
                )
            )
        return tuple(unresolved)

    def list_unreadable_claims(self) -> tuple[UnreadableClaim, ...]:
        unreadable: list[UnreadableClaim] = []
        for row in self._all_rows():
            try:
                self._decode(row["payload"], row["run_key"])
            except PendingWorkClaimDecodeError as exc:
                unreadable.append(
                    UnreadableClaim(
                        run_key=row["run_key"],
                        session_name=row["session_name"],
                        error=str(exc),
                    )
                )
        return tuple(unreadable)

    def resolve_by_run_key(self, run_key: str) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "DELETE FROM pending_work_claim WHERE run_key = ?", (run_key,)
            )

    def run_key_for(self, run: SessionRunAssets) -> str:
        """The run root, lexically normalised and never symlink-resolved.

        ``Path.resolve()`` would follow symlinks inside the agent-writable
        worktree, letting an agent retarget the key at another run (#6999 F11).
        ``os.path.normpath`` collapses ``.``/``..`` and separators without
        touching the filesystem, so the key is exactly the path the orchestrator
        allocated and the terminal registry recorded.
        """
        return os.path.normpath(str(run.run_dir))

    # -- quarantine --------------------------------------------------------

    def record_quarantine(
        self, run_key: str, *, session_name: str, issue_number: int, error: str
    ) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "INSERT INTO pending_work_claim_quarantine "
                "(run_key, session_name, issue_number, error, escalated) "
                "VALUES (?, ?, ?, ?, 0) "
                "ON CONFLICT(run_key) DO UPDATE SET error = excluded.error",
                (run_key, session_name, issue_number, error),
            )

    def mark_quarantine_escalated(self, run_key: str) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "UPDATE pending_work_claim_quarantine SET escalated = 1 "
                "WHERE run_key = ?",
                (run_key,),
            )

    def is_quarantine_escalated(self, run_key: str) -> bool:
        row = self._get_connection().execute(
            "SELECT escalated FROM pending_work_claim_quarantine WHERE run_key = ?",
            (run_key,),
        ).fetchone()
        return bool(row and row["escalated"])

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _identity_matches(row: sqlite3.Row, identity) -> bool:
        """Every recorded identity field, ``started_at`` included (#6999 F11)."""
        return (
            row["session_name"] == identity.session_name
            and row["run_id"] == identity.run_id
            and row["started_at"] == identity.started_at
        )

    @staticmethod
    def _decode(payload: str, key: str) -> PendingWorkClaim:
        try:
            loaded = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PendingWorkClaimDecodeError(
                f"pending work claim for run {key} is unreadable: {exc}"
            ) from exc
        return decode_claim(loaded)

    def _all_rows(self) -> list[sqlite3.Row]:
        return list(
            self._get_connection().execute(
                "SELECT run_key, session_name, deferred, payload "
                "FROM pending_work_claim"
            )
        )

    def _get_connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = open_sqlite(self._db_path, row_factory=sqlite3.Row)
            self._local.conn = conn
        return conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._get_connection()
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        conn.commit()


__all__ = ["STORE_FILENAME", "SqlitePendingWorkClaimStore"]
