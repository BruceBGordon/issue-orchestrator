"""SQLite home for the local tech-lead run history (ADR-0033 / #6858).

Its own database file, not another table in ``tech_lead_authority.sqlite``, for
a reason the two lifetimes make plain: the authority store holds LOAD-BEARING
rows — the launch grant completion validates against, the gated ops an approval
executes — and it is a retention owner that deletes them at each run's terminal.
This store holds the opposite: rows that exist only after they stop mattering to
control flow, and whose whole value is that nothing deletes them. Sharing one
file would put an operator's six-week history behind the same schema migrations
and the same corruption blast radius as a trust boundary.

Every write is best-effort, per the port's exception contract: an unwritable
history is a lost receipt, never a reason to fail the run that earned it.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from ..domain.tech_lead_run import TechLeadRunScopeKind
from ..domain.tech_lead_run_artifacts import TechLeadRunArtifacts, kinds_from_values
from ..domain.tech_lead_run_record import TechLeadRunPhase, TechLeadRunRecord
from ..domain.tech_lead_session import TechLeadSessionFlavor
from .repo_identity import state_dir
from .sqlite_connection import open_sqlite

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tech_lead_run_records (
    run_id TEXT NOT NULL,
    session_name TEXT NOT NULL,
    run_key TEXT NOT NULL,
    scope_kind TEXT NOT NULL,
    flavor TEXT NOT NULL,
    phase TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL DEFAULT '',
    subject_issue_number INTEGER NOT NULL DEFAULT 0,
    subject_title TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    findings INTEGER NOT NULL DEFAULT 0,
    proposals INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, session_name)
);
CREATE INDEX IF NOT EXISTS idx_tech_lead_run_records_started
    ON tech_lead_run_records (started_at DESC);
"""

# Columns added after the table's first shape, with the value an existing row
# gets. Applied idempotently at initialization because every write here is
# best-effort: a column this build expects and an older file lacks would turn
# "history is a receipt" into "history silently stopped being written".
_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    # The coordination anchor, recorded as an anchor rather than as the run's
    # subject (#6858 F5).
    ("anchor_issue_number", "INTEGER NOT NULL DEFAULT 0"),
    # Where the run's preserved artifacts live, and which kinds are there
    # (#6858 F4). Empty for a run whose artifacts were not preserved.
    ("artifact_dir", "TEXT NOT NULL DEFAULT ''"),
    ("artifact_kinds", "TEXT NOT NULL DEFAULT ''"),
)


