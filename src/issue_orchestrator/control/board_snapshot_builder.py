"""Builds a BoardSnapshot from OrchestratorState - facts only, no decisions.

Observation-flavored code: this module gathers facts from orchestrator state
and injected readers, and makes no decisions and no GitHub/network calls.
Every external data source (timeline store, orchestrator log, wall clock) is
an injected callable so the builder is deterministic and trivially testable:

- ``timeline_reader``: typically a lambda over the orchestrator's owned
  ``TimelineStore.read`` (wired at the composition root over the same store
  instance the timeline writer uses; control must not import execution
  directly, and the builder must not open a second store path to the db).
- ``log_tail_provider``: returns the last N lines of the orchestrator log.
- ``clock``: returns the current time; session ages are computed against it
  (never against ``datetime.now()`` internally).

Defensive bounds: the snapshot is written to a file that an agent with a
finite context window reads, so every list is capped - sessions, queue
entries, blocked issues, and failures at ``MAX_LIST_ENTRIES`` (100) each,
timeline extracts at ``MAX_TIMELINE_ISSUES`` (10) issues of at most
``timeline_limit`` records, and each log line / queue detail at
``MAX_LINE_CHARS`` (500) characters.
"""

import logging
from collections.abc import Callable, Sequence
from datetime import datetime

from ..domain.board_snapshot import (
    COMMITS_AHEAD_UNKNOWN,
    BoardAreaSignal,
    BoardBlockedIssue,
    BoardCaseFile,
    BoardE2EHealth,
    BoardFailure,
    BoardQueueEntry,
    BoardSessionInfo,
    BoardShippedFix,
    BoardSnapshot,
    BoardTimelineExtract,
    SessionActivityFacts,
    project_idle_minutes,
)
from ..domain.models import (
    DiscoveredFailure,
    OrchestratorState,
    PendingRetrospectiveReview,
    PendingRework,
    Session,
)
from ..domain.tech_lead_session import (
    TechLeadCaseFileSummary,
    TechLeadShippedFixSummary,
)
from ..ports.timeline_store import TimelineRecord

logger = logging.getLogger(__name__)

# The snapshot file is read by an agent with a finite context window, so
# every list the builder emits is bounded. See the module docstring.
MAX_LIST_ENTRIES = 100
MAX_TIMELINE_ISSUES = 10
MAX_LINE_CHARS = 500


