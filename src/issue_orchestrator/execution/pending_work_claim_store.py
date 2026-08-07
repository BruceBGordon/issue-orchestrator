"""SQLite-backed, orchestrator-owned pending-work claim store (#6999 F7).

Lives in the repository's orchestrator state directory, NOT in the session
worktree. The worktree run directory is handed to the launched agent and is
writable by it, so a claim kept there could be edited by the very process whose
work it describes: an agent could change which queue its session is holding,
which PR a restored rework rewrites, or which paths a tech-lead investigation
admits as evidence roots. Restoration accepts this record as truth, so it has to
sit on the orchestrator's side of that boundary — the same reason tech-lead
launch authority is stored here rather than in the worktree (ADR-0031).

Rows are keyed on the orchestrator-allocated run directory, which is unique per
run, and validated against the run identity recorded with them. Run ids are
timestamps and are NOT unique on their own - two sessions launched in the same
second share one - so keying on identity alone would make one session's launch
collide with another's. Validating identity on read means a manifest rewritten
to rename a run cannot make its session silently restore as claimless: the
mismatch raises, and the restoration seam quarantines that terminal.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..domain.pending_work import PendingWorkClaim
from ..domain.session_run import SessionRunAssets
from ..infra.repo_identity import state_dir
from ..infra.sqlite_connection import open_sqlite
from ..ports.pending_work_claim_store import ConflictingPendingWorkClaimError
from .pending_work_codec import (
    PendingWorkClaimDecodeError,
    decode_claim,
    encode_claim,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_work_claim (
    run_dir TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    session_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""

STORE_FILENAME = "pending_work_claims.sqlite"


class SqlitePendingWorkClaimStore:
    """Orchestrator-owned claim storage for one repository."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.initialize()

    @classmethod
    def for_repo(cls, repo_root: Path) -> "SqlitePendingWorkClaimStore":
        """Store handle for a repository's orchestrator state directory.

        Called only by the composition root (and adapter tests); control code
        depends on the injected ``PendingWorkClaimStore`` port instead.
        """
        return cls(state_dir(repo_root) / STORE_FILENAME)

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._get_connection().executescript(_SCHEMA)

    def write_pending_work_claim(
        self, run: SessionRunAssets, claim: PendingWorkClaim
    ) -> None:
        """Create-once: identical is a no-op, different is a conflict."""
        key = self._key(run)
        payload = json.dumps(encode_claim(claim), sort_keys=True)
        identity = run.identity
        with self._write_lock, self._transaction() as conn:
            row = conn.execute(
                "SELECT run_id, session_name, payload FROM pending_work_claim "
                "WHERE run_dir = ?",
                (key,),
            ).fetchone()
            if row is not None:
                if (
                    row["run_id"] == identity.run_id
                    and row["session_name"] == identity.session_name
                    and row["payload"] == payload
                ):
                    return
                raise ConflictingPendingWorkClaimError(
                    f"run {key} already holds a different pending-work claim "
                    f"(run {row['run_id']}, session {row['session_name']!r}); "
                    f"refusing to overwrite it with one for run "
                    f"{identity.run_id}, session {identity.session_name!r}"
                )
            conn.execute(
                "INSERT INTO pending_work_claim "
                "(run_dir, run_id, session_name, started_at, payload) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    key,
                    identity.run_id,
                    identity.session_name,
                    identity.started_at,
                    payload,
                ),
            )

    def read_pending_work_claim(
        self, run: SessionRunAssets
    ) -> PendingWorkClaim | None:
        key = self._key(run)
        row = self._get_connection().execute(
            "SELECT run_id, session_name, payload FROM pending_work_claim "
            "WHERE run_dir = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        identity = run.identity
        if (
            row["run_id"] != identity.run_id
            or row["session_name"] != identity.session_name
        ):
            # The run root matched but the identity recorded against it did not.
            # The identity comes from the worktree manifest, which the agent can
            # write; refusing here is what turns a rewritten manifest into a
            # quarantined terminal instead of a silently claimless one.
            raise PendingWorkClaimDecodeError(
                f"run {key} holds a claim recorded for run {row['run_id']}, "
                f"session {row['session_name']!r}, not run {identity.run_id}, "
                f"session {identity.session_name!r}"
            )
        try:
            payload = json.loads(row["payload"])
        except json.JSONDecodeError as exc:
            raise PendingWorkClaimDecodeError(
                f"pending work claim for run {key} is unreadable: {exc}"
            ) from exc
        return decode_claim(payload)

    def clear_pending_work_claim(self, run: SessionRunAssets) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "DELETE FROM pending_work_claim WHERE run_dir = ?", (self._key(run),)
            )

    @staticmethod
    def _key(run: SessionRunAssets) -> str:
        """The orchestrator-allocated run root, resolved to a stable string."""
        return str(run.run_dir.resolve())

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
