"""ADR-0031 tech_lead composition helpers for the bootstrap root.

Owns construction of the tech_lead launch-authority adapter and the
board-snapshot builder so the composition root stays within its line
budget while both stay composition-root-only concerns (control code must
never construct these — the authority store is a trust boundary and the
snapshot builder must reuse the root's OWNED timeline store; a second
store path would re-run schema init and open a write-capable connection
per read).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from ..infra.logging_config import get_repo_log_path, read_log_tail

if TYPE_CHECKING:
    from ..control.board_snapshot_builder import BoardSnapshotBuilder
    from ..control.fact_gatherer import FactGatherer
    from ..control.open_issue_corpus import OpenIssueCorpusManager
    from ..control.provider_resilience import ProviderResilienceManager
    from ..control.retry_history_state import ExpediteEligibility, ExpediteLane
    from ..ports import Issue
    from ..control.tech_lead_board import TechLeadBoardPublisher
    from ..domain.board_snapshot import BoardE2EHealth, SessionActivityFacts
    from ..domain.models import Session
    from ..infra.config import Config
    from ..infra.orchestrator import Orchestrator
    from ..ports import EventSink, RepositoryHost
    from ..ports.promotion_target import PromotionTargetHost
    from ..ports.queue_cache_store import QueueCacheStore
    from ..ports.timeline_store import TimelineStore
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from ..ports.open_issue_corpus_store import OpenIssueCorpusStore
    from ..ports.working_copy import WorkingCopy

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TechLeadComposition:
    """Dependencies that must share one authority and projection owner."""

    authority: "TechLeadAuthorityStore"
    open_issue_corpus: "OpenIssueCorpusManager"
    board_publisher: "TechLeadBoardPublisher | None"
    fact_gatherer: "FactGatherer | None"
    # Cross-repo filing seam for the finding-promotion lane (#6957). None when
    # the repository host is not a real GitHub adapter (offline/testing), which
    # leaves promotion actions failing loudly rather than silently no-oping.
    promotion_target: "PromotionTargetHost | None" = None


def create_tech_lead_authority_store(config: "Config") -> "TechLeadAuthorityStore":
    """The orchestrator-owned trusted tech_lead scope and ledger adapter.

    One SQLite home for launch authority, proposal ops, pattern indexes, and
    restart-safe shipped-fix memory (#6769, #6778, #6781).
    """
    from ..infra.tech_lead_authority_store import SqliteTechLeadAuthorityStore

    return SqliteTechLeadAuthorityStore.for_repo(config.repo_root)


def create_open_issue_corpus_store(config: "Config") -> "OpenIssueCorpusStore":
    """The rebuildable SQL cache of vetted GitHub open-issue fingerprints."""

    from ..infra.open_issue_corpus_store import SqliteOpenIssueCorpusStore

    return SqliteOpenIssueCorpusStore.for_repo(config.repo_root)


def wire_tech_lead_act_executors(orchestrator: "Orchestrator") -> None:
    """Post-construction wiring for act-level tech_lead executors (#6764/#6778).

    The executors' production runners close over live orchestrator state
    (sessions, queues, the reset pipeline), so they can only be wired once
    the orchestrator exists. The authority store is handed to the applier so
    the act-level executors can finalize gated proposals (discard ops after
    terminal handling) and the create-issue boundary can record them.
    """
    from .tech_lead_reset_retry_wiring import (
        build_tech_lead_kill_session_executor,
        build_tech_lead_reset_retry_executor,
    )

    applier = orchestrator.deps.action_applier
    applier.tech_lead_reset_retry = build_tech_lead_reset_retry_executor(orchestrator)
    applier.tech_lead_kill_session = build_tech_lead_kill_session_executor(orchestrator)
    applier.tech_lead_ops = orchestrator.deps.services.tech_lead_authority
    applier.promotion_target = orchestrator.deps.services.promotion_target
    applier.expedite_lane = build_expedite_lane(orchestrator)


def build_expedite_lane(orchestrator: "Orchestrator") -> "ExpediteLane":
    """Bind the tech-lead expedite lane to live orchestrator state (#6870).

    Closes over ``orchestrator`` so every read (state.priority_queue, the
    cached runnable/scope queues, the blocking-label policy) sees the current
    tick's data, and every WRITE routes through a fresh RetryHistoryState owner
    — the applier and planning cycle never touch priority_queue directly. The
    configured cap travels on the lane so the owner enforces it atomically.

    Eligibility mirrors the scheduler's availability rule: a pending gated
    follow-up becomes promotable exactly when it is in the runnable queue with
    no blocking label left (the ``proposed-tech-lead`` gate removed), i.e. when
    it first becomes eligible for work.
    """
    from ..control.retry_history_state import (
        ExpediteEligibility,
        ExpediteLane,
        RetryHistoryState,
    )

    def eligibility() -> "ExpediteEligibility":
        state = orchestrator.state
        label_manager = orchestrator.deps.label_manager
        eligible = frozenset(
            issue.number
            for issue in state.cached_queue_issues
            if not label_manager.get_blocking(issue.labels)
        )
        in_scope = frozenset(issue.number for issue in state.cached_scope_issues)
        return ExpediteEligibility(eligible=eligible, in_scope=in_scope)

    return ExpediteLane(
        owner_factory=lambda: RetryHistoryState(orchestrator.state),
        eligibility_provider=eligibility,
        max_expedited=orchestrator.config.tech_lead.max_expedited,
    )


def create_tech_lead_board_publisher(
    config: "Config", authority: "TechLeadAuthorityStore"
) -> "TechLeadBoardPublisher | None":
    """The fact gatherer's rung-1 tech-lead-board projection sink (#6781).

    Gated on a configured tech lead agent — the board projects the tech_lead
    ledgers (the same authority store the anchor scan classifies against),
    so with no tech lead agent there is nothing to project. When absent the
    fact gatherer's ``board_publisher`` stays ``None`` and publish is never
    called (no board file written, no crash). The board is a local operator
    artifact under the repo state dir, not a UI contract; it is refreshed
    each tick the anchor scan produces tech_lead facts.
    """
    if not config.tech_lead_review_agent:
        return None
    from ..control.tech_lead_board import TechLeadBoardPublisher, tech_lead_board_path

    return TechLeadBoardPublisher(
        board_path=tech_lead_board_path(config.repo_root),
        authority=authority,
    )


def _make_provider_circuit_reader(
    config: "Config", provider_resilience: "ProviderResilienceManager"
) -> "Callable[[Issue], bool]":
    """Predicate: is this issue's provider circuit still open? (#6824 F2).

    The stuck sweep treats a ``provider-unavailable`` issue as owned by the
    resilience manager while its circuit is open (it will resume it), and only
    re-examines it once the circuit has closed and the issue was orphaned.
    """
    from ..control.label_manager import LabelManager
    from ..control.provider_availability import ProviderAvailabilityPolicy

    policy = ProviderAvailabilityPolicy(config, provider_resilience, LabelManager(config))

    def is_open(issue: "Issue") -> bool:
        return policy.is_open(policy.provider_for_issue(issue))

    return is_open


def create_tech_lead_fact_gatherer(
    config: "Config",
    repository_host: "RepositoryHost | None",
    events: "EventSink",
    authority: "TechLeadAuthorityStore",
    board_publisher: "TechLeadBoardPublisher | None",
    queue_cache_store: "QueueCacheStore | None" = None,
    provider_resilience: "ProviderResilienceManager | None" = None,
    promotion_target: "PromotionTargetHost | None" = None,
) -> "FactGatherer | None":
    """Wire the read-only tech_lead ledgers and projections as one unit.

    ``queue_cache_store`` backs the tech-lead stuck sweep's durable timer +
    recovery counters (#6823); optional so the testing composition can omit it.
    ``provider_resilience`` backs the sweep's provider-circuit ownership check
    (#6824 F2); optional (unwired => provider-unavailable issues stay skipped).
    """
    if repository_host is None:
        return None
    from ..control.fact_gatherer import FactGatherer
    from ..infra.e2e_slot_policy import make_e2e_slot_reader

    return FactGatherer(
        config=config,
        repository_host=repository_host,
        events=events,
        tech_lead_authority=authority,
        board_publisher=board_publisher,
        promotion_target=promotion_target,
        queue_cache_store=queue_cache_store,
        # First-class E2E workload observation feed (e2e.occupies_session_slot).
        # Always wired; a no-op that touches nothing while the flag is off.
        e2e_slot_reader=make_e2e_slot_reader(config),
        provider_circuit_open=(
            _make_provider_circuit_reader(config, provider_resilience)
            if provider_resilience is not None
            else None
        ),
    )


def create_tech_lead_composition(
    config: "Config",
    repository_host: "RepositoryHost | None",
    events: "EventSink",
    fact_gatherer: "FactGatherer | None" = None,
    queue_cache_store: "QueueCacheStore | None" = None,
    provider_resilience: "ProviderResilienceManager | None" = None,
) -> TechLeadComposition:
    """Build the tech_lead store and ensure both projections share one publisher."""
    authority = create_tech_lead_authority_store(config)
    open_issue_corpus = create_open_issue_corpus_store(config)
    promotion_target = create_promotion_target_host(repository_host)
    from ..control.open_issue_corpus import OpenIssueCorpusManager

    open_issue_corpus_manager = OpenIssueCorpusManager(
        repository_host,
        open_issue_corpus,
        is_enabled=lambda: bool(
            config.tech_lead_review_agent and config.tech_lead.dedup.enabled
        ),
    )
    board_publisher = (
        fact_gatherer.board_publisher
        if fact_gatherer is not None
        else create_tech_lead_board_publisher(config, authority)
    )
    if fact_gatherer is None:
        fact_gatherer = create_tech_lead_fact_gatherer(
            config, repository_host, events, authority, board_publisher,
            queue_cache_store, provider_resilience, promotion_target,
        )
    return TechLeadComposition(
        authority=authority,
        open_issue_corpus=open_issue_corpus_manager,
        board_publisher=board_publisher,
        fact_gatherer=fact_gatherer,
        promotion_target=promotion_target,
    )


def create_promotion_target_host(
    repository_host: "RepositoryHost | None",
) -> "PromotionTargetHost | None":
    """The cross-repo filing seam for finding promotion (#6957).

    Delegates adapter construction to the provider factory so this composition
    helper depends on the ``execution`` seam, not the GitHub adapter package.
    """
    from ..execution.providers import (
        create_promotion_target_host as build_promotion_target_host,
    )

    return build_promotion_target_host(repository_host)


def create_board_snapshot_builder(
    config: "Config",
    timeline_store: "TimelineStore",
    board_publisher: "TechLeadBoardPublisher | None",
    working_copy: "WorkingCopy",
) -> "BoardSnapshotBuilder":
    """ADR-0031 §3 board-snapshot fact sources over the owned timeline store."""
    from ..control.board_snapshot_builder import BoardSnapshotBuilder

    log_path = get_repo_log_path(config.repo_root)
    return BoardSnapshotBuilder(
        timeline_reader=lambda issue, limit: timeline_store.read(issue, limit=limit),
        log_tail_provider=lambda lines: read_log_tail(log_path, lines),
        case_file_reader=board_publisher.case_files if board_publisher else lambda: (),
        shipped_fix_reader=(
            board_publisher.shipped_fixes if board_publisher else lambda _limit: ()
        ),
        e2e_health_reader=_make_e2e_health_reader(config),
        session_activity_reader=_make_session_activity_reader(working_copy),
        clock=datetime.now,
    )


def _make_session_activity_reader(
    working_copy: "WorkingCopy",
) -> "Callable[[Session], SessionActivityFacts | None]":
    """Best-effort hung-EVIDENCE probe feed for each active session (ADR-0031).

    Reads two cheap signals that tell a long-running-but-working session from a
    genuinely hung one — NEVER age alone: the mtime of the session's terminal
    recording (its agent output stream, a proxy for "last observable activity",
    the same file the quiescence detector samples) and the commit count on its
    branch ahead of base. Both are best-effort: a missing recording or an
    unreadable/absent worktree degrades that field to its unknown sentinel; the
    builder's backstop maps any unexpected error to ``None``. The health review
    reads these to corroborate a hang before proposing the GATED
    ``kill_hung_session`` (which never auto-executes).
    """
    from ..domain.board_snapshot import SessionActivityFacts

    def _read(session: "Session") -> "SessionActivityFacts | None":
        return SessionActivityFacts(
            commits_ahead=_session_commits_ahead(working_copy, session),
            last_activity_at=_recording_last_activity_iso(session),
        )

    return _read


def _recording_last_activity_iso(session: "Session") -> str | None:
    """Wall-clock ISO of the session recording's last write (mtime), else ``None``.

    The agent's output stream writes the terminal recording, so its mtime is a
    pragmatic "last observable activity" timestamp. ``None`` when the file is
    missing or unreadable (``OSError``) — the builder maps that to an unknown
    idle reading rather than a bogus "idle forever".
    """
    recording_path = session.run_assets.terminal_recording.path
    try:
        mtime = recording_path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(mtime).isoformat()


def _session_commits_ahead(working_copy: "WorkingCopy", session: "Session") -> int:
    """Commits on the session branch ahead of base, or the unknown sentinel.

    ``COMMITS_AHEAD_UNKNOWN`` when the worktree is gone or the read raises: a
    real ``0`` (no commits yet — a hang signal when paired with a high idle)
    must stay distinct from "could not read". Narrows to filesystem/git errors
    (git subprocess failures are wrapped by the working-copy port as
    ``GitError``); any other unexpected failure still surfaces via the builder's
    outer best-effort backstop.
    """
    from ..domain.board_snapshot import COMMITS_AHEAD_UNKNOWN
    from ..ports.git import GitError

    worktree = session.worktree_path
    try:
        if not worktree.exists():
            return COMMITS_AHEAD_UNKNOWN
        return len(working_copy.get_commits_ahead_of_main(worktree))
    except (OSError, GitError):
        return COMMITS_AHEAD_UNKNOWN


def _make_e2e_health_reader(
    config: "Config",
) -> Callable[[datetime], "BoardE2EHealth | None"]:
    """Read-only e2e-health projection feed for the board snapshot (ADR-0031).

    Reads the aggregate E2E-suite signal (cadence, red streak, chronic
    failures, quarantine) from the repo's ``e2e.db`` over a strictly read-only
    connection, plus the configured cadence/enabled flag and quarantine list.
    Best-effort: a repo with no ``e2e.db`` (or an unreadable/table-less one)
    yields ``None`` — a health review of a repo without E2E is fine.
    """
    from ..domain.board_snapshot import RECENT_E2E_RUN_WINDOW, BoardE2EHealth
    from ..infra.e2e_health_reader import read_e2e_health_facts
    from ..infra.e2e_quarantine import load_quarantine_list

    def _read(now: datetime) -> "BoardE2EHealth | None":
        db_path = config.repo_root / ".issue-orchestrator" / "e2e.db"
        if not db_path.exists():
            return None
        try:
            runs, chronic = read_e2e_health_facts(
                db_path, recent_run_limit=RECENT_E2E_RUN_WINDOW
            )
            quarantine = load_quarantine_list(
                config.repo_root / config.e2e.quarantine_file
            )
            return BoardE2EHealth.project(
                now=now,
                enabled=config.e2e.enabled,
                expected_interval_minutes=config.e2e.auto_run_interval_minutes,
                runs=runs,
                chronic_failures=chronic,
                quarantine_count=len(quarantine),
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            logger.warning("[board] e2e health projection unavailable: %s", exc)
            return None

    return _read