class SqliteTechLeadRunRecordStore:
    """Durable, append-mostly history of this engine's tech-lead runs."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.initialize()

    @classmethod
    def for_repo(cls, repo_root: Path) -> "SqliteTechLeadRunRecordStore":
        """Store handle for a repository's orchestrator state directory.

        Called only by the composition root (and adapter tests); control code
        depends on the injected ``TechLeadRunRecordStore`` port instead.
        """
        return cls(state_dir(repo_root) / "tech_lead_runs.sqlite")

    def initialize(self) -> None:
        """Create or migrate the history table.

        Raises on an unusable database rather than degrading here: the STORE
        cannot know whether losing durability is acceptable. That choice belongs
        to the composition root, which selects the in-memory implementation
        instead when this fails (#6858 round 1 F2).
        """
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_connection()
        conn.executescript(SCHEMA)
        self._add_missing_columns(conn)
        conn.commit()

    def _add_missing_columns(self, conn: sqlite3.Connection) -> None:
        existing = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(tech_lead_run_records)")
        }
        for column, declaration in _ADDED_COLUMNS:
            if column not in existing:
                conn.execute(
                    "ALTER TABLE tech_lead_run_records"
                    f" ADD COLUMN {column} {declaration}"
                )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def open_run(self, record: TechLeadRunRecord) -> None:
        """Record that a run started, replacing any earlier row for the run."""
        self._write(
            "INSERT OR REPLACE INTO tech_lead_run_records ("
            " run_id, session_name, run_key, scope_kind, flavor, phase,"
            " started_at, ended_at, subject_issue_number, subject_title,"
            " anchor_issue_number, detail, findings, proposals,"
            " artifact_dir, artifact_kinds"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.run_id,
                record.session_name,
                record.run_key,
                record.scope_kind.value,
                record.flavor.value,
                record.phase.value,
                record.started_at.isoformat(),
                record.ended_at.isoformat() if record.ended_at else "",
                record.subject_issue_number,
                record.subject_title,
                record.anchor_issue_number,
                record.detail,
                record.findings,
                record.proposals,
                *_artifact_columns(record.artifacts),
            ),
            what=f"open run {record.run_key}",
        )

    def conclude_run(
        self,
        *,
        run_id: str,
        session_name: str,
        phase: TechLeadRunPhase,
        ended_at: datetime,
        detail: str = "",
        findings: int = 0,
        proposals: int = 0,
        artifacts: Optional[TechLeadRunArtifacts] = None,
    ) -> None:
        """Close an open row. Silently no-ops when this engine opened none.

        The ``phase = 'running'`` predicate is the once-only guard: a publish
        retry re-enters completion for the same session run, and a second
        conclusion would overwrite the first verdict with whatever the retry
        happened to see.
        """
        artifact_dir, artifact_kinds = _artifact_columns(artifacts)
        self._write(
            "UPDATE tech_lead_run_records SET phase = ?, ended_at = ?,"
            " detail = ?, findings = ?, proposals = ?,"
            " artifact_dir = ?, artifact_kinds = ?"
            " WHERE run_id = ? AND session_name = ? AND phase = ?",
            (
                phase.value,
                ended_at.isoformat(),
                detail,
                findings,
                proposals,
                artifact_dir,
                artifact_kinds,
                run_id,
                session_name,
                TechLeadRunPhase.RUNNING.value,
            ),
            what=f"conclude run {run_id}/{session_name}",
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def recent(self, *, limit: int) -> tuple[TechLeadRunRecord, ...]:
        """The newest ``limit`` runs, most recently started first."""
        try:
            rows = self._get_connection().execute(
                "SELECT * FROM tech_lead_run_records"
                " ORDER BY started_at DESC LIMIT ?",
                (max(0, limit),),
            ).fetchall()
        except sqlite3.Error:
            logger.warning(
                "[TECH_LEAD_RUN] Could not read the local run history",
                exc_info=True,
            )
            return ()
        return tuple(record for record in map(_record_from_row, rows) if record)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _write(self, sql: str, params: tuple, *, what: str) -> None:
        try:
            with self._transaction() as conn:
                conn.execute(sql, params)
        except sqlite3.Error:
            # Per the port contract: history is a receipt, so a store failure
            # is logged and dropped rather than propagated into the run.
            logger.warning(
                "[TECH_LEAD_RUN] Could not %s in the local run history",
                what,
                exc_info=True,
            )

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


def _artifact_columns(
    artifacts: Optional[TechLeadRunArtifacts],
) -> tuple[str, str]:
    """The two stored columns for an artifact locator, or empty for none."""
    if artifacts is None:
        return ("", "")
    return (
        str(artifacts.location),
        ",".join(kind.value for kind in artifacts.kinds),
    )


def _artifacts_from_row(row: sqlite3.Row) -> Optional[TechLeadRunArtifacts]:
    """Rehydrate an artifact locator, or ``None`` when the row has none.

    A stored location that is no longer on disk yields ``None``: the archive is
    engine-owned and nothing prunes it, but an operator who moved or wiped their
    state directory should see "no drill-down" rather than a button that 404s.
    """
    location = str(row["artifact_dir"]).strip()
    kinds = kinds_from_values(
        tuple(part for part in str(row["artifact_kinds"]).split(",") if part)
    )
    if not location or not kinds:
        return None
    path = Path(location)
    if not path.is_absolute() or not path.is_dir():
        return None
    return TechLeadRunArtifacts(location=path, kinds=kinds)


def _record_from_row(row: sqlite3.Row) -> Optional[TechLeadRunRecord]:
    """Rehydrate one row, or ``None`` when it no longer parses.

    A row whose vocabulary this build does not recognise (an older engine's
    flavor, a hand-edited phase) is DROPPED from the history rather than
    crashing the dashboard that reads it. This is inspection, not control: the
    cost of an unreadable row is one missing history entry.
    """
    try:
        ended = str(row["ended_at"])
        return TechLeadRunRecord(
            run_key=str(row["run_key"]),
            scope_kind=TechLeadRunScopeKind(str(row["scope_kind"])),
            flavor=TechLeadSessionFlavor(str(row["flavor"])),
            phase=TechLeadRunPhase(str(row["phase"])),
            started_at=datetime.fromisoformat(str(row["started_at"])),
            run_id=str(row["run_id"]),
            session_name=str(row["session_name"]),
            subject_issue_number=int(row["subject_issue_number"]),
            subject_title=str(row["subject_title"]),
            anchor_issue_number=int(row["anchor_issue_number"]),
            ended_at=datetime.fromisoformat(ended) if ended else None,
            detail=str(row["detail"]),
            findings=int(row["findings"]),
            proposals=int(row["proposals"]),
            artifacts=_artifacts_from_row(row),
        )
    except (ValueError, TypeError, KeyError):
        logger.warning(
            "[TECH_LEAD_RUN] Dropping an unreadable local run-history row",
            exc_info=True,
        )
        return None


__all__ = ["SqliteTechLeadRunRecordStore"]
