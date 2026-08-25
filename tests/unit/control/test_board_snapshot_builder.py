"""Unit tests for BoardSnapshotBuilder.

The builder is observation-flavored: it gathers facts from a fabricated
OrchestratorState plus injected callables (timeline reader, log tail
provider, clock) and makes no decisions. These tests verify the fact
projection, the injected-clock age computation, the timeline issue
selection, and the defensive bounds documented on the builder.
"""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pytest

from issue_orchestrator.control.board_snapshot_builder import (
    MAX_LINE_CHARS,
    MAX_LIST_ENTRIES,
    MAX_TIMELINE_ISSUES,
    BoardSnapshotBuilder,
    StateBoardSnapshotProvider,
)
from issue_orchestrator.domain.board_snapshot import (
    COMMITS_AHEAD_UNKNOWN,
    IDLE_MINUTES_UNKNOWN,
    BoardE2EHealth,
    SessionActivityFacts,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadCaseFileSummary,
    TechLeadSessionFlavor,
    TechLeadShippedFixSummary,
)
from issue_orchestrator.control.pending_session_queues import PendingSessionQueues
from issue_orchestrator.domain.models import (
    AgentConfig,
    DependencyProblem,
    DiscoveredFailure,
    Issue,
    OrchestratorState,
    PendingReview,
    PendingRetrospectiveReview,
    PendingRework,
    PendingTechLeadReview,
    PendingValidationRetry,
    Session,
    SessionKey,
    TaskKind,
)
from issue_orchestrator.domain.session_run import SessionRunAssets
from issue_orchestrator.ports.timeline_store import TimelineRecord
from tests.unit.session_run_helpers import make_session_run_assets
from issue_orchestrator.domain.pause_state import (
    PauseActor,
    PauseReason,
    PauseState,
)


def _paused_state() -> PauseState:
    """An operator-paused state — the pause_state equivalent of the old ``paused=True``."""
    return PauseState.paused_now(
        reason=PauseReason.OPERATOR, actor=PauseActor.CONTROL_API
    )

FIXED_NOW = datetime(2026, 7, 10, 12, 0, 0)


class FakeTimelineReader:
    """Records calls and serves canned TimelineRecord lists per issue."""

    def __init__(self, records_by_issue: dict[int, list[TimelineRecord]] | None = None):
        self.records_by_issue = records_by_issue or {}
        self.calls: list[tuple[int, int]] = []

    def __call__(self, issue_number: int, limit: int) -> list[TimelineRecord]:
        self.calls.append((issue_number, limit))
        return self.records_by_issue.get(issue_number, [])


class FakeLogTail:
    """Records the requested line count and serves canned lines."""

    def __init__(self, lines: list[str]):
        self.lines = lines
        self.calls: list[int] = []

    def __call__(self, n: int) -> list[str]:
        self.calls.append(n)
        return self.lines


def _record(event_id: str) -> TimelineRecord:
    return TimelineRecord(
        event_id=event_id,
        timestamp="2026-07-10T11:00:00+00:00",
        event="session.started",
        data={"agent": "agent:test"},
    )


def _make_builder(
    *,
    timeline_reader: FakeTimelineReader | None = None,
    log_tail: FakeLogTail | None = None,
    case_files: tuple[TechLeadCaseFileSummary, ...] = (),
    shipped_fixes: tuple[TechLeadShippedFixSummary, ...] = (),
    e2e_health_reader: Callable[[datetime], BoardE2EHealth | None] | None = None,
    session_activity_reader: (
        Callable[[Session], SessionActivityFacts | None] | None
    ) = None,
) -> BoardSnapshotBuilder:
    return BoardSnapshotBuilder(
        timeline_reader=timeline_reader or FakeTimelineReader(),
        log_tail_provider=log_tail or FakeLogTail([]),
        case_file_reader=lambda: case_files,
        shipped_fix_reader=lambda limit: shipped_fixes[:limit],
        e2e_health_reader=e2e_health_reader or (lambda now: None),
        session_activity_reader=session_activity_reader or (lambda session: None),
        clock=lambda: FIXED_NOW,
    )


