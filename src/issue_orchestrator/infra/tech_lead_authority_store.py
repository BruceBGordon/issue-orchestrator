"""SQLite adapter for the ``TechLeadAuthorityStore`` port.

The agent-writable worktree carries copies of the tech_lead assignment and PR
manifest for the *agent* to read; the orchestrator must never treat those
copies as authority (an agent can rewrite them mid-session — #6761 re-review
finding 1). This adapter persists the :class:`TechLeadLaunchAuthority` recorded
at launch, keyed by session run identity, in the per-repo state directory —
the same orchestrator-owned home as ``queue_cache.sqlite`` /
``label_store.sqlite`` — so it survives restarts. It is constructed ONCE at
the composition root (``entrypoints/bootstrap.py``) and injected behind
``ports/tech_lead_authority.py`` into the launch and completion seams (#6769
finding 2); the database is registered in ``infra/sqlite_registry.py`` for
doctor checks, backups, and startup maintenance (#6769 finding 3).

Why not the existing stores:

* ``QueueCacheStore`` owns the in-scope issue snapshot with replace-all
  semantics (``save_snapshot`` wipes, ``clear()`` resets the warm cache);
  piggybacking launch authority onto its meta table couples two unrelated
  lifecycles and a cache reset would destroy authority mid-session.
* ``JsonSessionStore`` persists best-effort (save errors are swallowed) and
  is reachable only where SessionStore is injected — the completion action
  planner is constructed inside ``completion_handler`` with (config,
  repository_host, label_manager) only, so an injected instance cannot reach
  it; concurrent JSON read-modify-write from launcher + completion would
  also race.
* ``OrchestratorState`` + label recovery is in-memory/label-shaped; labels
  cannot carry a per-run manifest PR set.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..domain.models import DiscoveredFailure
from ..domain.tech_lead_findings import (
    VALID_PROMOTION_STATES,
    PatternEvidence,
    PromotedFinding,
    PromotionState,
)
from ..domain.tech_lead_session import (
    StoredTechLeadOp,
    TechLeadLaunchAuthority,
    TechLeadShippedFixSummary,
)
from ..ports.tech_lead_authority import (
    TechLeadAuthorityConflictError,
    TechLeadOpConflictError,
    TechLeadPatternConflictError,
    TechLeadPromotionConflictError,
    TechLeadShippedFixConflictError,
    TechLeadStormCohortConflictError,
    UnknownTechLeadPatternError,
)
from .repo_identity import state_dir
from .sqlite_connection import open_sqlite

logger = logging.getLogger(__name__)


def _cohort_from_payload(payload: str) -> tuple[DiscoveredFailure, ...]:
    """Rehydrate a stored cohort payload into typed failure facts."""
    return tuple(DiscoveredFailure.from_dict(item) for item in json.loads(payload))


def _promotion_from_row(row: sqlite3.Row) -> PromotedFinding:
    """Project one promoted-finding row onto its typed domain value (#6957)."""
    state = str(row["state"])
    if state not in VALID_PROMOTION_STATES:
        raise ValueError(
            f"tech_lead_promoted_findings row for signature"
            f" {str(row['signature'])!r} has unknown state {state!r}"
        )
    return PromotedFinding(
        signature=str(row["signature"]),
        case_file_issue_number=int(row["case_file_issue_number"]),
        target_repo=str(row["target_repo"]),
        target_issue_number=int(row["target_issue_number"]),
        state=state,  # type: ignore[arg-type]  # validated against the literal set
        area=str(row["area"]),
        title=str(row["title"]),
        shipped_pr_url=str(row["shipped_pr_url"]),
        recorded_at=str(row["recorded_at"]),
        reported_observations=int(row["reported_observations"]),
    )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tech_lead_launch_authority (
    run_id TEXT NOT NULL,
    session_name TEXT NOT NULL,
    authority TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (run_id, session_name)
);
CREATE TABLE IF NOT EXISTS tech_lead_proposal_ops (
    issue_number INTEGER PRIMARY KEY,
    op TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tech_lead_patterns (
    signature TEXT PRIMARY KEY,
    issue_number INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    observation_count INTEGER NOT NULL DEFAULT 1,
    fix_class TEXT NOT NULL DEFAULT '',
    area TEXT NOT NULL DEFAULT '',
    diagnosis TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tech_lead_promoted_findings (
    signature TEXT PRIMARY KEY,
    case_file_issue_number INTEGER NOT NULL,
    target_repo TEXT NOT NULL,
    target_issue_number INTEGER NOT NULL,
    state TEXT NOT NULL,
    area TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    shipped_pr_url TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL,
    reported_observations INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tech_lead_shipped_fixes (
    issue_number INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    pr_url TEXT NOT NULL,
    area TEXT NOT NULL,
    merged_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tech_lead_storm_cohorts (
    anchor_issue_number INTEGER PRIMARY KEY,
    cohort TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
"""


class SqliteTechLeadAuthorityStore:
    """Persists per-run tech_lead launch authority across restarts."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._local = threading.local()
        self._write_lock = threading.Lock()
        self.initialize()

    @classmethod
    def for_repo(cls, repo_root: Path) -> "SqliteTechLeadAuthorityStore":
        """Store handle for a repository's orchestrator state directory.

        Called only by the composition root (and adapter tests); control code
        depends on the injected ``TechLeadAuthorityStore`` port instead.
        """
        return cls(state_dir(repo_root) / "tech_lead_authority.sqlite")

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_connection()
        conn.executescript(_SCHEMA)
        self._migrate_pattern_promotion_columns(conn)

    @staticmethod
    def _migrate_pattern_promotion_columns(conn: sqlite3.Connection) -> None:
        """Add the #6957 promotion columns to a pre-existing pattern ledger.

        ``CREATE TABLE IF NOT EXISTS`` leaves an already-created table alone, so
        a store written before the promotion lane shipped keeps the old three
        columns. Promotion eligibility reads them, so backfill in place: the
        defaults preserve the old meaning exactly (one recorded observation,
        unclassified — therefore not promotable until the tech lead classifies
        the signature on a later observation).
        """
        pattern_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(tech_lead_patterns)")
        }
        additions = (
            ("observation_count", "INTEGER NOT NULL DEFAULT 1"),
            ("fix_class", "TEXT NOT NULL DEFAULT ''"),
            ("area", "TEXT NOT NULL DEFAULT ''"),
            ("diagnosis", "TEXT NOT NULL DEFAULT ''"),
        )
        added = False
        for name, ddl in additions:
            if name not in pattern_columns:
                conn.execute(f"ALTER TABLE tech_lead_patterns ADD COLUMN {name} {ddl}")
                added = True
        promotion_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(tech_lead_promoted_findings)")
        }
        if "reported_observations" not in promotion_columns:
            conn.execute(
                "ALTER TABLE tech_lead_promoted_findings ADD COLUMN"
                " reported_observations INTEGER NOT NULL DEFAULT 0"
            )
            added = True
        if added:
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

    def record(
        self, *, run_id: str, session_name: str, authority: TechLeadLaunchAuthority
    ) -> None:
        """Persist the launch authority for one session run (create-once).

        Identical payload for an existing key: no-op. Different payload:
        :class:`TechLeadAuthorityConflictError` — the scope must never
        silently change after launch (#6769 round 4).
        """
        payload = json.dumps(authority.to_dict(), sort_keys=True)
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT authority FROM tech_lead_launch_authority "
                "WHERE run_id = ? AND session_name = ?",
                (run_id, session_name),
            ).fetchone()
            if row is not None:
                existing = TechLeadLaunchAuthority.from_dict(json.loads(row[0]))
                if existing == authority:
                    return
                raise TechLeadAuthorityConflictError(
                    f"launch authority already recorded for run_id={run_id!r} "
                    f"session={session_name!r} with a different payload"
                )
            tx.execute(
                "INSERT INTO tech_lead_launch_authority "
                "(run_id, session_name, authority, recorded_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    run_id,
                    session_name,
                    payload,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        logger.info(
            "[tech_lead] Recorded launch authority: run_id=%s session=%s flavor=%s "
            "focus=%s manifest_prs=%s problem_issues=%s",
            run_id,
            session_name,
            authority.flavor.value,
            authority.focus_issue_number,
            list(authority.manifest_pr_numbers),
            list(authority.problem_issue_numbers),
        )

    def load(self, *, run_id: str, session_name: str) -> TechLeadLaunchAuthority | None:
        """Load the launch authority for a session run, or None when absent.

        Malformed stored content raises ValueError loudly — the store is
        orchestrator-owned, so corruption is a bug, never agent input to
        fail-safe around.
        """
        conn = self._get_connection()
        row = conn.execute(
            "SELECT authority FROM tech_lead_launch_authority "
            "WHERE run_id = ? AND session_name = ?",
            (run_id, session_name),
        ).fetchone()
        if row is None:
            return None
        return TechLeadLaunchAuthority.from_dict(json.loads(row["authority"]))

    def discard(self, *, run_id: str, session_name: str) -> None:
        """Remove a run's authority row (retention owner; no-op if absent).

        Called when the run reaches a terminal state — completion
        finalization, or a launch that failed after recording — so authority
        rows never outlive their session run (#6769 finding 3).
        """
        with self._transaction() as tx:
            deleted = tx.execute(
                "DELETE FROM tech_lead_launch_authority "
                "WHERE run_id = ? AND session_name = ?",
                (run_id, session_name),
            ).rowcount
        if deleted:
            logger.info(
                "[tech_lead] Discarded launch authority: run_id=%s session=%s",
                run_id,
                session_name,
            )

    # -- Gated proposal ops (#6778, ADR-0031 §2 amendment) -----------------

    def record_op(self, *, issue_number: int, op: StoredTechLeadOp) -> None:
        """Persist a proposal issue's executable op (create-once).

        Identical payload for an existing key: no-op. Different payload:
        :class:`TechLeadOpConflictError` — the approver's consent binds to
        exactly one recorded payload, which must never silently change.
        """
        payload = json.dumps(op.to_dict(), sort_keys=True)
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT op FROM tech_lead_proposal_ops WHERE issue_number = ?",
                (issue_number,),
            ).fetchone()
            if row is not None:
                if json.dumps(json.loads(row[0]), sort_keys=True) == payload:
                    return
                raise TechLeadOpConflictError(
                    f"a different tech_lead op is already recorded for proposal"
                    f" issue #{issue_number}"
                )
            tx.execute(
                "INSERT INTO tech_lead_proposal_ops (issue_number, op, recorded_at)"
                " VALUES (?, ?, ?)",
                (issue_number, payload, datetime.now(timezone.utc).isoformat()),
            )
        logger.info(
            "[tech_lead] Recorded proposal op: issue=#%d op=%s target=#%d action=%s",
            issue_number,
            op.op_type,
            op.target_issue_number,
            op.source_action_id,
        )

    def load_op(self, *, issue_number: int) -> StoredTechLeadOp | None:
        """Load a proposal issue's op, or None when absent.

        Malformed stored content raises ValueError loudly — the store is
        orchestrator-owned, so corruption is a bug, never agent input.
        """
        conn = self._get_connection()
        row = conn.execute(
            "SELECT op FROM tech_lead_proposal_ops WHERE issue_number = ?",
            (issue_number,),
        ).fetchone()
        if row is None:
            return None
        return StoredTechLeadOp.from_dict(json.loads(row["op"]))

    def discard_op(self, *, issue_number: int) -> None:
        """Remove a proposal issue's op row (once-only owner; no-op if absent)."""
        with self._transaction() as tx:
            deleted = tx.execute(
                "DELETE FROM tech_lead_proposal_ops WHERE issue_number = ?",
                (issue_number,),
            ).rowcount
        if deleted:
            logger.info("[tech_lead] Discarded proposal op: issue=#%d", issue_number)

    def list_ops(self) -> tuple[tuple[int, StoredTechLeadOp], ...]:
        """All (proposal_issue_number, op) rows — the open-proposal ledger."""
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT issue_number, op FROM tech_lead_proposal_ops ORDER BY issue_number",
        ).fetchall()
        return tuple(
            (
                int(row["issue_number"]),
                StoredTechLeadOp.from_dict(json.loads(row["op"])),
            )
            for row in rows
        )

    # -- Pattern case files (#6781) -----------------------------------------

    def record_pattern(
        self,
        *,
        signature: str,
        issue_number: int,
        fix_class: str = "",
        area: str = "",
        observations: int = 1,
        diagnosis: str = "",
    ) -> None:
        """Persist a signature's case-file issue (create-once).

        Same issue for an existing signature: no-op. Different issue:
        :class:`TechLeadPatternConflictError` — a signature keys exactly one
        evidence trail, which must never silently move. ``fix_class``/``area``
        and the observation count are the promotion facts (#6957).
        """
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT issue_number FROM tech_lead_patterns WHERE signature = ?",
                (signature,),
            ).fetchone()
            if row is not None:
                if int(row[0]) == issue_number:
                    return
                raise TechLeadPatternConflictError(
                    f"pattern signature {signature!r} is already recorded for"
                    f" case-file issue #{int(row[0])}"
                )
            tx.execute(
                "INSERT INTO tech_lead_patterns (signature, issue_number,"
                " recorded_at, observation_count, fix_class, area, diagnosis)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    signature,
                    issue_number,
                    datetime.now(timezone.utc).isoformat(),
                    max(1, observations),
                    fix_class,
                    area,
                    diagnosis,
                ),
            )
        logger.info(
            "[tech_lead] Recorded pattern case file: signature=%r issue=#%d"
            " observations=%d fix_class=%r",
            signature,
            issue_number,
            max(1, observations),
            fix_class,
        )

    def note_pattern_observation(
        self, *, signature: str, fix_class: str = "", area: str = ""
    ) -> None:
        """Increment a signature's durable observation count (#6957).

        A later observation may also CLASSIFY a signature the first one left
        unclassified (and vice versa for the area), so a non-empty value
        upgrades the row; an empty one preserves what is already recorded.
        """
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT observation_count, fix_class, area FROM tech_lead_patterns"
                " WHERE signature = ?",
                (signature,),
            ).fetchone()
            if row is None:
                raise UnknownTechLeadPatternError(
                    f"no pattern case file is recorded for signature {signature!r}"
                )
            tx.execute(
                "UPDATE tech_lead_patterns SET observation_count = ?, fix_class = ?,"
                " area = ? WHERE signature = ?",
                (
                    int(row["observation_count"]) + 1,
                    fix_class or str(row["fix_class"]),
                    area or str(row["area"]),
                    signature,
                ),
            )

    def lookup_pattern(self, *, signature: str) -> int | None:
        """Return the case-file issue for a signature, or None when absent."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT issue_number FROM tech_lead_patterns WHERE signature = ?",
            (signature,),
        ).fetchone()
        return int(row["issue_number"]) if row is not None else None

    def list_patterns(self) -> tuple[tuple[str, int], ...]:
        """All (signature, case_file_issue_number) rows — the pattern ledger."""
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT signature, issue_number FROM tech_lead_patterns ORDER BY signature",
        ).fetchall()
        return tuple((str(row["signature"]), int(row["issue_number"])) for row in rows)

    def list_pattern_evidence(self) -> tuple[PatternEvidence, ...]:
        """All case-file rows with their promotion facts (#6957)."""
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT signature, issue_number, observation_count, fix_class, area,"
            " diagnosis"
            " FROM tech_lead_patterns ORDER BY signature",
        ).fetchall()
        return tuple(
            PatternEvidence(
                signature=str(row["signature"]),
                case_file_issue_number=int(row["issue_number"]),
                observation_count=int(row["observation_count"]),
                fix_class=str(row["fix_class"]),
                area=str(row["area"]),
                diagnosis=str(row["diagnosis"]),
            )
            for row in rows
        )

    # -- Promoted findings (#6957) ------------------------------------------

    def record_promotion(self, *, promotion: PromotedFinding) -> None:
        """Persist a promoted finding (create-once by signature)."""
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT target_repo, target_issue_number FROM"
                " tech_lead_promoted_findings WHERE signature = ?",
                (promotion.signature,),
            ).fetchone()
            if row is not None:
                if (
                    str(row["target_repo"]) == promotion.target_repo
                    and int(row["target_issue_number"]) == promotion.target_issue_number
                ):
                    return
                raise TechLeadPromotionConflictError(
                    f"pattern signature {promotion.signature!r} is already promoted"
                    f" to {row['target_repo']}#{int(row['target_issue_number'])}"
                )
            tx.execute(
                "INSERT INTO tech_lead_promoted_findings (signature,"
                " case_file_issue_number, target_repo, target_issue_number, state,"
                " area, title, shipped_pr_url, recorded_at, reported_observations)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    promotion.signature,
                    promotion.case_file_issue_number,
                    promotion.target_repo,
                    promotion.target_issue_number,
                    promotion.state,
                    promotion.area,
                    promotion.title,
                    promotion.shipped_pr_url,
                    promotion.recorded_at or datetime.now(timezone.utc).isoformat(),
                    promotion.reported_observations,
                ),
            )
        logger.info(
            "[tech_lead] Recorded promoted finding: signature=%r -> %s#%d"
            " (case file #%d)",
            promotion.signature,
            promotion.target_repo,
            promotion.target_issue_number,
            promotion.case_file_issue_number,
        )

    def note_promotion_reported(self, *, signature: str, observations: int) -> None:
        """Advance a promotion's reported-observation high-water mark (#6957).

        Written AFTER the evidence comment lands on the promoted issue, so a
        crash between the two repeats one comment rather than silently dropping
        evidence the promoted issue was never told about. ``max`` keeps the mark
        monotonic against an out-of-order retry.
        """
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT reported_observations FROM tech_lead_promoted_findings"
                " WHERE signature = ?",
                (signature,),
            ).fetchone()
            if row is None:
                raise UnknownTechLeadPatternError(
                    f"no promotion is recorded for signature {signature!r}"
                )
            tx.execute(
                "UPDATE tech_lead_promoted_findings SET reported_observations = ?"
                " WHERE signature = ?",
                (max(int(row["reported_observations"]), observations), signature),
            )

    def load_promotion(self, *, signature: str) -> PromotedFinding | None:
        """Return a signature's promotion row, or None when never promoted."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM tech_lead_promoted_findings WHERE signature = ?",
            (signature,),
        ).fetchone()
        return _promotion_from_row(row) if row is not None else None

    def list_promotions(self) -> tuple[PromotedFinding, ...]:
        """All promotion rows — the dedup, cap, and loop-closure ledger read."""
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT * FROM tech_lead_promoted_findings ORDER BY signature",
        ).fetchall()
        return tuple(_promotion_from_row(row) for row in rows)

    def settle_promotion(
        self,
        *,
        signature: str,
        state: PromotionState,
        shipped_pr_url: str = "",
    ) -> None:
        """Move a promotion to a terminal state (``declined``/``shipped``)."""
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT shipped_pr_url FROM tech_lead_promoted_findings"
                " WHERE signature = ?",
                (signature,),
            ).fetchone()
            if row is None:
                raise UnknownTechLeadPatternError(
                    f"no promotion is recorded for signature {signature!r}"
                )
            tx.execute(
                "UPDATE tech_lead_promoted_findings SET state = ?, shipped_pr_url = ?"
                " WHERE signature = ?",
                (state, shipped_pr_url or str(row["shipped_pr_url"]), signature),
            )
        logger.info(
            "[tech_lead] Promotion for signature=%r settled as %s%s",
            signature,
            state,
            f" ({shipped_pr_url})" if shipped_pr_url else "",
        )

    # -- Problem-storm cohorts (#6780) ------------------------------------

    def record_storm_cohort(
        self, *, anchor_issue_number: int, cohort: tuple[DiscoveredFailure, ...]
    ) -> None:
        """Persist an anchor's problem cohort (create-once).

        Identical cohort for an existing anchor: no-op. Different cohort:
        :class:`TechLeadStormCohortConflictError` — the cohort is the health
        review's act-level authority and the retention scope for the members'
        run artifacts, so it must never silently change after creation.
        """
        payload = json.dumps([problem.to_dict() for problem in cohort], sort_keys=True)
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT cohort FROM tech_lead_storm_cohorts"
                " WHERE anchor_issue_number = ?",
                (anchor_issue_number,),
            ).fetchone()
            if row is not None:
                if json.dumps(json.loads(row[0]), sort_keys=True) == payload:
                    return
                raise TechLeadStormCohortConflictError(
                    f"a different storm cohort is already recorded for anchor"
                    f" issue #{anchor_issue_number}"
                )
            tx.execute(
                "INSERT INTO tech_lead_storm_cohorts (anchor_issue_number, cohort,"
                " recorded_at) VALUES (?, ?, ?)",
                (
                    anchor_issue_number,
                    payload,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        logger.info(
            "[tech_lead] Recorded storm cohort for anchor #%d: %d problem issue(s)",
            anchor_issue_number,
            len(cohort),
        )

    def load_storm_cohort(
        self, *, anchor_issue_number: int
    ) -> tuple[DiscoveredFailure, ...] | None:
        """Return an anchor's persisted cohort, or None when absent."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT cohort FROM tech_lead_storm_cohorts WHERE anchor_issue_number = ?",
            (anchor_issue_number,),
        ).fetchone()
        if row is None:
            return None
        return _cohort_from_payload(row["cohort"])

    def discard_storm_cohort(self, *, anchor_issue_number: int) -> None:
        """Remove an anchor's cohort row. No-op if absent (retention owner)."""
        with self._transaction() as tx:
            tx.execute(
                "DELETE FROM tech_lead_storm_cohorts WHERE anchor_issue_number = ?",
                (anchor_issue_number,),
            )

    def list_storm_cohorts(
        self,
    ) -> tuple[tuple[int, tuple[DiscoveredFailure, ...]], ...]:
        """All (anchor_issue_number, cohort) rows — the cleanup-hold read."""
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT anchor_issue_number, cohort FROM tech_lead_storm_cohorts"
            " ORDER BY anchor_issue_number",
        ).fetchall()
        return tuple(
            (int(row["anchor_issue_number"]), _cohort_from_payload(row["cohort"]))
            for row in rows
        )

    # -- Shipped-fix operational memory (#6781 amendment) -----------------

    def record_shipped_fix(
        self, *, issue_number: int, title: str, pr_url: str, area: str
    ) -> None:
        """Persist an area-tagged merged fix (create-once by issue)."""
        with self._transaction() as tx:
            row = tx.execute(
                "SELECT pr_url, area FROM tech_lead_shipped_fixes "
                "WHERE issue_number = ?",
                (issue_number,),
            ).fetchone()
            if row is not None:
                if str(row["pr_url"]) == pr_url and str(row["area"]) == area:
                    return
                raise TechLeadShippedFixConflictError(
                    f"different shipped-fix evidence is already recorded for"
                    f" issue #{issue_number}"
                )
            tx.execute(
                "INSERT INTO tech_lead_shipped_fixes "
                "(issue_number, title, pr_url, area, merged_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    issue_number,
                    title,
                    pr_url,
                    area,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        logger.info(
            "[tech_lead] Recorded shipped fix: issue=#%d area=%r pr=%s",
            issue_number,
            area,
            pr_url,
        )

    def list_recent_shipped_fixes(
        self, *, limit: int
    ) -> tuple[TechLeadShippedFixSummary, ...]:
        """Return the newest durable shipped-fix facts."""
        if limit <= 0:
            raise ValueError("shipped-fix limit must be positive")
        rows = (
            self._get_connection()
            .execute(
                "SELECT issue_number, title, pr_url, area, merged_at "
                "FROM tech_lead_shipped_fixes "
                "ORDER BY merged_at DESC, issue_number DESC LIMIT ?",
                (limit,),
            )
            .fetchall()
        )
        return tuple(
            TechLeadShippedFixSummary(
                issue_number=int(row["issue_number"]),
                title=str(row["title"]),
                pr_url=str(row["pr_url"]),
                area=str(row["area"]),
                merged_at=str(row["merged_at"]),
            )
            for row in rows
        )