class BoardSnapshotBuilder:
    """Assembles a BoardSnapshot from OrchestratorState and injected readers.

    Gathers facts only: no decisions, no mutations, no GitHub/network calls.
    """

    def __init__(
        self,
        *,
        timeline_reader: Callable[[int, int], Sequence[TimelineRecord]],
        log_tail_provider: Callable[[int], list[str]],
        case_file_reader: Callable[[], Sequence[TechLeadCaseFileSummary]],
        shipped_fix_reader: Callable[[int], Sequence[TechLeadShippedFixSummary]],
        e2e_health_reader: Callable[[datetime], BoardE2EHealth | None],
        session_activity_reader: Callable[[Session], SessionActivityFacts | None],
        clock: Callable[[], datetime],
    ) -> None:
        """Create a builder over injected fact sources.

        Args:
            timeline_reader: ``(issue_number, limit) -> records``; records
                must expose the ``TimelineRecord`` fields (``event_id``,
                ``timestamp``, ``event``, ``data``).
            log_tail_provider: ``(n) -> last n orchestrator log lines``.
            case_file_reader: latest open case files from the anchor scan.
            shipped_fix_reader: ``(limit) -> recent durable merged-fix facts``.
            e2e_health_reader: ``(now) -> aggregate E2E health | None``. Called
                with the same clock the builder uses so ``age``/``stale`` are
                deterministic. Best-effort: a reader that returns ``None`` or
                raises yields ``None`` here — the E2E block is an ENHANCEMENT
                (like the evidence map), never a required snapshot fact.
            session_activity_reader: ``(session) -> hung-evidence facts | None``.
                Reaches the filesystem/git to read a session's last-activity
                mtime + commits-ahead so the builder itself stays free of that
                reach. Best-effort like ``e2e_health_reader``: a reader that
                returns ``None`` or raises degrades the session's evidence
                fields to their unknown sentinels — it never fails the snapshot.
            clock: current-time source; injected so session ages are
                deterministic under test.
        """
        self._timeline_reader = timeline_reader
        self._log_tail_provider = log_tail_provider
        self._case_file_reader = case_file_reader
        self._shipped_fix_reader = shipped_fix_reader
        self._e2e_health_reader = e2e_health_reader
        self._session_activity_reader = session_activity_reader
        self._clock = clock

    def build(
        self,
        state: OrchestratorState,
        *,
        focus_issue: int | None = None,
        failures: Sequence[DiscoveredFailure] = (),
        problem_cohort: Sequence[int] = (),
        timeline_limit: int = 30,
        log_tail_lines: int = 200,
    ) -> BoardSnapshot:
        """Build a bounded snapshot of the board from ``state``.

        ``problem_cohort`` is a health review's OWNED act-level remit, given
        by the launch boundary. It is recorded verbatim as its own surface and
        is NOT derived from ``failures``: the failure list is deliberately
        broad context (live buffer + every pending investigation + every
        pending cohort), so reading authority back out of it granted reviews
        scope over unrelated issues (#6780). Unlike the other list
        fields it is not truncated — a cohort is a small, exact grant, and
        silently dropping a member would shrink authority rather than bound a
        display.

        Failures are an explicit parameter rather than read from
        ``state.discovered_failures`` because that field is a per-tick fact
        buffer: the observer appends to it during a tick and the orchestrator
        clears it alongside the other discovered facts after planning (see
        ``control.orchestrator_support._DISCOVERED_FACT_ATTRS``). Outside a
        tick it is usually empty, so callers must pass the failures they want
        surfaced (e.g. captured at completion time, or
        ``state.discovered_failures`` when building mid-tick).

        Timeline extracts cover ``focus_issue`` (first) plus every issue with
        an active session or a passed failure, deduplicated in that order and
        capped at ``MAX_TIMELINE_ISSUES`` issues of ``timeline_limit`` records
        each.

        Defensive bounds (the output is a file an agent reads): sessions,
        queue entries, blocked issues, and failures are capped at
        ``MAX_LIST_ENTRIES`` each; log lines and queue details are truncated
        to ``MAX_LINE_CHARS`` characters; the log tail is capped at
        ``log_tail_lines`` lines.
        """
        now = self._clock()
        case_files = tuple(self._case_file_reader())[:MAX_LIST_ENTRIES]
        shipped_fixes = tuple(self._shipped_fix_reader(MAX_LIST_ENTRIES))[
            :MAX_LIST_ENTRIES
        ]
        return BoardSnapshot(
            generated_at=now.isoformat(),
            orchestrator_paused=state.paused,
            sessions=[
                self._session_info(session, now)
                for session in state.active_sessions[:MAX_LIST_ENTRIES]
            ],
            queues=self._queue_entries(state)[:MAX_LIST_ENTRIES],
            blocked_issues=[
                BoardBlockedIssue(
                    issue_number=problem.issue_number,
                    issue_title=problem.issue_title,
                    summary=problem.summary,
                    blocked_by=list(problem.blocked_by),
                )
                for problem in list(state.dependency_problems.values())[
                    :MAX_LIST_ENTRIES
                ]
            ],
            recent_failures=[
                BoardFailure(
                    issue_number=failure.issue_number,
                    issue_title=failure.issue_title,
                    failure_reason=failure.failure_reason,
                    # Projected verbatim from the discovery seam: the hints
                    # were gathered at completion time from paths that existed
                    # on disk (never invented, never discarded here).
                    artifact_hints=list(failure.artifact_hints),
                )
                for failure in list(failures)[:MAX_LIST_ENTRIES]
            ],
            problem_cohort=sorted(set(problem_cohort)),
            case_files=[
                BoardCaseFile(
                    issue_number=item.issue_number,
                    title=item.title,
                    comment_count=item.comment_count,
                    updated_at=item.updated_at,
                    area=item.area,
                )
                for item in case_files
            ],
            area_signals=_area_signals(case_files, shipped_fixes),
            recent_shipped_fixes=[
                BoardShippedFix(
                    issue_number=item.issue_number,
                    title=item.title,
                    pr_url=item.pr_url,
                    area=item.area,
                    merged_at=item.merged_at,
                )
                for item in shipped_fixes
            ],
            timeline=self._timeline_extracts(
                state, focus_issue=focus_issue, failures=failures, limit=timeline_limit
            ),
            log_tail=[
                line[:MAX_LINE_CHARS]
                for line in list(self._log_tail_provider(log_tail_lines))[
                    :log_tail_lines
                ]
            ],
            e2e_health=self._read_e2e_health(now),
        )

    def _read_e2e_health(self, now: datetime) -> BoardE2EHealth | None:
        """Best-effort aggregate E2E health; never breaks the snapshot.

        Unlike the snapshot's required facts, ``e2e_health`` is an ENHANCEMENT
        (like the evidence map): a reader that returns ``None`` or raises must
        yield ``None`` here, never propagate. The reader itself narrows the
        expected db errors; this is the final backstop for anything unexpected.
        """
        try:
            return self._e2e_health_reader(now)
        except Exception as exc:
            logger.warning("[board] e2e health reader failed (non-fatal): %s", exc)
            return None

    def _session_info(self, session: Session, now: datetime) -> BoardSessionInfo:
        """Project one active session onto the board.

        ``age_minutes`` is computed from the injected clock, deliberately not
        via ``Session.runtime_minutes`` (which calls ``datetime.now``
        internally and is untestable deterministically). ``agent_type`` is ""
        when the issue carries no ``agent:*`` label - a legitimate state.

        Hung-EVIDENCE fields ride alongside so the health review can judge a
        session hung from evidence (idle with no progress), not age alone. The
        activity probe reaches the filesystem/git; the builder keeps that reach
        out of itself and projects ``idle_minutes`` against the same clock as
        ``age_minutes``. A probe that fails degrades to the unknown sentinels.
        """
        age_minutes = int((now - session.started_at).total_seconds() / 60)
        activity = self._read_session_activity(session)
        last_activity_at = activity.last_activity_at if activity else None
        return BoardSessionInfo(
            issue_number=session.issue.number,
            issue_title=session.issue.title,
            agent_type=session.issue.agent_type or "",
            session_type=session.key.task.value,
            status=session.status.value,
            started_at=session.started_at.isoformat(),
            age_minutes=age_minutes,
            terminal_id=session.terminal_id,
            run_id=session.run_assets.run_id,
            idle_minutes=project_idle_minutes(last_activity_at, now),
            commits_ahead=activity.commits_ahead if activity else COMMITS_AHEAD_UNKNOWN,
            last_activity_at=last_activity_at,
        )

    def _read_session_activity(self, session: Session) -> SessionActivityFacts | None:
        """Best-effort hung-evidence probe; never breaks the snapshot.

        Mirrors ``_read_e2e_health``: the reader itself narrows the expected
        filesystem/git errors, and this is the final backstop for anything
        unexpected. A failure yields ``None`` (all evidence fields fall to their
        unknown sentinels), never a propagated exception — the evidence is an
        ENHANCEMENT, not a required snapshot fact.
        """
        try:
            return self._session_activity_reader(session)
        except Exception as exc:
            logger.warning(
                "[board] session activity reader failed (non-fatal): %s", exc
            )
            return None

    def _queue_entries(self, state: OrchestratorState) -> list[BoardQueueEntry]:
        """Flatten the pending queues into board entries, in queue order.

        Every planner-consumed pending queue must be projected here; queue
        names mirror the ``OrchestratorSnapshot`` field names so the coverage
        test can derive the expected set from the planner contract.
        """
        entries: list[BoardQueueEntry] = []
        for review in state.pending_reviews:
            entries.append(
                BoardQueueEntry(
                    queue="pending_reviews",
                    issue_number=review.issue_number,
                    detail=_clip(
                        f"PR #{review.pr_number} awaiting review ({review.pr_url})"
                    ),
                )
            )
        for retro in state.pending_retrospective_reviews:
            entries.append(
                BoardQueueEntry(
                    queue="pending_retrospective_reviews",
                    issue_number=retro.issue_number,
                    detail=_clip(_retrospective_detail(retro)),
                )
            )
        for rework in state.pending_reworks:
            entries.append(
                BoardQueueEntry(
                    queue="pending_reworks",
                    issue_number=_rework_issue_number(rework),
                    detail=_clip(_rework_detail(rework)),
                )
            )
        for tech_lead in state.pending_tech_lead_reviews:
            entries.append(
                BoardQueueEntry(
                    queue="pending_tech_lead",
                    issue_number=tech_lead.issue_number,
                    detail=_clip(tech_lead.title),
                )
            )
        for retry in state.pending_validation_retries:
            entries.append(
                BoardQueueEntry(
                    queue="pending_validation_retries",
                    issue_number=retry.issue_number,
                    detail=_clip(
                        f"validation retry {retry.retry_count}: {retry.validation_error}"
                    ),
                )
            )
        for issue_number in state.priority_queue:
            entries.append(
                BoardQueueEntry(
                    queue="priority_queue",
                    issue_number=issue_number,
                    detail="",
                )
            )
        return entries

    def _timeline_extracts(
        self,
        state: OrchestratorState,
        *,
        focus_issue: int | None,
        failures: Sequence[DiscoveredFailure],
        limit: int,
    ) -> list[BoardTimelineExtract]:
        """Read bounded timeline extracts for the issues that matter.

        Covers ``focus_issue`` (always first) plus issues with an active
        session or a recent failure, deduplicated in that order and capped at
        ``MAX_TIMELINE_ISSUES``. Each extract holds at most ``limit`` records
        mirrored as plain dicts of the TimelineRecord fields.
        """
        candidates: list[int] = []
        if focus_issue is not None:
            candidates.append(focus_issue)
        candidates.extend(session.issue.number for session in state.active_sessions)
        candidates.extend(failure.issue_number for failure in failures)
        selected = list(dict.fromkeys(candidates))[:MAX_TIMELINE_ISSUES]
        return [
            BoardTimelineExtract(
                issue_number=issue_number,
                records=[
                    {
                        "event_id": record.event_id,
                        "timestamp": record.timestamp,
                        "event": record.event,
                        "data": record.data,
                    }
                    for record in list(self._timeline_reader(issue_number, limit))[
                        :limit
                    ]
                ],
            )
            for issue_number in selected
        ]