def _make_session(
    run_assets: SessionRunAssets,
    prompt_path: Path,
    *,
    issue_number: int,
    title: str = "Test issue",
    started_at: datetime,
    task: TaskKind = TaskKind.CODE,
) -> Session:
    return Session(
        key=SessionKey(issue=FakeIssueKey(str(issue_number)), task=task),
        issue=Issue(number=issue_number, title=title, labels=["agent:test"]),
        agent_config=AgentConfig(prompt_path=prompt_path, model="sonnet"),
        terminal_id=f"issue-{issue_number}",
        worktree_path=run_assets.worktree_path,
        branch_name=f"{issue_number}-test",
        run_assets=run_assets,
        started_at=started_at,
    )


@pytest.fixture
def run_assets(tmp_path: Path) -> SessionRunAssets:
    return make_session_run_assets(tmp_path / "worktree")


@pytest.fixture
def prompt_path(tmp_path: Path) -> Path:
    path = tmp_path / "prompt.md"
    path.write_text("prompt", encoding="utf-8")
    return path


class TestSessions:
    def test_session_ages_computed_from_injected_clock(
        self, run_assets: SessionRunAssets, prompt_path: Path
    ) -> None:
        s1 = _make_session(
            run_assets,
            prompt_path,
            issue_number=101,
            title="Older session",
            started_at=datetime(2026, 7, 10, 11, 15, 0),
        )
        s2 = _make_session(
            run_assets,
            prompt_path,
            issue_number=102,
            title="Newer session",
            started_at=datetime(2026, 7, 10, 11, 58, 0),
            task=TaskKind.REVIEW,
        )
        state = OrchestratorState(active_sessions=[s1, s2])

        snapshot = _make_builder().build(state)

        assert len(snapshot.sessions) == 2
        first, second = snapshot.sessions
        assert first.issue_number == 101
        assert first.issue_title == "Older session"
        assert first.agent_type == "agent:test"
        assert first.session_type == "code"
        assert first.status == "running"
        assert first.started_at == "2026-07-10T11:15:00"
        assert first.age_minutes == 45
        assert first.terminal_id == "issue-101"
        assert second.age_minutes == 2
        assert second.session_type == "review"

    def test_sessions_capped_at_max_entries(
        self, run_assets: SessionRunAssets, prompt_path: Path
    ) -> None:
        sessions = [
            _make_session(
                run_assets,
                prompt_path,
                issue_number=1000 + i,
                started_at=datetime(2026, 7, 10, 11, 0, 0),
            )
            for i in range(MAX_LIST_ENTRIES + 1)
        ]
        state = OrchestratorState(active_sessions=sessions)

        snapshot = _make_builder().build(state)

        assert len(state.active_sessions) == 101
        assert len(snapshot.sessions) == MAX_LIST_ENTRIES
        assert snapshot.sessions[0].issue_number == 1000  # first entries win


class TestPatternCaseFiles:
    def test_projects_open_case_files_and_durable_shipped_fix_facts(self) -> None:
        case_files = (
            TechLeadCaseFileSummary(
                issue_number=700, title="Pattern case file: db-timeout",
                comment_count=4, updated_at="2026-07-10T11:00:00+00:00", area="db",
            ),
            TechLeadCaseFileSummary(
                issue_number=701, title="Pattern case file: pool-starvation",
                comment_count=2, updated_at="2026-07-10T10:00:00+00:00", area="db",
            ),
        )
        shipped_fixes = (
            TechLeadShippedFixSummary(
                issue_number=600,
                title="Repair DB seam",
                pr_url="https://github.com/o/r/pull/600",
                area="db",
                merged_at="2026-07-09T12:00:00+00:00",
            ),
        )

        snapshot = _make_builder(
            case_files=case_files, shipped_fixes=shipped_fixes
        ).build(OrchestratorState())

        assert [item.issue_number for item in snapshot.case_files] == [700, 701]
        assert snapshot.case_files[0].comment_count == 4
        [signal] = snapshot.area_signals
        assert (signal.area, signal.distinct_patterns, signal.shipped_fixes) == ("db", 2, 1)
        [fix] = snapshot.recent_shipped_fixes
        assert (fix.issue_number, fix.area, fix.pr_url) == (
            600,
            "db",
            "https://github.com/o/r/pull/600",
        )

    def test_recent_shipped_fixes_are_bounded_when_reader_overdelivers(self) -> None:
        fixes = tuple(
            TechLeadShippedFixSummary(
                issue_number=600 + index,
                title=f"Fix {index}",
                pr_url=f"https://github.com/o/r/pull/{700 + index}",
                area="db",
                merged_at="2026-07-09T12:00:00+00:00",
            )
            for index in range(MAX_LIST_ENTRIES + 1)
        )
        builder = BoardSnapshotBuilder(
            timeline_reader=FakeTimelineReader(),
            log_tail_provider=FakeLogTail([]),
            case_file_reader=lambda: (),
            shipped_fix_reader=lambda _limit: fixes,
            e2e_health_reader=lambda now: None,
            session_activity_reader=lambda session: None,
            clock=lambda: FIXED_NOW,
        )

        snapshot = builder.build(OrchestratorState())

        assert len(snapshot.recent_shipped_fixes) == MAX_LIST_ENTRIES
        assert snapshot.area_signals[0].shipped_fixes == MAX_LIST_ENTRIES


