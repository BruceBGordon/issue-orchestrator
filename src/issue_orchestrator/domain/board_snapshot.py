"""Board snapshot - typed orchestrator-state facts for tech_lead/tech-lead sessions.

A ``BoardSnapshot`` is a serializable bundle of orchestrator-state facts
(active sessions, pending queues, blocked issues, recent failures, timeline
extracts, log tail) written to a file that a tech_lead or tech-lead agent
session reads to understand the state of the board.

Flow:
1. Orchestrator: ``BoardSnapshotBuilder.build()`` -> ``BoardSnapshot``
2. Orchestrator: ``snapshot.write(path)`` into the session worktree
3. Agent: reads the snapshot file, reasons about the board, reports via
   ``coding-done``

The snapshot is a point-in-time fact bundle, not live state: it carries a
``generated_at`` timestamp and a ``schema_version`` so readers can reject
payloads they do not understand. ``from_dict`` fails fast on an unexpected
schema version or missing required keys - a malformed snapshot must never
be silently reinterpreted.
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

BOARD_SNAPSHOT_SCHEMA_VERSION = 6

# --- Hung-session evidence projection ---------------------------------------
# The health review must judge a session HUNG from EVIDENCE (idle with no
# progress), NOT from age alone: a long-running session still emitting output
# or landing commits is working, not hung. These two best-effort signals ride
# on each active session so the review can tell the two apart before proposing
# ``kill_hung_session`` under the repository's configured authority.

# Sentinel for an unknown idle reading (the terminal recording's mtime could
# not be read, or the activity-probe returned nothing). Kept an int — the field
# is a plain int — and read as "no evidence", never as "idle for -1 minutes".
IDLE_MINUTES_UNKNOWN = -1

# Sentinel for an unknown commits-ahead reading (the session worktree was gone
# or the working copy could not be read). Distinct from a real ``0`` (a genuine
# "no commits yet" — itself a hang signal when paired with a high idle).
COMMITS_AHEAD_UNKNOWN = -1

# --- E2E health projection tuning -------------------------------------------
# The health review's board snapshot carries an AGGREGATE view of E2E suite
# health (ADR-0031). E2E is easy to neglect — it runs on a slow ungoverned
# cadence and rots unwatched — so these facts make cadence, red streaks, and
# chronic failures first-class on the review's primary input.

# How many of the most-recent runs the snapshot carries as "chronically red"
# evidence, and the window over which ``nonpassing_streak`` is measured (the
# streak is therefore bounded by this window: a value equal to it means "at
# least this many", look at ``recent_runs``).
RECENT_E2E_RUN_WINDOW = 8

# Cap on chronic-failure rows surfaced (top-N by fail_count). Truncation past
# this is logged, never silent.
MAX_CHRONIC_E2E_FAILURES = 10

# A last run is "stale" (off-cadence) once its age exceeds this multiple of the
# configured auto-run interval. One missed cadence window is normal (the runner
# is busy, the orchestrator restarted); 3x the interval with no run means the
# cadence has clearly slipped and is a FINDING, not noise.
E2E_STALE_INTERVAL_MULTIPLIER = 3

# Sentinel age for a run whose ``started_at`` could not be parsed. Kept as an
# int (the field is a plain int) and treated as stale — freshness cannot be
# confirmed, so we flag rather than assume healthy.
_E2E_AGE_UNKNOWN = -1

# Run statuses. Only ``passed`` is a pass; ``running``/``canceled``/
# ``interrupted`` are in-flight or neutral verdicts that neither count toward a
# non-passing streak nor break it (they are skipped).
_E2E_PASSED_STATUSES = frozenset({"passed"})
_E2E_INFLIGHT_STATUSES = frozenset({"running", "canceled", "interrupted"})

# Canonical snapshot filename inside a session's tech-lead-data directory,
# next to TECH_LEAD_ASSIGNMENT_FILENAME (domain/tech_lead_session.py).
BOARD_SNAPSHOT_FILENAME = "board-snapshot.json"


class BoardSessionInfoDict(TypedDict):
    """Serialized form of BoardSessionInfo."""

    issue_number: int
    issue_title: str
    agent_type: str
    session_type: str
    status: str
    started_at: str
    age_minutes: int
    terminal_id: str
    run_id: str
    idle_minutes: int
    commits_ahead: int
    last_activity_at: str | None


class BoardQueueEntryDict(TypedDict):
    """Serialized form of BoardQueueEntry."""

    queue: str
    issue_number: int
    detail: str


class BoardBlockedByDict(TypedDict):
    """Serialized form of one blocking dependency."""

    number: int
    title: str
    state: str


class BoardBlockedIssueDict(TypedDict):
    """Serialized form of BoardBlockedIssue."""

    issue_number: int
    issue_title: str
    summary: str
    blocked_by: list[BoardBlockedByDict]


class BoardFailureDict(TypedDict):
    """Serialized form of BoardFailure."""

    issue_number: int
    issue_title: str
    failure_reason: str
    artifact_hints: list[str]


class BoardTimelineExtractDict(TypedDict):
    """Serialized form of BoardTimelineExtract."""

    issue_number: int
    records: list[dict[str, Any]]


class BoardCaseFileDict(TypedDict):
    issue_number: int
    title: str
    comment_count: int
    updated_at: str
    area: str


class BoardAreaSignalDict(TypedDict):
    area: str
    distinct_patterns: int
    shipped_fixes: int


class BoardShippedFixDict(TypedDict):
    issue_number: int
    title: str
    pr_url: str
    area: str
    merged_at: str


class BoardE2ERunDict(TypedDict):
    """Serialized form of BoardE2ERun."""

    id: int
    status: str
    started_at: str
    age_minutes: int
    duration_seconds: float | None
    failed_count: int
    passed_count: int


class BoardE2EChronicFailureDict(TypedDict):
    """Serialized form of BoardE2EChronicFailure."""

    nodeid: str
    fail_count: int
    tracking_issue: int | None
    tracking_resolved: bool


class BoardE2EHealthDict(TypedDict):
    """Serialized form of BoardE2EHealth."""

    enabled: bool
    expected_interval_minutes: int
    stale: bool
    nonpassing_streak: int
    quarantine_count: int
    last_run: BoardE2ERunDict | None
    recent_runs: list[BoardE2ERunDict]
    chronic_failures: list[BoardE2EChronicFailureDict]


class BoardSnapshotDict(TypedDict):
    """Serialized form of BoardSnapshot."""

    schema_version: int
    generated_at: str
    orchestrator_paused: bool
    sessions: list[BoardSessionInfoDict]
    queues: list[BoardQueueEntryDict]
    blocked_issues: list[BoardBlockedIssueDict]
    recent_failures: list[BoardFailureDict]
    problem_cohort: list[int]
    case_files: list[BoardCaseFileDict]
    area_signals: list[BoardAreaSignalDict]
    recent_shipped_fixes: list[BoardShippedFixDict]
    timeline: list[BoardTimelineExtractDict]
    log_tail: list[str]
    e2e_health: BoardE2EHealthDict | None


@dataclass(frozen=True)
class SessionActivityFacts:
    """Best-effort hung-EVIDENCE probe result for one active session.

    A pure input to :meth:`BoardSessionInfo` projection, gathered by an injected
    reader that reaches the filesystem/git (the builder itself does not). Two
    cheap signals distinguish "long-running but working" from "genuinely hung",
    NEVER age alone:

    - ``last_activity_at``: ISO wall-clock of the session's last observable
      activity — the mtime of its terminal recording, which the agent's output
      stream writes. ``None`` when that mtime could not be read. The builder
      projects this into ``idle_minutes`` against its injected clock (mirroring
      how ``E2ERunHealthFact.started_at`` becomes ``age_minutes``), so a high
      idle with no commit progress is a hang signal.
    - ``commits_ahead``: commits on the session's branch ahead of base. A real
      ``0`` after a long idle is strong hang evidence; recent commits mean it is
      working. ``COMMITS_AHEAD_UNKNOWN`` (-1) when the working copy could not be
      read (e.g. the worktree is gone).

    A reader that can read neither signal may return ``None`` instead of this
    envelope; the builder maps that to the unknown sentinels on every field. A
    partial read fills the readable field and leaves the other at its sentinel.
    """

    commits_ahead: int
    last_activity_at: str | None = None


@dataclass
class BoardSessionInfo:
    """An active agent session, as seen on the board.

    ``agent_type`` is the issue's ``agent:*`` label; empty string when the
    issue carries no agent label (a legitimate state, not an error).
    ``age_minutes`` is computed by the builder from an injected clock so
    snapshots are deterministic and testable.

    ``idle_minutes``/``commits_ahead``/``last_activity_at`` are best-effort
    hung-EVIDENCE fields (see :class:`SessionActivityFacts`): they let the
    health review judge a session HUNG from evidence — idle with no progress —
    rather than from age alone. Each degrades to its "unknown" sentinel
    (``IDLE_MINUTES_UNKNOWN`` / ``COMMITS_AHEAD_UNKNOWN`` / ``None``) when the
    probe could not read it; the snapshot never fails on a missing reading.
    """

    issue_number: int
    issue_title: str
    agent_type: str
    session_type: str
    status: str
    started_at: str  # ISO timestamp
    age_minutes: int
    terminal_id: str
    # Per-launch generation identity. Unlike the reusable terminal id, this
    # changes every time the same session slot is relaunched.
    run_id: str = ""
    idle_minutes: int = IDLE_MINUTES_UNKNOWN
    commits_ahead: int = COMMITS_AHEAD_UNKNOWN
    last_activity_at: str | None = None


@dataclass
class BoardQueueEntry:
    """One entry in a pending queue.

    ``queue`` names the source queue on OrchestratorState (e.g.
    "pending_reviews", "pending_reworks", "pending_tech_lead",
    "pending_validation_retries", "priority_queue"). ``detail`` is a short
    human-readable elaboration; may be "" (e.g. priority_queue entries).
    """

    queue: str
    issue_number: int
    detail: str


@dataclass
class BoardBlockedIssue:
    """An issue blocked on unresolved dependencies.

    ``blocked_by`` lists (dependency number, title, state) tuples, mirroring
    ``DependencyProblem.blocked_by``.
    """

    issue_number: int
    issue_title: str
    summary: str
    blocked_by: list[tuple[int, str, str]]


@dataclass
class BoardFailure:
    """A recently failed session, pending tech_lead attention.

    ``artifact_hints`` lists paths to session artifacts worth inspecting;
    may be empty when the failure source carries no artifact information.
    """

    issue_number: int
    issue_title: str
    failure_reason: str
    artifact_hints: list[str]


@dataclass
class BoardTimelineExtract:
    """Recent timeline records for one issue.

    Each record is a plain dict mirroring the fields of
    ``ports.timeline_store.TimelineRecord`` that matter to a board reader:
    ``event_id``, ``timestamp``, ``event``, ``data``.
    """

    issue_number: int
    records: list[dict[str, Any]]


@dataclass
class BoardCaseFile:
    """An open signature-keyed pattern evidence ledger (#6781)."""

    issue_number: int
    title: str
    comment_count: int
    updated_at: str
    area: str


@dataclass
class BoardAreaSignal:
    """Step-back evidence for one component/seam (#6781 amendment)."""

    area: str
    distinct_patterns: int
    shipped_fixes: int


@dataclass
class BoardShippedFix:
    """Restart-safe patch evidence for an area-tagged merged issue."""

    issue_number: int
    title: str
    pr_url: str
    area: str
    merged_at: str


@dataclass(frozen=True)
class E2ERunHealthFact:
    """Raw per-run facts read from the E2E results db, fed to the projection.

    Newest-first ordering is the reader's responsibility. ``passed_count`` /
    ``failed_count`` are per-run outcome tallies (failed counts both ``failed``
    and ``error`` outcomes). A pure input to :meth:`BoardE2EHealth.project` —
    it carries no computed fields (age/streak need ``now``, computed there).
    """

    id: int
    status: str
    started_at: str
    duration_seconds: float | None
    passed_count: int
    failed_count: int


@dataclass(frozen=True)
class E2EChronicFailureFact:
    """Raw recurring-failure facts (one per nodeid) fed to the projection.

    ``tracking_issue`` is the GitHub issue filed for this chronic failure (from
    ``e2e_failure_issues``), or ``None`` when the failure is untracked.
    ``tracking_resolved`` mirrors whether that issue was marked resolved.
    """

    nodeid: str
    fail_count: int
    tracking_issue: int | None
    tracking_resolved: bool


@dataclass(frozen=True)
class BoardE2ERun:
    """One E2E run projected onto the board (age computed against the clock)."""

    id: int
    status: str
    started_at: str
    age_minutes: int
    duration_seconds: float | None
    failed_count: int
    passed_count: int

    def to_dict(self) -> BoardE2ERunDict:
        return {
            "id": self.id,
            "status": self.status,
            "started_at": self.started_at,
            "age_minutes": self.age_minutes,
            "duration_seconds": self.duration_seconds,
            "failed_count": self.failed_count,
            "passed_count": self.passed_count,
        }

    @classmethod
    def from_dict(cls, data: BoardE2ERunDict) -> "BoardE2ERun":
        return cls(
            id=data["id"],
            status=data["status"],
            started_at=data["started_at"],
            age_minutes=data["age_minutes"],
            duration_seconds=data["duration_seconds"],
            failed_count=data["failed_count"],
            passed_count=data["passed_count"],
        )


@dataclass(frozen=True)
class BoardE2EChronicFailure:
    """A recurring failing test, with its tracking-issue status if any."""

    nodeid: str
    fail_count: int
    tracking_issue: int | None
    tracking_resolved: bool

    def to_dict(self) -> BoardE2EChronicFailureDict:
        return {
            "nodeid": self.nodeid,
            "fail_count": self.fail_count,
            "tracking_issue": self.tracking_issue,
            "tracking_resolved": self.tracking_resolved,
        }

    @classmethod
    def from_dict(cls, data: BoardE2EChronicFailureDict) -> "BoardE2EChronicFailure":
        return cls(
            nodeid=data["nodeid"],
            fail_count=data["fail_count"],
            tracking_issue=data["tracking_issue"],
            tracking_resolved=data["tracking_resolved"],
        )


@dataclass(frozen=True)
class BoardE2EHealth:
    """Aggregate E2E suite health, projected onto the board snapshot.

    A pure projection of data the E2E results db already holds (ADR-0031): it
    makes E2E a first-class, neglect-proof signal for the health review. It
    decides nothing and touches no GitHub/network — the reader closure gathers
    the raw facts and :meth:`project` classifies them against an injected
    ``now`` (never ``datetime.now()`` here).

    ``stale`` (off-cadence) and ``nonpassing_streak`` answer "is E2E running,
    and is it green?"; ``chronic_failures`` answers "are the recurring failures
    tracked and being worked, or neglected?".
    """

    enabled: bool
    expected_interval_minutes: int
    stale: bool
    nonpassing_streak: int
    quarantine_count: int
    last_run: BoardE2ERun | None = None
    recent_runs: tuple[BoardE2ERun, ...] = ()
    chronic_failures: tuple[BoardE2EChronicFailure, ...] = ()

    @classmethod
    def project(
        cls,
        *,
        now: datetime,
        enabled: bool,
        expected_interval_minutes: int,
        runs: Sequence[E2ERunHealthFact],
        chronic_failures: Sequence[E2EChronicFailureFact],
        quarantine_count: int,
        recent_run_window: int = RECENT_E2E_RUN_WINDOW,
        max_chronic_failures: int = MAX_CHRONIC_E2E_FAILURES,
        stale_multiplier: int = E2E_STALE_INTERVAL_MULTIPLIER,
    ) -> "BoardE2EHealth":
        """Project raw e2e-health facts into the board signal (pure).

        ``runs`` MUST be newest-first. ``now`` is threaded from the builder's
        clock. ``started_at`` is parsed defensively — an unparseable timestamp
        yields ``age_minutes == -1`` and is treated as stale.
        """
        board_runs = tuple(
            _project_e2e_run(fact, now) for fact in list(runs)[:recent_run_window]
        )
        last_run = board_runs[0] if board_runs else None
        return cls(
            enabled=enabled,
            expected_interval_minutes=expected_interval_minutes,
            stale=_compute_e2e_stale(
                enabled, expected_interval_minutes, last_run, stale_multiplier
            ),
            nonpassing_streak=_e2e_nonpassing_streak(board_runs),
            quarantine_count=quarantine_count,
            last_run=last_run,
            recent_runs=board_runs,
            chronic_failures=_project_e2e_chronic(
                chronic_failures, max_chronic_failures
            ),
        )

    def to_dict(self) -> BoardE2EHealthDict:
        return {
            "enabled": self.enabled,
            "expected_interval_minutes": self.expected_interval_minutes,
            "stale": self.stale,
            "nonpassing_streak": self.nonpassing_streak,
            "quarantine_count": self.quarantine_count,
            "last_run": self.last_run.to_dict() if self.last_run is not None else None,
            "recent_runs": [run.to_dict() for run in self.recent_runs],
            "chronic_failures": [item.to_dict() for item in self.chronic_failures],
        }

    @classmethod
    def from_dict(cls, data: BoardE2EHealthDict) -> "BoardE2EHealth":
        last_run = data["last_run"]
        return cls(
            enabled=data["enabled"],
            expected_interval_minutes=data["expected_interval_minutes"],
            stale=data["stale"],
            nonpassing_streak=data["nonpassing_streak"],
            quarantine_count=data["quarantine_count"],
            last_run=BoardE2ERun.from_dict(last_run) if last_run is not None else None,
            recent_runs=tuple(
                BoardE2ERun.from_dict(run) for run in data["recent_runs"]
            ),
            chronic_failures=tuple(
                BoardE2EChronicFailure.from_dict(item)
                for item in data["chronic_failures"]
            ),
        )


def _parse_iso_datetime(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp defensively (``None`` on anything invalid)."""
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def project_idle_minutes(last_activity_at: str | None, now: datetime) -> int:
    """Whole minutes since a session's last observable activity.

    ``IDLE_MINUTES_UNKNOWN`` (-1) when the activity timestamp is absent (the
    recording mtime could not be read) or unparseable. Mirrors ``age_minutes``:
    both are minutes-since-a-timestamp against the builder's injected clock, so
    the value is deterministic under test (never ``datetime.now()`` here). Uses
    POSIX timestamps so a naive-local ``now`` and a naive-local recording mtime
    resolve to the same absolute epoch; clamped at 0 (a future mtime from clock
    skew reads as "just now", never negative).
    """
    if last_activity_at is None:
        return IDLE_MINUTES_UNKNOWN
    parsed = _parse_iso_datetime(last_activity_at)
    if parsed is None:
        return IDLE_MINUTES_UNKNOWN
    return max(0, int((now.timestamp() - parsed.timestamp()) / 60))


def _e2e_age_minutes(started_at: str, now: datetime) -> int:
    """Whole minutes since ``started_at``; ``-1`` when the timestamp is unparseable.

    Uses POSIX timestamps so the delta is robust to naive/aware mismatch: the
    db stores UTC-aware ISO strings while the builder's clock is naive-local,
    and ``.timestamp()`` resolves both to absolute epoch seconds.
    """
    parsed = _parse_iso_datetime(started_at)
    if parsed is None:
        return _E2E_AGE_UNKNOWN
    return int((now.timestamp() - parsed.timestamp()) / 60)


def _project_e2e_run(fact: E2ERunHealthFact, now: datetime) -> BoardE2ERun:
    return BoardE2ERun(
        id=fact.id,
        status=fact.status,
        started_at=fact.started_at,
        age_minutes=_e2e_age_minutes(fact.started_at, now),
        duration_seconds=fact.duration_seconds,
        failed_count=fact.failed_count,
        passed_count=fact.passed_count,
    )


def _compute_e2e_stale(
    enabled: bool,
    expected_interval_minutes: int,
    last_run: BoardE2ERun | None,
    stale_multiplier: int,
) -> bool:
    """Whether the suite is off-cadence.

    Disabled E2E is never "stale" (report ``enabled=False`` instead). Enabled
    but never-run is stale. With a positive cadence, stale once the last run is
    older than ``stale_multiplier`` intervals; an unparseable last-run
    timestamp is treated as stale (freshness cannot be confirmed).
    """
    if not enabled:
        return False
    if last_run is None:
        return True
    if expected_interval_minutes <= 0:
        return False
    if last_run.age_minutes < 0:
        return True
    return last_run.age_minutes > stale_multiplier * expected_interval_minutes


def _e2e_nonpassing_streak(runs: Sequence[BoardE2ERun]) -> int:
    """Consecutive most-recent runs that did not pass.

    In-flight/neutral verdicts (running/canceled/interrupted) are skipped
    without breaking the streak; a ``passed`` run stops it. Bounded by the
    number of runs supplied (the recent-run window).
    """
    streak = 0
    for run in runs:
        status = (run.status or "").strip().lower()
        if status in _E2E_INFLIGHT_STATUSES:
            continue
        if status in _E2E_PASSED_STATUSES:
            break
        streak += 1
    return streak


def _project_e2e_chronic(
    facts: Sequence[E2EChronicFailureFact], max_chronic_failures: int
) -> tuple[BoardE2EChronicFailure, ...]:
    """Top-N chronic failures by fail_count; truncation is logged, not silent."""
    ordered = sorted(facts, key=lambda fact: fact.fail_count, reverse=True)
    if len(ordered) > max_chronic_failures:
        logger.warning(
            "[board] e2e chronic-failure list truncated to %d of %d (top by fail_count)",
            max_chronic_failures,
            len(ordered),
        )
    return tuple(
        BoardE2EChronicFailure(
            nodeid=fact.nodeid,
            fail_count=fact.fail_count,
            tracking_issue=fact.tracking_issue,
            tracking_resolved=fact.tracking_resolved,
        )
        for fact in ordered[:max_chronic_failures]
    )


@dataclass
class BoardSnapshot:
    """Point-in-time bundle of orchestrator-state facts for an agent session.

    Created by ``control.board_snapshot_builder.BoardSnapshotBuilder``,
    written to a file, read by a tech_lead/tech-lead agent. All list fields are
    bounded by the builder (the file is consumed by an agent with a finite
    context window); see the builder's docstrings for the exact caps.
    """

    generated_at: str  # ISO timestamp
    orchestrator_paused: bool
    schema_version: int = BOARD_SNAPSHOT_SCHEMA_VERSION
    sessions: list[BoardSessionInfo] = field(default_factory=list)
    queues: list[BoardQueueEntry] = field(default_factory=list)
    blocked_issues: list[BoardBlockedIssue] = field(default_factory=list)
    recent_failures: list[BoardFailure] = field(default_factory=list)
    problem_cohort: list[int] = field(default_factory=list)
    case_files: list[BoardCaseFile] = field(default_factory=list)
    area_signals: list[BoardAreaSignal] = field(default_factory=list)
    recent_shipped_fixes: list[BoardShippedFix] = field(default_factory=list)
    timeline: list[BoardTimelineExtract] = field(default_factory=list)
    log_tail: list[str] = field(default_factory=list)
    # Aggregate E2E suite health (ADR-0031). ``None`` when the repo has no E2E
    # results db or the best-effort projection could not be built — an
    # ENHANCEMENT, never a required fact, so its absence never fails a snapshot.
    e2e_health: BoardE2EHealth | None = None

    def problem_issue_numbers(self) -> frozenset[int]:
        """The health review's OWNED problem cohort — its act-level remit.

        This is the dedicated cohort surface (#6780), deliberately
        NOT derived from ``recent_failures``. Those are board CONTEXT: the
        provider merges the live failure buffer plus every pending failure
        investigation and every pending health-review cohort, so a review
        reading that list sees issues it does not own. Inferring authority
        from it let a storm review act on an unrelated pending investigation,
        and handed a periodic review act-level scope whenever anything on the
        board happened to be failing.

        Empty for a periodic health review (its remit is to walk the board and
        PROPOSE, not to act on a cohort) and for every non-health flavor,
        which scope by focus issue or PR manifest instead.
        """
        return frozenset(self.problem_cohort)

    def to_dict(self) -> BoardSnapshotDict:
        """Convert to JSON-serializable dict."""
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "orchestrator_paused": self.orchestrator_paused,
            "sessions": [
                {
                    "issue_number": s.issue_number,
                    "issue_title": s.issue_title,
                    "agent_type": s.agent_type,
                    "session_type": s.session_type,
                    "status": s.status,
                    "started_at": s.started_at,
                    "age_minutes": s.age_minutes,
                    "terminal_id": s.terminal_id,
                    "run_id": s.run_id,
                    "idle_minutes": s.idle_minutes,
                    "commits_ahead": s.commits_ahead,
                    "last_activity_at": s.last_activity_at,
                }
                for s in self.sessions
            ],
            "queues": [
                {
                    "queue": q.queue,
                    "issue_number": q.issue_number,
                    "detail": q.detail,
                }
                for q in self.queues
            ],
            "blocked_issues": [
                {
                    "issue_number": b.issue_number,
                    "issue_title": b.issue_title,
                    "summary": b.summary,
                    "blocked_by": [
                        {"number": number, "title": title, "state": state}
                        for number, title, state in b.blocked_by
                    ],
                }
                for b in self.blocked_issues
            ],
            "recent_failures": [
                {
                    "issue_number": f.issue_number,
                    "issue_title": f.issue_title,
                    "failure_reason": f.failure_reason,
                    "artifact_hints": list(f.artifact_hints),
                }
                for f in self.recent_failures
            ],
            "problem_cohort": list(self.problem_cohort),
            "case_files": [
                {
                    "issue_number": item.issue_number,
                    "title": item.title,
                    "comment_count": item.comment_count,
                    "updated_at": item.updated_at,
                    "area": item.area,
                }
                for item in self.case_files
            ],
            "area_signals": [
                {
                    "area": item.area,
                    "distinct_patterns": item.distinct_patterns,
                    "shipped_fixes": item.shipped_fixes,
                }
                for item in self.area_signals
            ],
            "recent_shipped_fixes": [
                {
                    "issue_number": item.issue_number,
                    "title": item.title,
                    "pr_url": item.pr_url,
                    "area": item.area,
                    "merged_at": item.merged_at,
                }
                for item in self.recent_shipped_fixes
            ],
            "timeline": [
                {
                    "issue_number": t.issue_number,
                    "records": [dict(record) for record in t.records],
                }
                for t in self.timeline
            ],
            "log_tail": list(self.log_tail),
            "e2e_health": (
                self.e2e_health.to_dict() if self.e2e_health is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, data: BoardSnapshotDict) -> "BoardSnapshot":
        """Load from dict. Fails fast on schema drift or missing keys.

        Raises:
            ValueError: if ``schema_version`` is not the supported version.
            KeyError: if any required key is missing.
        """
        schema_version = data["schema_version"]
        if schema_version != BOARD_SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported board snapshot schema_version {schema_version!r}; "
                f"this reader supports schema_version {BOARD_SNAPSHOT_SCHEMA_VERSION}"
            )
        return cls(
            schema_version=schema_version,
            generated_at=data["generated_at"],
            orchestrator_paused=data["orchestrator_paused"],
            sessions=[
                BoardSessionInfo(
                    issue_number=s["issue_number"],
                    issue_title=s["issue_title"],
                    agent_type=s["agent_type"],
                    session_type=s["session_type"],
                    status=s["status"],
                    started_at=s["started_at"],
                    age_minutes=s["age_minutes"],
                    terminal_id=s["terminal_id"],
                    run_id=s["run_id"],
                    idle_minutes=s["idle_minutes"],
                    commits_ahead=s["commits_ahead"],
                    last_activity_at=s["last_activity_at"],
                )
                for s in data["sessions"]
            ],
            queues=[
                BoardQueueEntry(
                    queue=q["queue"],
                    issue_number=q["issue_number"],
                    detail=q["detail"],
                )
                for q in data["queues"]
            ],
            blocked_issues=[
                BoardBlockedIssue(
                    issue_number=b["issue_number"],
                    issue_title=b["issue_title"],
                    summary=b["summary"],
                    blocked_by=[
                        (dep["number"], dep["title"], dep["state"])
                        for dep in b["blocked_by"]
                    ],
                )
                for b in data["blocked_issues"]
            ],
            recent_failures=[
                BoardFailure(
                    issue_number=f["issue_number"],
                    issue_title=f["issue_title"],
                    failure_reason=f["failure_reason"],
                    artifact_hints=list(f["artifact_hints"]),
                )
                for f in data["recent_failures"]
            ],
            problem_cohort=list(data["problem_cohort"]),
            case_files=[
                BoardCaseFile(
                    issue_number=item["issue_number"],
                    title=item["title"],
                    comment_count=item["comment_count"],
                    updated_at=item["updated_at"],
                    area=item["area"],
                )
                for item in data["case_files"]
            ],
            area_signals=[
                BoardAreaSignal(
                    area=item["area"],
                    distinct_patterns=item["distinct_patterns"],
                    shipped_fixes=item["shipped_fixes"],
                )
                for item in data["area_signals"]
            ],
            recent_shipped_fixes=[
                BoardShippedFix(
                    issue_number=item["issue_number"],
                    title=item["title"],
                    pr_url=item["pr_url"],
                    area=item["area"],
                    merged_at=item["merged_at"],
                )
                for item in data["recent_shipped_fixes"]
            ],
            timeline=[
                BoardTimelineExtract(
                    issue_number=t["issue_number"],
                    records=[dict(record) for record in t["records"]],
                )
                for t in data["timeline"]
            ],
            log_tail=list(data["log_tail"]),
            e2e_health=(
                BoardE2EHealth.from_dict(data["e2e_health"])
                if data["e2e_health"] is not None
                else None
            ),
        )

    def write(self, path: Path) -> None:
        """Write the snapshot to ``path`` as JSON, creating parent dirs."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        logger.info(
            "[board] Snapshot written: %s (%d sessions, %d queue entries)",
            path,
            len(self.sessions),
            len(self.queues),
        )

    @classmethod
    def read(cls, path: Path) -> "BoardSnapshot":
        """Read a snapshot from ``path``. Fails fast on unreadable payloads."""
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)