class StateBoardSnapshotProvider:
    """``BoardSnapshotProvider`` over live orchestrator state.

    Wired at the composition root with a state getter so consumers (the
    session launcher) never hold ``OrchestratorState`` themselves. Session
    launches run inside the tick under the state lock, so reading the live
    state here is safe.

    Failure sources are merged from two places because
    ``discovered_failures`` is a per-tick fact buffer that the orchestrator
    clears after planning (see ``BoardSnapshotBuilder.build``):

    - the live buffer, holding failures discovered THIS tick; and
    - the typed failure context preserved on queued
      ``PendingTechLeadReview`` items (``PendingTechLeadReview.failure``) — a
      failure investigation discovered on tick N launches on tick N+1, after
      the buffer was cleared, so without this merge the investigation's own
      triggering failure would be missing from its snapshot.

    The live buffer wins on duplicate issue numbers (fresher fact).
    """

    def __init__(
        self,
        builder: BoardSnapshotBuilder,
        state_getter: Callable[[], OrchestratorState],
    ) -> None:
        self._builder = builder
        self._state_getter = state_getter

    def snapshot(
        self,
        focus_issue: int | None,
        problem_cohort: tuple[int, ...] = (),
    ) -> BoardSnapshot:
        """Build a snapshot of the current board (``BoardSnapshotProvider``).

        ``problem_cohort`` is passed through verbatim from the launch boundary
        that owns the grant; this provider never derives it. The merged
        failure list below is deliberately WIDER than the cohort — it is the
        context a reviewer reads — which is exactly why authority must not be
        read back out of it (#6780).
        """
        state = self._state_getter()
        return self._builder.build(
            state,
            focus_issue=focus_issue,
            failures=_merge_failure_sources(state),
            problem_cohort=problem_cohort,
        )