class TestQueuesBlockedAndFailures:
    def _state_with_queues(self) -> OrchestratorState:
        return OrchestratorState(
            pause_state=_paused_state(),
            pending_reviews=[
                PendingReview(
                    issue_key=FakeIssueKey("201"),
                    pr_number=7,
                    pr_url="https://github.com/o/r/pull/7",
                    branch_name="201-branch",
                    _issue_number=201,
                ),
            ],
            pending_retrospective_reviews=[
                PendingRetrospectiveReview(
                    issue_key=FakeIssueKey("207"),
                    issue_number=207,
                    issue_title="Existing implementation",
                    agent_label="agent:test",
                    trigger_label="io:review-existing",
                    prior_pr_number=12,
                ),
            ],
            pending_reworks=[
                PendingRework(
                    issue_key=FakeIssueKey("202"),
                    agent_type="agent:test",
                    rework_cycle=2,
                    issue_number=202,
                    pr_number=8,
                    feedback="Missing tests",
                ),
            ],
            pending_tech_lead_reviews=[
                PendingTechLeadReview(issue_number=203, title="Tech Lead batch review", flavor=TechLeadSessionFlavor.BATCH_REVIEW),
            ],
            pending_validation_retries=[
                PendingValidationRetry(
                    issue_number=204,
                    issue_title="Validation victim",
                    agent_label="agent:test",
                    worktree_path="/wt/204",
                    branch_name="204-branch",
                    original_prompt=None,
                    validation_error="pytest exploded",
                    validation_error_file=None,
                    retry_count=1,
                    source_task=TaskKind.CODE,
                ),
            ],
            priority_queue=[42],
            dependency_problems={
                205: DependencyProblem(
                    issue_number=205,
                    issue_title="Blocked feature",
                    blocked_by=[(99, "Dependency issue", "open")],
                    summary="Blocked by 1 open dependency",
                ),
            },
        )

    @staticmethod
    def _planner_queue_names() -> set[str]:
        """Every planner-consumed pending queue, derived from the contract.

        ``OrchestratorSnapshot`` is the planner's input; its ``pending_*``
        fields plus ``priority_queue`` are exactly the capacity-consuming
        queues. Deriving the set here (instead of hand-listing) makes this
        test fail when a future queue is added to the planner contract but
        not projected onto the board snapshot.
        """
        import dataclasses

        from issue_orchestrator.control.planner_types import OrchestratorSnapshot

        return {
            f.name
            for f in dataclasses.fields(OrchestratorSnapshot)
            if f.name.startswith("pending_") or f.name == "priority_queue"
        }

    def test_queue_entries_cover_every_queue(self) -> None:
        snapshot = _make_builder().build(self._state_with_queues())

        by_queue = {entry.queue: entry for entry in snapshot.queues}
        expected = self._planner_queue_names()
        assert "pending_retrospective_reviews" in expected  # sanity: derivation works
        assert set(by_queue) == expected
        assert by_queue["pending_reviews"].issue_number == 201
        assert "PR #7" in by_queue["pending_reviews"].detail
        assert by_queue["pending_retrospective_reviews"].issue_number == 207
        assert "io:review-existing" in by_queue["pending_retrospective_reviews"].detail
        assert "prior PR #12" in by_queue["pending_retrospective_reviews"].detail
        assert by_queue["pending_reworks"].issue_number == 202
        assert "rework cycle 2" in by_queue["pending_reworks"].detail
        assert "Missing tests" in by_queue["pending_reworks"].detail
        assert by_queue["pending_tech_lead"].issue_number == 203
        assert by_queue["pending_tech_lead"].detail == "Tech Lead batch review"
        assert by_queue["pending_validation_retries"].issue_number == 204
        assert "pytest exploded" in by_queue["pending_validation_retries"].detail
        assert by_queue["priority_queue"].issue_number == 42
        assert by_queue["priority_queue"].detail == ""

    def test_retrospective_detail_without_prior_pr(self) -> None:
        state = OrchestratorState(
            pending_retrospective_reviews=[
                PendingRetrospectiveReview(
                    issue_key=FakeIssueKey("208"),
                    issue_number=208,
                    issue_title="No prior PR",
                    agent_label="agent:test",
                    trigger_label="io:review-existing",
                ),
            ],
        )

        snapshot = _make_builder().build(state)

        (entry,) = snapshot.queues
        assert entry.queue == "pending_retrospective_reviews"
        assert "io:review-existing" in entry.detail
        assert "prior PR" not in entry.detail

    def test_paused_and_generated_at_reflect_state_and_clock(self) -> None:
        snapshot = _make_builder().build(self._state_with_queues())

        assert snapshot.orchestrator_paused is True
        assert snapshot.generated_at == FIXED_NOW.isoformat()

    def test_blocked_issues_mirror_dependency_problems(self) -> None:
        snapshot = _make_builder().build(self._state_with_queues())

        assert len(snapshot.blocked_issues) == 1
        blocked = snapshot.blocked_issues[0]
        assert blocked.issue_number == 205
        assert blocked.issue_title == "Blocked feature"
        assert blocked.summary == "Blocked by 1 open dependency"
        assert blocked.blocked_by == [(99, "Dependency issue", "open")]

    def test_failures_taken_from_explicit_parameter(self) -> None:
        failures = [
            DiscoveredFailure(
                issue_number=206,
                issue_title="Failed thing",
                failure_reason="timed_out",
                artifact_hints=(
                    "/runs/issue-206/failure-diagnostic.md",
                    "/runs/issue-206/analysis.json",
                ),
            ),
        ]

        snapshot = _make_builder().build(OrchestratorState(), failures=failures)

        assert len(snapshot.recent_failures) == 1
        failure = snapshot.recent_failures[0]
        assert failure.issue_number == 206
        assert failure.issue_title == "Failed thing"
        assert failure.failure_reason == "timed_out"
        # #6762: hints gathered at the discovery seam are projected verbatim
        # — never invented, never discarded.
        assert failure.artifact_hints == [
            "/runs/issue-206/failure-diagnostic.md",
            "/runs/issue-206/analysis.json",
        ]

    def test_failure_without_hints_projects_empty_hints(self) -> None:
        """A failure source with no artifacts yields empty hints, not fabricated ones."""
        failures = [
            DiscoveredFailure(
                issue_number=207, issue_title="No artifacts", failure_reason="failed"
            ),
        ]

        snapshot = _make_builder().build(OrchestratorState(), failures=failures)

        assert snapshot.recent_failures[0].artifact_hints == []

    def test_queues_capped_at_max_entries(self) -> None:
        state = OrchestratorState(priority_queue=list(range(MAX_LIST_ENTRIES + 50)))

        snapshot = _make_builder().build(state)

        assert len(snapshot.queues) == MAX_LIST_ENTRIES

    def test_long_queue_detail_is_truncated(self) -> None:
        state = OrchestratorState(
            pending_tech_lead_reviews=[
                PendingTechLeadReview(issue_number=203, title="x" * (MAX_LINE_CHARS + 100), flavor=TechLeadSessionFlavor.BATCH_REVIEW),
            ],
        )

        snapshot = _make_builder().build(state)

        assert len(snapshot.queues[0].detail) == MAX_LINE_CHARS

    def test_unresolvable_rework_issue_number_fails_fast(self) -> None:
        state = OrchestratorState(
            pending_reworks=[
                PendingRework(issue_key=FakeIssueKey("not-a-number"), agent_type="agent:test"),
            ],
        )

        with pytest.raises(ValueError, match="not-a-number"):
            _make_builder().build(state)


