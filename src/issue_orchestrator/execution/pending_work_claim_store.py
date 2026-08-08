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
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..domain.pending_work import PendingWorkClaim, PendingWorkKind
from ..domain.session_run import SessionRunAssets
from ..infra.repo_identity import state_dir
from ..infra.sqlite_connection import open_sqlite
from ..ports.pending_work_claim_store import (
    ClaimLookup,
    ClaimState,
    ConflictingPendingWorkClaimError,
    QuarantineCause,
    QuarantineLabelState,
    QuarantineRecord,
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
    issue_number INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pending_work_claim_work
    ON pending_work_claim (work_key);
CREATE TABLE IF NOT EXISTS pending_work_claim_quarantine (
    quarantine_key TEXT PRIMARY KEY,
    run_key TEXT NOT NULL,
    session_name TEXT NOT NULL,
    issue_number INTEGER NOT NULL,
    error TEXT NOT NULL,
    -- Durable state machine (#6999 F12). ``label_state`` records whether THIS
    -- quarantine actually acquired the shared blocking label or found it
    -- already there, so release can only ever remove a label it added.
    -- ``announced`` is separate because AddLabel can land while the comment
    -- fails. ``releasing`` marks a resolved cause whose cleanup has not yet
    -- committed, so the row survives to be retried.
    label_state TEXT NOT NULL DEFAULT 'unknown',
    announced INTEGER NOT NULL DEFAULT 0,
    releasing INTEGER NOT NULL DEFAULT 0,
    -- The observation the announcement was written for, and the work it names
    -- (#6999 F6). Durable because ``announced`` is: a quarantine re-observed
    -- under a DIFFERENT cause has to rewrite the operator's story, and the only
    -- way to know it changed is to have kept the one that was announced.
    -- Nullable on purpose - a row from before this column read as "no cause
    -- recorded", which must differ from every observable cause so the next
    -- scan re-announces rather than standing on a story nothing vouches for.
    cause TEXT,
    work_kind TEXT
);
-- Durable provenance for every OTHER cause of the shared needs-human block
-- (#6999 F2 round 2). The tech-lead marker label and the quarantine table
-- above already record their own. A session or planner escalation recorded
-- nothing, so a remover saw an owner-less label and took it off. Rows are
-- meaningful only while the label is present and are dropped with it, so a
-- stale one can never strand an issue in needs-human.
-- NOTE: no semicolons in this comment - the schema is split on them.
CREATE TABLE IF NOT EXISTS needs_human_cause (
    issue_number INTEGER NOT NULL,
    cause TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (issue_number, cause)
);
"""

# Additive columns the quarantine table gained after it shipped. ``CREATE TABLE
# IF NOT EXISTS`` leaves an existing table exactly as it is, so a database
# written by an earlier build keeps the old shape and every later statement
# referencing these columns fails. Unlike the claim table this needs no
# all-or-nothing rebuild: quarantines carry no queued work, so a NULL cause is
# recoverable by the next scan re-announcing (#6999 F6).
_QUARANTINE_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("cause", "TEXT"),
    ("work_kind", "TEXT"),
)

STORE_FILENAME = "pending_work_claims.sqlite"


def _schema_statements() -> tuple[str, ...]:
    """The schema as individual statements.

    ``executescript`` commits any pending transaction before it runs, so it
    cannot be used inside the migration's single transaction (#6999 F13).
    """
    return tuple(
        statement.strip()
        for statement in _SCHEMA.split(";")
        if statement.strip()
    )

logger = logging.getLogger(__name__)


class PendingWorkClaimMigrationError(RuntimeError):
    """An older ledger holds a row that cannot be carried forward (#6999 F13).

    Raised before anything is renamed or dropped, so the legacy table stays
    exactly as it was. Failing startup is the correct outcome: the alternative
    is running with an authoritative record the orchestrator cannot see, which
    is precisely how a live terminal gets admitted as carrying no work.
    """


def _quarantine_record(row: sqlite3.Row) -> QuarantineRecord:
    return QuarantineRecord(
        quarantine_key=str(row["quarantine_key"]),
        run_key=str(row["run_key"]),
        session_name=str(row["session_name"]),
        issue_number=int(row["issue_number"]),
        error=str(row["error"]),
        label_state=QuarantineLabelState(row["label_state"]),
        announced=bool(row["announced"]),
        releasing=bool(row["releasing"]),
        cause=QuarantineCause(row["cause"]) if row["cause"] else None,
        work_kind=PendingWorkKind(row["work_kind"]) if row["work_kind"] else None,
    )


def _issue_number_of(claim: PendingWorkClaim) -> int:
    """Trusted issue number for a claim being migrated forward."""
    request = claim.request
    resolver = getattr(request, "resolve_issue_number", None)
    if resolver is not None:
        return int(resolver() or 0)
    return int(getattr(request, "issue_number", 0))


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
        for statement in _schema_statements():
            conn.execute(statement)
        self._add_missing_quarantine_columns(conn)
        conn.commit()

    @staticmethod
    def _add_missing_quarantine_columns(conn: sqlite3.Connection) -> None:
        """Bring an earlier quarantine table up to the current column set."""
        existing = {
            row[1]
            for row in conn.execute("PRAGMA table_info(pending_work_claim_quarantine)")
        }
        for column, declaration in _QUARANTINE_ADDED_COLUMNS:
            if column not in existing:
                conn.execute(
                    "ALTER TABLE pending_work_claim_quarantine "
                    f"ADD COLUMN {column} {declaration}"
                )

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Carry an older table forward WITHOUT losing a single claim.

        ``CREATE TABLE IF NOT EXISTS`` leaves an existing table untouched, so a
        database written against an earlier shape would keep columns the new
        statements do not know about. Dropping it is not an option (#6999 F13):
        these rows are the only authoritative copy of work that has already left
        its queue, and the whole reason this table exists is that terminal
        discovery CANNOT reconstruct a typed queued request. An upgrade with a
        live review, validation retry, rework or failure investigation would
        delete exactly the record restoration is about to need.

        So the migration is all-or-nothing. Every row is decoded FIRST, before
        anything is renamed or created. If even one cannot be rebuilt, nothing
        is touched and initialization raises: the legacy table remains the
        authority, intact and inspectable. Archiving the bad row into a side
        table while startup carried on was worse than the drop it replaced -
        the row became operationally invisible, so a surviving terminal for it
        read as ABSENT and could be admitted claimless.
        """
        leftover = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name = 'pending_work_claim_old'"
        ).fetchone()
        if leftover is not None:
            # Belt and braces: the single transaction below makes a surviving
            # ``_old`` table unreachable, but a database that somehow reached
            # that state must never be started against silently - the whole
            # point of F13 is that its rows are still the authority.
            raise PendingWorkClaimMigrationError(
                "pending_work_claim_old is present, so a previous migration did "
                "not complete. It still holds authoritative claims; resolve it "
                "before starting again."
            )
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(pending_work_claim)")
        }
        if not columns or {"work_key", "deferred", "issue_number"} <= columns:
            return
        legacy = list(
            conn.execute(
                "SELECT run_key, session_name, run_id, started_at, payload "
                "FROM pending_work_claim"
            )
        )
        migrated: list[tuple[object, ...]] = []
        for row in legacy:
            try:
                claim = decode_claim(json.loads(row["payload"]))
            except (PendingWorkClaimDecodeError, json.JSONDecodeError, TypeError) as exc:
                raise PendingWorkClaimMigrationError(
                    f"pending-work claim for run {row['run_key']} cannot be "
                    f"migrated to the current schema: {exc}. The existing table "
                    "is left untouched and remains authoritative; resolve or "
                    "remove that row before starting again."
                ) from exc
            migrated.append(
                (
                    row["run_key"],
                    claim.work_key(),
                    row["session_name"],
                    row["run_id"],
                    row["started_at"],
                    _issue_number_of(claim),
                    row["payload"],
                )
            )
        # ONE transaction for rename + create + copy + drop (#6999 F13).
        # ``executescript`` would commit any pending transaction before running,
        # which is exactly how a stop mid-migration could leave a current-shaped
        # table beside the renamed original - after which the column check
        # ABOVE returns early on the next start and the real rows are invisible.
        # The schema statements are therefore executed individually, inside an
        # explicit transaction that rolls back as a unit.
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "ALTER TABLE pending_work_claim RENAME TO pending_work_claim_old"
            )
            for statement in _schema_statements():
                conn.execute(statement)
            conn.executemany(
                "INSERT OR REPLACE INTO pending_work_claim "
                "(run_key, work_key, deferred, session_name, run_id, started_at, "
                "issue_number, payload) VALUES (?, ?, 0, ?, ?, ?, ?, ?)",
                migrated,
            )
            conn.execute("DROP TABLE pending_work_claim_old")
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        logger.info(
            "[WORK] Migrated %d pending-work claim(s) to the current schema",
            len(migrated),
        )

    # -- claim lifecycle ---------------------------------------------------

    def hold_pending_work_claim(
        self, run: SessionRunAssets, claim: PendingWorkClaim, *, issue_number: int
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
                "issue_number, payload) VALUES (?, ?, 0, ?, ?, ?, ?, ?)",
                (
                    key,
                    work_key,
                    identity.session_name,
                    identity.run_id,
                    identity.started_at,
                    issue_number,
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

    def look_up_pending_work_claim(self, run: SessionRunAssets) -> ClaimLookup:
        key = self.run_key_for(run)
        row = self._get_connection().execute(
            "SELECT session_name, run_id, started_at, deferred, payload "
            "FROM pending_work_claim WHERE run_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return ClaimLookup(ClaimState.ABSENT)
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
        claim = self._decode(row["payload"], key)
        if row["deferred"]:
            # Deferred work belongs to the queue, not to this run. Answering
            # ABSENT would let a stale terminal be admitted as claimless and
            # settle work the queue already owns (#6999 F8).
            return ClaimLookup(ClaimState.DEFERRED, claim)
        return ClaimLookup(ClaimState.HELD, claim)

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
                    started_at=str(row["started_at"]),
                    issue_number=int(row["issue_number"]),
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
                        issue_number=int(row["issue_number"]),
                        error=str(exc),
                        started_at=str(row["started_at"]),
                    )
                )
        return tuple(unreadable)

    def retire_deferred_claim(self, work_key: str) -> None:
        """Drop the deferred row for work a launch transaction gave up on."""
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "DELETE FROM pending_work_claim WHERE work_key = ? AND deferred = 1",
                (work_key,),
            )

    def refresh_deferred_claim(
        self, work_key: str, claim: PendingWorkClaim
    ) -> bool:
        """Rewrite the deferred row's payload; report whether one existed.

        ``rowcount`` is the whole point (#6999 F1 round 2): an UPDATE that
        matches nothing is a successful statement and a failed commitment, and
        the caller spending a bounded retry budget has to be able to tell them
        apart.
        """
        payload = json.dumps(encode_claim(claim), sort_keys=True)
        with self._write_lock, self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE pending_work_claim SET payload = ? "
                "WHERE work_key = ? AND deferred = 1",
                (payload, work_key),
            )
            return cursor.rowcount > 0

    def mark_deferred_by_run_key(self, run_key: str) -> None:
        """Keep the row; only a relaunch of the same work may retire it."""
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "UPDATE pending_work_claim SET deferred = 1 WHERE run_key = ?",
                (run_key,),
            )

    def quarantine_key_for(self, run: SessionRunAssets) -> str:
        """Run root PLUS the start instant the ORCHESTRATOR recorded for it.

        Run roots are named from a second-resolution timestamp and created with
        ``exist_ok``, so a replacement run of one session can land on the same
        directory; the start instant has sub-second precision and tells the
        generations apart (#6999 F12).

        It is read from this store's own row rather than from the run assets,
        because those are rebuilt from the worktree manifest the agent can
        write. Taking it from there would let a rewritten timestamp mint a fresh
        quarantine key on every scan, re-commenting forever. Only when no row
        exists at all - nothing was ever held for this run - does the run's own
        value stand in, and no claim can be lost in that case.
        """
        key = self.run_key_for(run)
        row = self._get_connection().execute(
            "SELECT started_at FROM pending_work_claim WHERE run_key = ?",
            (key,),
        ).fetchone()
        recorded = row["started_at"] if row else run.identity.started_at
        return f"{key}@{recorded}"

    def run_key_for_path(self, run_dir: Path) -> str:
        """The key for a run root the orchestrator recorded, before any parsing.

        Discovery hands back a run directory long before ``SessionRunAssets``
        can be rebuilt from it, and a run whose assets fail to parse must still
        be recognised as live (#6999 F14).
        """
        return os.path.normpath(str(run_dir))

    def run_key_for(self, run: SessionRunAssets) -> str:
        """The run root, lexically normalised and never symlink-resolved.

        ``Path.resolve()`` would follow symlinks inside the agent-writable
        worktree, letting an agent retarget the key at another run (#6999 F11).
        ``os.path.normpath`` collapses ``.``/``..`` and separators without
        touching the filesystem, so the key is exactly the path the orchestrator
        allocated and the terminal registry recorded.
        """
        return self.run_key_for_path(run.run_dir)

    # -- shared needs-human provenance -------------------------------------

    def record_needs_human_cause(
        self, issue_number: int, cause: str, *, reason: str
    ) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "INSERT INTO needs_human_cause (issue_number, cause, reason) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(issue_number, cause) DO UPDATE SET "
                "reason = excluded.reason",
                (issue_number, cause, reason),
            )

    def restart_needs_human_causes(
        self, issue_number: int, cause: str, *, reason: str
    ) -> None:
        """Replace every cause with this one, in a single transaction."""
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "DELETE FROM needs_human_cause WHERE issue_number = ?",
                (issue_number,),
            )
            conn.execute(
                "INSERT INTO needs_human_cause (issue_number, cause, reason) "
                "VALUES (?, ?, ?)",
                (issue_number, cause, reason),
            )

    def needs_human_causes(self, issue_number: int) -> frozenset[str]:
        return frozenset(
            str(row["cause"])
            for row in self._get_connection().execute(
                "SELECT cause FROM needs_human_cause WHERE issue_number = ?",
                (issue_number,),
            )
        )

    def withdraw_needs_human_cause(self, issue_number: int, cause: str) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "DELETE FROM needs_human_cause "
                "WHERE issue_number = ? AND cause = ?",
                (issue_number, cause),
            )

    def clear_needs_human_causes(self, issue_number: int) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "DELETE FROM needs_human_cause WHERE issue_number = ?",
                (issue_number,),
            )

    # -- quarantine --------------------------------------------------------

    def record_quarantine(
        self,
        quarantine_key: str,
        *,
        run_key: str,
        session_name: str,
        issue_number: int,
        error: str,
        cause: QuarantineCause,
        work_kind: PendingWorkKind | None,
    ) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "INSERT INTO pending_work_claim_quarantine "
                "(quarantine_key, run_key, session_name, issue_number, error, "
                "cause, work_kind) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(quarantine_key) DO UPDATE SET "
                "error = excluded.error, releasing = 0, "
                "work_kind = excluded.work_kind, "
                # The announcement belongs to the cause that produced it. A
                # changed cause invalidates the comment and the event, so the
                # flag is cleared in the same statement that records the new
                # cause - the two can never disagree (#6999 F6). An unchanged
                # cause keeps it, which is what makes a re-observed quarantine
                # comment exactly once.
                "announced = CASE WHEN pending_work_claim_quarantine.cause "
                "IS excluded.cause THEN pending_work_claim_quarantine.announced "
                "ELSE 0 END, "
                "cause = excluded.cause",
                (
                    quarantine_key,
                    run_key,
                    session_name,
                    issue_number,
                    error,
                    cause.value,
                    work_kind.value if work_kind is not None else None,
                ),
            )

    def read_quarantine(self, quarantine_key: str) -> QuarantineRecord | None:
        row = self._get_connection().execute(
            "SELECT quarantine_key, run_key, session_name, issue_number, error, "
            "label_state, announced, releasing, cause, work_kind "
            "FROM pending_work_claim_quarantine WHERE quarantine_key = ?",
            (quarantine_key,),
        ).fetchone()
        return _quarantine_record(row) if row else None

    def record_quarantine_label_state(
        self, quarantine_key: str, label_state: QuarantineLabelState
    ) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "UPDATE pending_work_claim_quarantine SET label_state = ? "
                "WHERE quarantine_key = ?",
                (label_state.value, quarantine_key),
            )

    def mark_quarantine_announced(self, quarantine_key: str) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "UPDATE pending_work_claim_quarantine SET announced = 1 "
                "WHERE quarantine_key = ?",
                (quarantine_key,),
            )

    def mark_quarantine_releasing(self, quarantine_key: str) -> None:
        """Record that the cause is gone but cleanup has not committed yet."""
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "UPDATE pending_work_claim_quarantine SET releasing = 1 "
                "WHERE quarantine_key = ?",
                (quarantine_key,),
            )

    def release_quarantine(self, quarantine_key: str) -> None:
        with self._write_lock, self._transaction() as conn:
            conn.execute(
                "DELETE FROM pending_work_claim_quarantine WHERE quarantine_key = ?",
                (quarantine_key,),
            )

    def list_quarantines(self) -> tuple[QuarantineRecord, ...]:
        return tuple(
            _quarantine_record(row)
            for row in self._get_connection().execute(
                "SELECT quarantine_key, run_key, session_name, issue_number, "
                "error, label_state, announced, releasing, cause, work_kind "
                "FROM pending_work_claim_quarantine"
            )
        )

    def quarantined_issue_numbers(self) -> frozenset[int]:
        """Issues held open by a quarantine whose cause is still present.

        Rows already being released are excluded: their block is on its way
        out, so nothing should treat them as a live reason to keep it.
        """
        return frozenset(
            int(row["issue_number"])
            for row in self._get_connection().execute(
                "SELECT issue_number FROM pending_work_claim_quarantine "
                "WHERE releasing = 0"
            )
        )

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
                "SELECT run_key, session_name, deferred, issue_number, "
                "started_at, payload FROM pending_work_claim"
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


__all__ = [
    "STORE_FILENAME",
    "PendingWorkClaimMigrationError",
    "SqlitePendingWorkClaimStore",
]