def _merge_failure_sources(state: OrchestratorState) -> tuple[DiscoveredFailure, ...]:
    """Merge this tick's failure buffer with failure context preserved on the queue.

    Live buffer first (fresher facts), then queued triggering failures not
    already covered, deduplicated by issue number.
    """
    live = tuple(state.discovered_failures)
    seen = {failure.issue_number for failure in live}
    queued: list[DiscoveredFailure] = []
    for tech_lead in state.pending_tech_lead_reviews:
        candidates = (
            (tech_lead.failure,)
            if tech_lead.failure is not None
            else tech_lead.problem_cohort
        )
        for failure in candidates:
            if failure.issue_number in seen:
                continue
            queued.append(failure)
            seen.add(failure.issue_number)
    return live + tuple(queued)


def _rework_issue_number(rework: PendingRework) -> int:
    """Resolve a rework's issue number, failing fast when unresolvable.

    ``PendingRework`` is store-agnostic; a rework whose issue key is not
    numeric and that carries no explicit issue number cannot be placed on the
    board. That is an invariant violation upstream, not a display concern -
    surface it immediately rather than emitting a bogus entry.
    """
    issue_number = rework.resolve_issue_number()
    if issue_number is None:
        raise ValueError(
            f"PendingRework for issue key {rework.issue_key} has no resolvable "
            "issue number; cannot place it on the board snapshot"
        )
    return issue_number