class TestTimeline:
    def test_focus_issue_timeline_included_first_and_deduped(
        self, run_assets: SessionRunAssets, prompt_path: Path
    ) -> None:
        session = _make_session(
            run_assets,
            prompt_path,
            issue_number=101,
            started_at=datetime(2026, 7, 10, 11, 0, 0),
        )
        state = OrchestratorState(active_sessions=[session])
        failures = [
            DiscoveredFailure(issue_number=206, issue_title="Failed", failure_reason="failed"),
            # Duplicate of the focus issue - must not produce a second extract.
            DiscoveredFailure(issue_number=300, issue_title="Also focus", failure_reason="failed"),
        ]
        reader = FakeTimelineReader({300: [_record("evt-focus")], 101: [_record("evt-s")]})
        builder = _make_builder(timeline_reader=reader)

        snapshot = builder.build(state, focus_issue=300, failures=failures, timeline_limit=5)

        assert [t.issue_number for t in snapshot.timeline] == [300, 101, 206]
        assert reader.calls == [(300, 5), (101, 5), (206, 5)]
        focus_extract = snapshot.timeline[0]
        assert focus_extract.records == [
            {
                "event_id": "evt-focus",
                "timestamp": "2026-07-10T11:00:00+00:00",
                "event": "session.started",
                "data": {"agent": "agent:test"},
            },
        ]
        assert snapshot.timeline[2].records == []  # no records for issue 206

    def test_timeline_issues_capped(
        self, run_assets: SessionRunAssets, prompt_path: Path
    ) -> None:
        sessions = [
            _make_session(
                run_assets,
                prompt_path,
                issue_number=1000 + i,
                started_at=datetime(2026, 7, 10, 11, 0, 0),
            )
            for i in range(MAX_TIMELINE_ISSUES + 2)
        ]
        reader = FakeTimelineReader()
        builder = _make_builder(timeline_reader=reader)

        snapshot = builder.build(
            OrchestratorState(active_sessions=sessions), focus_issue=999
        )

        assert len(snapshot.timeline) == MAX_TIMELINE_ISSUES
        assert snapshot.timeline[0].issue_number == 999  # focus issue always first
        assert len(reader.calls) == MAX_TIMELINE_ISSUES

    def test_timeline_records_capped_at_limit(self) -> None:
        reader = FakeTimelineReader({500: [_record(f"evt-{i}") for i in range(10)]})
        builder = _make_builder(timeline_reader=reader)

        snapshot = builder.build(OrchestratorState(), focus_issue=500, timeline_limit=3)

        assert len(snapshot.timeline[0].records) == 3


class TestLogTail:
    def test_log_tail_provider_receives_requested_line_count(self) -> None:
        log_tail = FakeLogTail(["line one", "line two"])
        builder = _make_builder(log_tail=log_tail)

        snapshot = builder.build(OrchestratorState(), log_tail_lines=50)

        assert log_tail.calls == [50]
        assert snapshot.log_tail == ["line one", "line two"]

    def test_log_lines_truncated_and_capped(self) -> None:
        long_line = "y" * (MAX_LINE_CHARS + 200)
        log_tail = FakeLogTail([long_line, "short", "extra-beyond-cap"])
        builder = _make_builder(log_tail=log_tail)

        snapshot = builder.build(OrchestratorState(), log_tail_lines=2)

        # Provider over-delivered: list capped at log_tail_lines, lines clipped.
        assert len(snapshot.log_tail) == 2
        assert snapshot.log_tail[0] == "y" * MAX_LINE_CHARS
        assert snapshot.log_tail[1] == "short"


class TestE2EHealth:
    """The e2e-health block is an ENHANCEMENT: carried when present, best-effort
    when the reader fails — a failure must never break the snapshot."""

    def _health(self) -> BoardE2EHealth:
        return BoardE2EHealth(
            enabled=True,
            expected_interval_minutes=240,
            stale=True,
            nonpassing_streak=8,
            quarantine_count=0,
        )

    def test_reader_result_is_carried_onto_the_snapshot(self) -> None:
        health = self._health()

        snapshot = _make_builder(e2e_health_reader=lambda now: health).build(
            OrchestratorState()
        )

        assert snapshot.e2e_health is health

    def test_reader_receives_the_builder_clock(self) -> None:
        seen: list[datetime] = []

        def reader(now: datetime) -> BoardE2EHealth | None:
            seen.append(now)
            return None

        _make_builder(e2e_health_reader=reader).build(OrchestratorState())

        assert seen == [FIXED_NOW]

    def test_reader_returning_none_leaves_health_absent(self) -> None:
        snapshot = _make_builder(e2e_health_reader=lambda now: None).build(
            OrchestratorState()
        )

        assert snapshot.e2e_health is None

    def test_raising_reader_never_breaks_the_snapshot(self) -> None:
        def boom(now: datetime) -> BoardE2EHealth | None:
            raise RuntimeError("db exploded unexpectedly")

        snapshot = _make_builder(e2e_health_reader=boom).build(OrchestratorState())

        assert snapshot.e2e_health is None
        # The rest of the snapshot is intact.
        assert snapshot.generated_at == FIXED_NOW.isoformat()