def _rework_detail(rework: PendingRework) -> str:
    """Short human-readable elaboration for a rework queue entry."""
    parts = [f"rework cycle {rework.rework_cycle}"]
    if rework.pr_number is not None:
        parts.append(f"PR #{rework.pr_number}")
    if rework.feedback:
        parts.append(rework.feedback)
    return "; ".join(parts)


def _retrospective_detail(retro: PendingRetrospectiveReview) -> str:
    """Short human-readable elaboration for a retrospective review entry."""
    parts = [f"retrospective review of existing implementation ({retro.trigger_label})"]
    if retro.prior_pr_number is not None:
        parts.append(f"prior PR #{retro.prior_pr_number}")
    return "; ".join(parts)


def _clip(text: str) -> str:
    """Truncate a detail/log line to the per-line bound (agent-read file)."""
    return text[:MAX_LINE_CHARS]


def _area_signals(
    case_files: Sequence[TechLeadCaseFileSummary],
    shipped_fix_history: Sequence[TechLeadShippedFixSummary],
) -> list[BoardAreaSignal]:
    """Assemble bounded cross-signature step-back facts by area/seam."""
    distinct_patterns: dict[str, int] = {}
    for case_file in case_files:
        area = case_file.area or "unclassified"
        distinct_patterns[area] = distinct_patterns.get(area, 0) + 1

    shipped_fixes: dict[str, int] = {}
    for fix in shipped_fix_history:
        area = fix.area or "unclassified"
        shipped_fixes[area] = shipped_fixes.get(area, 0) + 1

    return [
        BoardAreaSignal(
            area=area,
            distinct_patterns=distinct_patterns.get(area, 0),
            shipped_fixes=shipped_fixes.get(area, 0),
        )
        for area in sorted(set(distinct_patterns) | set(shipped_fixes))
    ]