class TestSessionActivity:
    """Hung-EVIDENCE probe: idle_minutes + commits_ahead ride each session.

    The health review must judge a session hung from EVIDENCE (idle with no
    progress), never age alone, so the builder projects an injected best-effort
    probe onto every active session. Like the e2e-health block it is an
    ENHANCEMENT: a probe that returns None or raises degrades to the unknown
    sentinels and never breaks the snapshot. ``idle_minutes`` is projected from
    the probe's ``last_activity_at`` against the builder clock (FIXED_NOW).
    """

    def _session(
        self,
        run_assets: SessionRunAssets,
        prompt_path: Path,
        *,
        issue_number: int = 101,
    ) -> Session:
        # started 3h before FIXED_NOW: OLD by age, so age alone cannot decide.
        return _make_session(
            run_assets,
            prompt_path,
            issue_number=issue_number,
            started_at=datetime(2026, 7, 10, 9, 0, 0),
        )

    def test_high_idle_and_zero_commits_surface_as_hang_evidence(
        self, run_assets: SessionRunAssets, prompt_path: Path
    ) -> None:
        def reader(session: Session) -> SessionActivityFacts:
            # Last output 90m ago, no commits landed: a hang signal.
            return SessionActivityFacts(
                commits_ahead=0, last_activity_at="2026-07-10T10:30:00"
            )

        state = OrchestratorState(
            active_sessions=[self._session(run_assets, prompt_path)]
        )

        info = _make_builder(session_activity_reader=reader).build(state).sessions[0]

        assert info.age_minutes == 180  # old by age...
        assert info.idle_minutes == 90  # ...and idle with...
        assert info.commits_ahead == 0  # ...no progress => hang evidence
        assert info.last_activity_at == "2026-07-10T10:30:00"

    def test_recent_commits_and_low_idle_surface_as_working(
        self, run_assets: SessionRunAssets, prompt_path: Path
    ) -> None:
        def reader(session: Session) -> SessionActivityFacts:
            # Same 3h-old session, but fresh output and commits still landing.
            return SessionActivityFacts(
                commits_ahead=4, last_activity_at="2026-07-10T11:58:00"
            )

        state = OrchestratorState(
            active_sessions=[self._session(run_assets, prompt_path)]
        )

        info = _make_builder(session_activity_reader=reader).build(state).sessions[0]

        assert info.age_minutes == 180  # old by age, but NOT hung:
        assert info.idle_minutes == 2  # fresh output...
        assert info.commits_ahead == 4  # ...and commits landing

    def test_probe_returning_none_yields_unknown_sentinels(
        self, run_assets: SessionRunAssets, prompt_path: Path
    ) -> None:
        state = OrchestratorState(
            active_sessions=[self._session(run_assets, prompt_path)]
        )

        info = (
            _make_builder(session_activity_reader=lambda session: None)
            .build(state)
            .sessions[0]
        )

        assert info.idle_minutes == IDLE_MINUTES_UNKNOWN
        assert info.commits_ahead == COMMITS_AHEAD_UNKNOWN
        assert info.last_activity_at is None

    def test_partial_probe_fills_readable_field_only(
        self, run_assets: SessionRunAssets, prompt_path: Path
    ) -> None:
        """commits readable, recording mtime not: only the readable field fills."""

        def reader(session: Session) -> SessionActivityFacts:
            return SessionActivityFacts(commits_ahead=3, last_activity_at=None)

        state = OrchestratorState(
            active_sessions=[self._session(run_assets, prompt_path)]
        )

        info = _make_builder(session_activity_reader=reader).build(state).sessions[0]

        assert info.commits_ahead == 3
        assert info.idle_minutes == IDLE_MINUTES_UNKNOWN
        assert info.last_activity_at is None

    def test_raising_probe_never_breaks_the_snapshot(
        self, run_assets: SessionRunAssets, prompt_path: Path
    ) -> None:
        def boom(session: Session) -> SessionActivityFacts | None:
            raise RuntimeError("git blew up unexpectedly")

        state = OrchestratorState(
            active_sessions=[self._session(run_assets, prompt_path)]
        )

        snapshot = _make_builder(session_activity_reader=boom).build(state)

        info = snapshot.sessions[0]
        assert info.idle_minutes == IDLE_MINUTES_UNKNOWN
        assert info.commits_ahead == COMMITS_AHEAD_UNKNOWN
        assert info.last_activity_at is None
        # The rest of the snapshot is intact.
        assert snapshot.generated_at == FIXED_NOW.isoformat()

    def test_probe_is_called_for_each_active_session(
        self, run_assets: SessionRunAssets, prompt_path: Path
    ) -> None:
        seen: list[int] = []

        def reader(session: Session) -> SessionActivityFacts:
            seen.append(session.issue.number)
            return SessionActivityFacts(commits_ahead=1, last_activity_at=None)

        state = OrchestratorState(
            active_sessions=[
                self._session(run_assets, prompt_path, issue_number=101),
                self._session(run_assets, prompt_path, issue_number=102),
            ]
        )

        _make_builder(session_activity_reader=reader).build(state)

        assert seen == [101, 102]


class TestStateBoardSnapshotProvider:
    """StateBoardSnapshotProvider binds the builder to live orchestrator state.

    It is the composition-root seam behind the BoardSnapshotProvider port:
    the launcher calls ``snapshot(focus_issue)`` and the provider must read
    the *current* state through the getter and forward this tick's
    ``discovered_failures`` (a per-tick buffer the orchestrator clears after
    planning).
    """

    def test_snapshot_reads_current_state_and_forwards_failures(self) -> None:
        state = OrchestratorState(pause_state=_paused_state())
        state.discovered_failures.append(
            DiscoveredFailure(
                issue_number=301, issue_title="Boom", failure_reason="failed"
            )
        )
        provider = StateBoardSnapshotProvider(_make_builder(), lambda: state)

        snapshot = provider.snapshot(None)

        assert snapshot.orchestrator_paused is True
        assert [f.issue_number for f in snapshot.recent_failures] == [301]

    def test_focus_issue_is_forwarded_to_the_builder(self) -> None:
        timeline_reader = FakeTimelineReader({777: [_record("e1")]})
        provider = StateBoardSnapshotProvider(
            _make_builder(timeline_reader=timeline_reader),
            lambda: OrchestratorState(),
        )

        snapshot = provider.snapshot(777)

        assert [t.issue_number for t in snapshot.timeline] == [777]
        assert timeline_reader.calls and timeline_reader.calls[0][0] == 777

    def test_queued_failure_context_survives_cleared_tick_buffer(self) -> None:
        """P1 regression: discovery on tick N, launch on tick N+1.

        ``discovered_failures`` is cleared after planning, so by the time the
        queued failure investigation launches the buffer is empty. The typed
        failure context preserved on the queue item (via the real queue
        owner, PendingSessionQueues) must appear in ``recent_failures`` — a
        failure investigation whose snapshot is missing its own triggering
        failure was the defect.
        """
        state = OrchestratorState()
        failure = DiscoveredFailure(
            issue_number=904,
            issue_title="Broken thing",
            failure_reason="timed_out",
            artifact_hints=("/runs/issue-904/failure-diagnostic.md",),
        )
        # Tick N: producer queues the investigation with its failure context.
        PendingSessionQueues(state).queue_failure_investigation(
            904, "Investigate: Broken thing (timed_out)", failure=failure
        )
        # Tick boundary: the orchestrator clears the per-tick fact buffer.
        state.discovered_failures.clear()
        provider = StateBoardSnapshotProvider(_make_builder(), lambda: state)

        snapshot = provider.snapshot(904)

        assert [
            (f.issue_number, f.issue_title, f.failure_reason, f.artifact_hints)
            for f in snapshot.recent_failures
        ] == [
            (
                904,
                "Broken thing",
                "timed_out",
                ["/runs/issue-904/failure-diagnostic.md"],
            )
        ]
        # The triggering failure also drives a timeline extract for the issue.
        assert [t.issue_number for t in snapshot.timeline] == [904]

    def test_live_buffer_wins_over_queued_context_on_duplicate_issue(self) -> None:
        """Merge rule: live buffer first, queued context deduped by issue."""
        state = OrchestratorState()
        PendingSessionQueues(state).queue_failure_investigation(
            904,
            "Investigate: Broken thing (timed_out)",
            failure=DiscoveredFailure(
                issue_number=904, issue_title="Stale title", failure_reason="timed_out"
            ),
        )
        PendingSessionQueues(state).queue_failure_investigation(
            905,
            "Investigate: Other thing (failed)",
            failure=DiscoveredFailure(
                issue_number=905, issue_title="Other thing", failure_reason="failed"
            ),
        )
        # This tick re-discovered issue 904's failure with fresher facts.
        state.discovered_failures.append(
            DiscoveredFailure(
                issue_number=904, issue_title="Fresh title", failure_reason="failed"
            )
        )
        provider = StateBoardSnapshotProvider(_make_builder(), lambda: state)

        snapshot = provider.snapshot(None)

        assert [
            (f.issue_number, f.issue_title) for f in snapshot.recent_failures
        ] == [(904, "Fresh title"), (905, "Other thing")]

    def test_batch_review_queue_items_add_no_failures(self) -> None:
        """Batch reviews carry no failure context and must not fabricate any."""
        state = OrchestratorState()
        PendingSessionQueues(state).queue_batch_review(7, "Tech Lead Batch")
        provider = StateBoardSnapshotProvider(_make_builder(), lambda: state)

        snapshot = provider.snapshot(None)

        assert snapshot.recent_failures == []

    def test_storm_health_queue_preserves_problem_cohort_after_tick(self) -> None:
        """The health launch snapshot retains the whole storm after the
        per-tick discovery buffer is cleared (#6780).

        Also pins the context/authority split (R4 F1): the merged failure list
        is CONTEXT the provider assembles, while the cohort surface is only
        ever what the launch boundary grants — the provider never promotes its
        own merged failures into authority.
        """
        state = OrchestratorState()
        cohort = tuple(
            DiscoveredFailure(
                issue_number=number,
                issue_title=f"Problem {number}",
                failure_reason="blocked" if number == 42 else "failed",
                observed_at=1_000.0,
            )
            for number in (41, 42, 43)
        )
        PendingSessionQueues(state).queue_health_review(
            900,
            "Health Review — walk the floor",
            problem_cohort=cohort,
        )
        state.discovered_failures.clear()

        provider = StateBoardSnapshotProvider(_make_builder(), lambda: state)
        snapshot = provider.snapshot(None, (41, 42, 43))

        assert [failure.issue_number for failure in snapshot.recent_failures] == [
            41,
            42,
            43,
        ]
        assert snapshot.problem_issue_numbers() == frozenset({41, 42, 43})

        # Same board, no grant: the failures are still there to read, but the
        # provider grants nothing on its own.
        ungranted = provider.snapshot(None)
        assert [f.issue_number for f in ungranted.recent_failures] == [41, 42, 43]
        assert ungranted.problem_issue_numbers() == frozenset()
