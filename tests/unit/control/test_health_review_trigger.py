"""Trigger-side lifecycle for the periodic health review (ADR-0031 §4, #6763).

These tests drive the real ``health_review_trigger`` functions through fake
ports (a durable store and a repository host), never internal helpers, and
cover the two reliability findings that the batch/planner tests cannot reach:

* **finding 6** — a lost persist of ``last_health_review_at`` must NOT re-fire
  the review after the anchor closes; a restart reconciles the interval from
  the anchor issue's own creation time, the crash-safe truth.
* **finding 7** — one scoped, exhaustive anchor-discovery owner backs both
  fact gathering and startup recovery, so neither strands an older anchor nor
  crosses the ``filtering.label`` boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone

from types import SimpleNamespace

import pytest

from issue_orchestrator.adapters.github.errors import GitHubHttpError
from issue_orchestrator.control.actions import ActionResult, CreateTechLeadIssueAction
from issue_orchestrator.control.health_review_trigger import (
    HEALTH_REVIEW_ISSUE_TITLE,
    board_review_fingerprint,
    discover_open_health_review_anchor,
    discover_open_tech_lead_anchor_issues,
    ensure_on_demand_health_review_anchor,
    health_review_decision,
    health_review_due,
    hydrate_last_health_review_at,
    intake_created_tech_lead_anchor,
    most_recent_health_anchor_created_at,
    plan_health_review_issue_creation,
    record_health_review_creation,
)
from issue_orchestrator.control.tech_lead_issue_policy import (
    health_review_issue_labels,
)
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    DiscoveredFailure,
    Issue,
    OrchestratorState,
    TechLeadFacts,
)
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.tech_lead_session import (
    HEALTH_REVIEW_MARKER_LABEL,
    TechLeadSessionFlavor,
)
from issue_orchestrator.infra.config import Config


def _config(interval_minutes: int = 60, *, filter_label: str | None = None) -> Config:
    config = Config()
    config.tech_lead_review_agent = "agent:tech-lead"
    config.tech_lead.health_review.interval_minutes = interval_minutes
    config.filtering.label = filter_label
    return config


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class _FakeStore:
    """QueueCacheStore fake for the durable last-fired timestamp."""

    def __init__(
        self,
        stored: float = 0.0,
        *,
        raise_on_save: bool = False,
        stored_fingerprint: str = "",
    ) -> None:
        self._stored = stored
        self._stored_fingerprint = stored_fingerprint
        self.raise_on_save = raise_on_save
        self.saved: list[float] = []
        self.saved_fingerprints: list[str] = []

    def load_last_health_review_at(self) -> float:
        return self._stored

    def save_last_health_review_at(self, value: float) -> None:
        if self.raise_on_save:
            raise OSError("disk full")
        self._stored = value
        self.saved.append(value)

    def load_last_reviewed_board_fingerprint(self) -> str:
        return self._stored_fingerprint

    def save_last_reviewed_board_fingerprint(self, value: str) -> None:
        if self.raise_on_save:
            raise OSError("disk full")
        self._stored_fingerprint = value
        self.saved_fingerprints.append(value)

    def load_last_stuck_sweep_at(self) -> float:
        return 0.0

    def save_last_stuck_sweep_at(self, value: float) -> None:
        pass

    def load_recovery_attempts(self) -> dict[int, int]:
        return {}

    def save_recovery_attempts(self, value: dict[int, int]) -> None:
        pass

    def load_pending_escalations(self) -> set[int]:
        return set()

    def save_pending_escalations(self, value: set[int]) -> None:
        pass


class _AnchorTracker:
    """RepositoryHost fake honoring GitHub's label AND-filter, state, and limit."""

    def __init__(self, issues) -> None:
        self._issues = list(issues)
        self.calls: list[dict] = []

    def list_issues(self, labels=None, state="open", limit=100, **kwargs):
        self.calls.append(
            {
                "labels": list(labels or []),
                "state": state,
                "limit": limit,
                "exhaustive": kwargs.get("exhaustive", False),
            }
        )
        wanted = {label.casefold() for label in (labels or [])}
        matched = [
            issue
            for issue in self._issues
            if state == "all" or issue.state == state
            if wanted <= {label.casefold() for label in issue.labels}
        ]
        return matched[:limit]


def _anchor(number: int, epoch: float, *, state: str = "closed", config: Config):
    return Issue(
        number=number,
        title=HEALTH_REVIEW_ISSUE_TITLE,
        labels=list(health_review_issue_labels(config)),
        state=state,
        created_at=_iso(epoch),
    )


class TestPersistFailureReconciliation:
    """Finding 6: a lost persist can never re-fire the review early."""

    def test_record_swallows_persist_failure_but_stamps_memory(self) -> None:
        """The created issue must not be reported as an apply failure, yet the
        in-memory stamp still guards the current process."""
        config = _config()
        store = _FakeStore(raise_on_save=True)
        state = OrchestratorState()
        action = CreateTechLeadIssueAction(
            title=HEALTH_REVIEW_ISSUE_TITLE,
            labels=health_review_issue_labels(config),
        )

        # Must not raise even though the store persist fails.
        record_health_review_creation(action, state, store, now=1_000_000.0)

        assert state.last_health_review_at == 1_000_000.0
        assert store.load_last_health_review_at() == 0.0  # persist was lost

    def test_restart_after_lost_persist_does_not_refire_before_interval(self) -> None:
        """The full failure path: fire -> persist fails -> anchor closes ->
        restart hydrates 0.0 from the store -> reconcile from the anchor's
        created_at, so the review is NOT due again until the interval elapses.
        """
        config = _config(interval_minutes=60)
        store = _FakeStore(stored=0.0, raise_on_save=True)
        fired_at = 1_000_000.0
        action = CreateTechLeadIssueAction(
            title=HEALTH_REVIEW_ISSUE_TITLE,
            labels=health_review_issue_labels(config),
        )

        live = OrchestratorState()
        record_health_review_creation(action, live, store, now=fired_at)
        assert store.load_last_health_review_at() == 0.0  # nothing durable landed

        # Contrast: plain store hydration (0.0) WOULD re-fire immediately.
        naive = OrchestratorState()
        naive.priority_queue = [42]  # non-empty board: isolate the interval gate
        naive.last_health_review_at = store.load_last_health_review_at()
        assert health_review_due(config, naive, now=fired_at + 1800) is True

        # Reconciled restart: the closed anchor carries the true fire time.
        tracker = _AnchorTracker([_anchor(200, fired_at, config=config)])
        restarted = OrchestratorState()
        restarted.priority_queue = [42]  # non-empty board: isolate the interval gate
        hydrate_last_health_review_at(config, restarted, store, tracker)

        assert restarted.last_health_review_at == pytest.approx(fired_at)
        assert health_review_due(config, restarted, now=fired_at + 1800) is False
        assert health_review_due(config, restarted, now=fired_at + 3700) is True

    def test_reconciliation_self_heals_the_store_when_persist_recovers(self) -> None:
        """Once the store can write again, the reconciled value is persisted so
        the next restart takes the fast path."""
        config = _config(interval_minutes=60)
        fired_at = 2_000_000.0
        store = _FakeStore(stored=0.0)  # save now succeeds
        tracker = _AnchorTracker([_anchor(200, fired_at, config=config)])

        state = OrchestratorState()
        hydrate_last_health_review_at(config, state, store, tracker)

        assert state.last_health_review_at == pytest.approx(fired_at)
        assert store.load_last_health_review_at() == pytest.approx(fired_at)

    def test_store_ahead_of_anchor_is_not_downgraded(self) -> None:
        """The store is the fast path; reconciliation only pulls FORWARD, never
        back to an older anchor."""
        config = _config(interval_minutes=60)
        newer = 5_000_000.0
        older = 4_000_000.0
        store = _FakeStore(stored=newer)
        tracker = _AnchorTracker([_anchor(200, older, config=config)])

        state = OrchestratorState()
        hydrate_last_health_review_at(config, state, store, tracker)

        assert state.last_health_review_at == pytest.approx(newer)

    def test_no_anchor_leaves_store_value_untouched(self) -> None:
        config = _config(interval_minutes=60)
        store = _FakeStore(stored=1234.0)
        tracker = _AnchorTracker([])

        state = OrchestratorState()
        hydrate_last_health_review_at(config, state, store, tracker)

        assert state.last_health_review_at == pytest.approx(1234.0)
        assert most_recent_health_anchor_created_at(tracker, config) == 0.0

    def test_disabled_interval_hydrates_store_without_a_github_call(self) -> None:
        """A disabled trigger reconciles nothing and pays no anchor scan."""
        config = _config(interval_minutes=0)
        store = _FakeStore(stored=42.0)
        tracker = _AnchorTracker([_anchor(200, 9_000_000.0, config=config)])

        state = OrchestratorState()
        hydrate_last_health_review_at(config, state, store, tracker)

        assert state.last_health_review_at == pytest.approx(42.0)
        assert tracker.calls == []  # no GitHub scan when disabled


class TestSharedAnchorDiscovery:
    """Finding 7: one scoped, exhaustive discovery owner for both paths."""

    def test_discovery_ignores_anchors_outside_the_filter_scope(self) -> None:
        config = _config(filter_label="io:e2e:run-1")
        in_scope = Issue(
            number=1,
            title="Batch",
            labels=["agent:tech-lead", "io:e2e:run-1"],
            state="open",
        )
        out_of_scope = Issue(
            number=2,
            title="Health Review — walk the floor",
            labels=["agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL],
            state="open",
        )
        tracker = _AnchorTracker([in_scope, out_of_scope])

        found = discover_open_tech_lead_anchor_issues(tracker, config)

        assert [issue.number for issue in found] == [1]

    def test_discovery_is_exhaustive_past_a_twenty_item_page(self) -> None:
        """More than twenty candidate anchors are ALL discovered — no fixed
        first page strands an older anchor (the pre-fix startup limit=20 bug)."""
        config = _config()
        crowd = [
            Issue(number=n, title=f"Batch {n}", labels=["agent:tech-lead"], state="open")
            for n in range(1, 26)
        ]
        tracker = _AnchorTracker(crowd)

        found = discover_open_tech_lead_anchor_issues(tracker, config)

        assert len(found) == 25
        assert all(call["limit"] >= 25 for call in tracker.calls)

    def test_discovery_requests_fail_loud_exhaustive_scan(self) -> None:
        """R17: the authoritative scan is requested ``exhaustive`` so the pager
        fails loud on a truncated read instead of returning a partial set."""
        config = _config()
        tracker = _AnchorTracker(
            [Issue(number=1, title="Batch", labels=["agent:tech-lead"], state="open")]
        )

        discover_open_tech_lead_anchor_issues(tracker, config)

        assert tracker.calls
        assert all(call.get("exhaustive") is True for call in tracker.calls)

    def test_discovery_propagates_truncated_scan_error_as_blocking(self) -> None:
        """R17: a fail-loud scan error must PROPAGATE — planning/recovery cannot
        proceed from a partial anchor set. It is never swallowed as 'no anchors'
        (which would cause duplicate creation or a missed startup recovery)."""

        class _RaisingTracker:
            def list_issues(self, labels=None, state="open", limit=100, **kwargs):
                raise GitHubHttpError(
                    "GitHub returned status 500 while paging repository issues",
                    method="GET",
                    url="/repos/o/r/issues",
                    status_code=500,
                )

        with pytest.raises(GitHubHttpError):
            discover_open_tech_lead_anchor_issues(_RaisingTracker(), _config())

    def test_marker_scoped_lookup_finds_anchor_beyond_the_broad_page(self) -> None:
        config = _config()
        crowd = [
            Issue(number=n, title=f"Batch {n}", labels=["agent:tech-lead"], state="open")
            for n in range(1, 15)
        ]
        anchor = Issue(
            number=200,
            title=HEALTH_REVIEW_ISSUE_TITLE,
            labels=["agent:tech-lead", HEALTH_REVIEW_MARKER_LABEL],
            state="open",
        )
        tracker = _AnchorTracker([*crowd, anchor])

        assert discover_open_health_review_anchor(tracker, config) == 200
        assert any(
            HEALTH_REVIEW_MARKER_LABEL in call["labels"] for call in tracker.calls
        )


def _board(*, blocked=(), queue=(), sessions=()) -> OrchestratorState:
    """An OrchestratorState carrying a minimal reviewable board."""
    state = OrchestratorState()
    state.dependency_problems = {n: object() for n in blocked}  # only keys are read
    state.priority_queue = list(queue)
    state.active_sessions = list(sessions)
    return state


def _storm_problem(number: int) -> DiscoveredFailure:
    return DiscoveredFailure(
        issue_number=number,
        issue_title=f"boom #{number}",
        failure_reason="failed",
    )


def _session(
    number: int,
    last_output_at: "float | None",
    *,
    task: TaskKind = TaskKind.CODE,
    started_at: "datetime | None" = None,
):
    """Stand-in exposing only what board_review_fingerprint reads on a session:
    ``key.stable_id()``, ``last_output_at``, and ``started_at``."""
    return SimpleNamespace(
        key=SessionKey(issue=FakeIssueKey(str(number)), task=task),
        last_output_at=last_output_at,
        started_at=started_at or datetime.fromtimestamp(0.0),
    )


class TestBoardReviewFingerprint:
    def test_empty_board_is_blank(self) -> None:
        assert board_review_fingerprint(OrchestratorState(), now=1000.0) == ""

    def test_nonempty_board_is_identity_sensitive(self) -> None:
        fp_a = board_review_fingerprint(_board(blocked=[500]), now=1000.0)
        fp_b = board_review_fingerprint(_board(blocked=[501]), now=1000.0)
        assert fp_a and fp_b and fp_a != fp_b

    def test_time_alone_does_not_change_it(self) -> None:
        state = _board(queue=[7])
        assert board_review_fingerprint(state, now=1000.0) == board_review_fingerprint(
            state, now=9_000.0
        )

    def test_hung_session_flips_the_fingerprint(self) -> None:
        state = _board(sessions=[_session(7, last_output_at=1000.0)])
        fresh = board_review_fingerprint(state, now=1000.0 + 60)  # just emitted
        hung = board_review_fingerprint(state, now=1000.0 + 31 * 60)  # silent > 30m
        assert fresh != hung


class TestHealthReviewDueGate:
    def test_unchanged_board_is_not_due(self) -> None:
        config = _config(interval_minutes=60)
        state = _board(queue=[7])
        state.last_health_review_at = 1000.0
        state.last_reviewed_board_fingerprint = board_review_fingerprint(state, 1000.0)
        assert health_review_due(config, state, now=1000.0 + 3600) is False

    def test_changed_board_is_due(self) -> None:
        config = _config(interval_minutes=60)
        # A different blocked issue is a genuine board change (blocked issues are
        # tracked by identity, unlike pending-queue depth).
        state = _board(blocked=[500])
        state.last_health_review_at = 1000.0
        state.last_reviewed_board_fingerprint = board_review_fingerprint(
            _board(blocked=[501]), 1000.0
        )
        assert health_review_due(config, state, now=1000.0 + 3600) is True

    def test_empty_board_is_never_due(self) -> None:
        config = _config(interval_minutes=60)
        assert health_review_due(config, OrchestratorState(), now=999_999.0) is False

    def test_first_run_with_backlog_is_due(self) -> None:
        config = _config(interval_minutes=60)
        state = _board(blocked=[500])  # never reviewed -> fingerprint ""
        assert health_review_due(config, state, now=999_999.0) is True

    def test_not_due_within_interval_even_when_changed(self) -> None:
        config = _config(interval_minutes=60)
        state = _board(queue=[7])
        state.last_health_review_at = 1000.0
        assert health_review_due(config, state, now=1000.0 + 1800) is False


class TestFingerprintStampAndHydrate:
    def _action(self, config, fingerprint: str = "") -> CreateTechLeadIssueAction:
        return CreateTechLeadIssueAction(
            title=HEALTH_REVIEW_ISSUE_TITLE,
            labels=health_review_issue_labels(config),
            health_review_fingerprint=fingerprint,
        )

    def test_creation_records_the_decisions_fingerprint_verbatim(self) -> None:
        # The stamp records what the DECISION carried, never a recompute against
        # the live board — by stamp time the board has gained this very anchor.
        config = _config(interval_minutes=60)
        store = _FakeStore()
        state = _board(blocked=[500])
        decided = health_review_decision(config, state, now=5000.0).fingerprint

        # Mutate the board after the decision to prove the stamp ignores it.
        state.dependency_problems[999] = object()
        record_health_review_creation(
            self._action(config, decided), state, store, now=5000.0
        )

        assert state.last_reviewed_board_fingerprint == decided
        assert store.saved_fingerprints == [decided]
        assert decided != board_review_fingerprint(state, 5000.0)  # board moved on

    def test_hydrate_restores_fingerprint_and_suppresses_refire(self) -> None:
        config = _config(interval_minutes=60)
        board = _board(queue=[7])
        fp = board_review_fingerprint(board, 5000.0)
        store = _FakeStore(stored=5000.0, stored_fingerprint=fp)
        restarted = _board(queue=[7])
        hydrate_last_health_review_at(config, restarted, store, _AnchorTracker([]))
        assert restarted.last_reviewed_board_fingerprint == fp
        # Interval elapsed but the board is unchanged -> no re-walk.
        assert health_review_due(config, restarted, now=5000.0 + 3600) is False

    def test_lost_fingerprint_fails_toward_reviewing(self) -> None:
        config = _config(interval_minutes=60)
        store = _FakeStore(stored=5000.0, stored_fingerprint="")  # persist was lost
        restarted = _board(queue=[7])
        hydrate_last_health_review_at(config, restarted, store, _AnchorTracker([]))
        assert restarted.last_reviewed_board_fingerprint == ""
        assert health_review_due(config, restarted, now=5000.0 + 3600) is True


class TestSessionHungFlag:
    """The aging caveat: a silent session must flip the fingerprint even when
    nothing else on the board moves (#6793)."""

    def test_never_output_session_is_hung_once_it_ages(self) -> None:
        # last_output_at stays None when the session wedges before its first
        # log write (agent CLI failed to spawn). started_at is the fallback, so
        # the MOST severely hung session is not the one case we cannot see.
        state = _board(
            sessions=[_session(7, None, started_at=datetime.fromtimestamp(1000.0))]
        )
        fresh = board_review_fingerprint(state, now=1000.0 + 60)
        hung = board_review_fingerprint(state, now=1000.0 + 31 * 60)
        assert fresh != hung

    def test_same_issue_different_task_is_a_distinct_session(self) -> None:
        # Sessions are keyed by SessionKey, not issue number: a coding session
        # replaced by a review session on the same issue is a real board change.
        coding = _board(sessions=[_session(7, None, task=TaskKind.CODE)])
        review = _board(sessions=[_session(7, None, task=TaskKind.REVIEW)])
        assert board_review_fingerprint(coding, 1000.0) != board_review_fingerprint(
            review, 1000.0
        )


class TestGateSuppressesAcrossRealCreation:
    """The gate's central guarantee, driven through the production intake path
    rather than a hand-stamped fingerprint (#6793).

    Creating the review queues the anchor into ``state.pending_tech_lead_reviews``,
    which is itself part of the board. A stamp that recomputed the fingerprint
    here would record that transient state and never match the settled board
    again — re-firing every interval forever, the exact waste this gate exists
    to prevent.
    """

    def test_static_board_does_not_re_fire_after_a_real_review(self) -> None:
        config = _config(interval_minutes=60)
        store = _FakeStore()
        T0 = 100_000.0

        # A static, non-empty board: blocked issues nobody is fixing.
        state = _board(blocked=[1, 2, 3])
        settled = board_review_fingerprint(state, T0)

        # Interval elapsed, never reviewed -> the backlog review fires.
        decision = health_review_decision(config, state, now=T0)
        assert decision.due is True

        action = CreateTechLeadIssueAction(
            title=HEALTH_REVIEW_ISSUE_TITLE,
            labels=health_review_issue_labels(config),
            health_review_fingerprint=decision.fingerprint,
        )
        intake_created_tech_lead_anchor(action, 900, state, store)
        state.last_health_review_at = T0  # intake stamps wall-clock time.time()

        # The anchor is queued: the board is transiently different...
        assert len(state.pending_tech_lead_reviews) == 1
        # ...but what we recorded is the board we DECIDED on, not that transient.
        assert state.last_reviewed_board_fingerprint == settled

        # The review launches and completes; the board settles back unchanged.
        state.pending_tech_lead_reviews.clear()
        assert board_review_fingerprint(state, T0 + 3600) == settled

        # Nothing changed -> no re-walk. This is the whole point of the gate.
        assert health_review_due(config, state, now=T0 + 3600) is False
        # And it stays suppressed for as long as the board stays put.
        assert health_review_due(config, state, now=T0 + 30 * 3600) is False

    def test_board_change_after_a_real_review_still_fires(self) -> None:
        # The suppressor must not become a silencer: a genuine change still fires.
        config = _config(interval_minutes=60)
        store = _FakeStore()
        T0 = 100_000.0
        state = _board(blocked=[1, 2, 3])

        decision = health_review_decision(config, state, now=T0)
        action = CreateTechLeadIssueAction(
            title=HEALTH_REVIEW_ISSUE_TITLE,
            labels=health_review_issue_labels(config),
            health_review_fingerprint=decision.fingerprint,
        )
        intake_created_tech_lead_anchor(action, 900, state, store)
        state.last_health_review_at = T0
        state.pending_tech_lead_reviews.clear()

        state.dependency_problems[4] = object()  # a new issue got blocked
        assert health_review_due(config, state, now=T0 + 3600) is True

    def test_storm_anchor_without_facts_fails_toward_reviewing(self) -> None:
        # "" means never-reviewed, so the next due review fires rather than
        # being silently suppressed by an anchor we cannot attribute a board to.
        config = _config(interval_minutes=60)
        store = _FakeStore()
        state = _board(blocked=[1])
        action = CreateTechLeadIssueAction(
            title=HEALTH_REVIEW_ISSUE_TITLE,
            labels=health_review_issue_labels(config),
        )
        intake_created_tech_lead_anchor(action, 900, state, store)
        state.last_health_review_at = 100_000.0
        state.pending_tech_lead_reviews.clear()
        assert state.last_reviewed_board_fingerprint == ""
        assert health_review_due(config, state, now=100_000.0 + 3600) is True


class TestPlannerCarriesTheDecidedFingerprint:
    """Producer -> action seam: the planner must put the fingerprint the trigger
    decided on onto the action, or the stamp has nothing truthful to record."""

    class _OpenGate:
        def should_create_health_review(self, paused, *, available_slots) -> bool:
            return True

    def _plan(self, config, facts, storm_problems=()):
        return plan_health_review_issue_creation(
            facts,
            (),
            config,
            workflow=self._OpenGate(),
            available_slots=1,
            paused=False,
            storm_problems=storm_problems,
        )

    def test_interval_anchor_carries_the_fingerprint(self) -> None:
        config = _config(interval_minutes=60)
        facts = TechLeadFacts(
            health_review_due=True, health_review_fingerprint="abc123"
        )
        action = self._plan(config, facts)
        assert action is not None
        assert action.health_review_fingerprint == "abc123"

    def test_storm_anchor_carries_the_fingerprint_too(self) -> None:
        # A storm review walks the board as well, so the periodic gate must
        # count it as reviewed rather than re-walking straight after it.
        config = _config(interval_minutes=60)
        facts = TechLeadFacts(
            health_review_due=False, health_review_fingerprint="storm-board"
        )
        action = self._plan(
            config, facts, storm_problems=(_storm_problem(11),)
        )
        assert action is not None
        assert action.health_review_fingerprint == "storm-board"

    def test_end_to_end_decide_plan_create_suppresses_the_re_walk(self) -> None:
        """The whole chain on one static board: decide -> plan -> intake ->
        stamp -> next tick suppressed. No hand-stamped fingerprints."""
        config = _config(interval_minutes=60)
        store = _FakeStore()
        T0 = 100_000.0
        state = _board(blocked=[1, 2, 3])

        decision = health_review_decision(config, state, now=T0)
        facts = TechLeadFacts(
            health_review_due=decision.due,
            health_review_fingerprint=decision.fingerprint,
        )
        action = self._plan(config, facts)
        assert action is not None

        intake_created_tech_lead_anchor(action, 900, state, store)
        state.last_health_review_at = T0  # intake stamps wall-clock time.time()
        state.pending_tech_lead_reviews.clear()  # review launched and completed

        assert health_review_due(config, state, now=T0 + 3600) is False


class _HealthAnchorRepo:
    """RepositoryHost fake: label AND-filtered list_issues plus get_issue."""

    def __init__(self, issues=()) -> None:
        self._issues = list(issues)
        self.list_calls = 0

    def list_issues(self, labels=None, state="open", limit=100, **kwargs):
        self.list_calls += 1
        wanted = {label.casefold() for label in (labels or [])}
        return [
            issue
            for issue in self._issues
            if (state == "all" or issue.state == state)
            and wanted <= {label.casefold() for label in issue.labels}
        ][:limit]

    def get_issue(self, number):
        return next((i for i in self._issues if i.number == number), None)


class _FakeApplier:
    """SupportsApplyAction fake: records applied actions, returns a canned result."""

    def __init__(self, *, issue_number: int = 777, success: bool = True) -> None:
        self._issue_number = issue_number
        self._success = success
        self.applied: list[CreateTechLeadIssueAction] = []

    def apply(self, action):
        self.applied.append(action)
        if not self._success:
            return ActionResult.fail(action, "creation exploded")
        return ActionResult.ok(action, issue_number=self._issue_number)


def _open_health_anchor(number: int, *, config: Config) -> Issue:
    return Issue(
        number=number,
        title=HEALTH_REVIEW_ISSUE_TITLE,
        labels=list(health_review_issue_labels(config)),
        state="open",
        created_at=_iso(1_000.0),
    )


class TestEnsureOnDemandHealthReviewAnchor:
    """The on-demand trigger: force a review NOW, reusing the timer lifecycle."""

    def test_creates_and_queues_even_when_interval_not_due(self) -> None:
        """The whole point: an operator request bypasses the interval+fingerprint
        debounce, yet the walked board fingerprint is still recorded so the next
        timer tick will not double-fire."""
        config = _config(interval_minutes=60)
        state = _board(queue=[42])  # non-empty board => non-empty fingerprint
        now = 5_000_000.0
        state.last_health_review_at = now  # interval NOT elapsed => not due

        # Precondition: the timer gate would decline this tick.
        assert health_review_decision(config, state, now).due is False
        expected_fp = board_review_fingerprint(state, now)
        assert expected_fp  # non-empty, so we can assert it was recorded

        repo = _HealthAnchorRepo([])  # no open anchor => create path
        applier = _FakeApplier(issue_number=777)
        store = _FakeStore()

        result = ensure_on_demand_health_review_anchor(
            state=state,
            config=config,
            repository_host=repo,
            action_applier=applier,
            queue_cache_store=store,
            tech_lead_authority=None,
            now=now,
        )

        # An anchor was shaped + created through the real apply path...
        assert len(applier.applied) == 1
        action = applier.applied[0]
        assert action.flavor is TechLeadSessionFlavor.HEALTH_REVIEW
        assert HEALTH_REVIEW_MARKER_LABEL in action.labels
        assert action.health_review_fingerprint == expected_fp
        # ...queued as a HEALTH_REVIEW pending item, and returned for launch.
        assert result is not None
        assert result.issue_number == 777
        assert result.flavor is TechLeadSessionFlavor.HEALTH_REVIEW
        assert result in state.pending_tech_lead_reviews
        # ...and the walked fingerprint was stamped (memory + durable store).
        assert state.last_reviewed_board_fingerprint == expected_fp
        assert store.saved_fingerprints == [expected_fp]

    def test_reuses_existing_open_anchor_without_creating(self) -> None:
        config = _config(interval_minutes=60)
        state = _board(queue=[42])
        repo = _HealthAnchorRepo([_open_health_anchor(200, config=config)])
        applier = _FakeApplier()

        result = ensure_on_demand_health_review_anchor(
            state=state,
            config=config,
            repository_host=repo,
            action_applier=applier,
            queue_cache_store=_FakeStore(),
            tech_lead_authority=None,
            now=5_000_000.0,
        )

        assert applier.applied == []  # no new anchor created
        assert result is not None
        assert result.issue_number == 200
        assert result.flavor is TechLeadSessionFlavor.HEALTH_REVIEW
        assert result in state.pending_tech_lead_reviews
        # Recovery of an existing anchor does not stamp (creation time stands).
        assert state.last_reviewed_board_fingerprint == ""

    def test_no_tech_lead_agent_returns_none_without_touching_github(self) -> None:
        config = Config()
        config.tech_lead_review_agent = None
        config.tech_lead.health_review.interval_minutes = 60
        repo = _HealthAnchorRepo([])
        applier = _FakeApplier()

        result = ensure_on_demand_health_review_anchor(
            state=_board(queue=[42]),
            config=config,
            repository_host=repo,
            action_applier=applier,
            queue_cache_store=None,
            tech_lead_authority=None,
            now=1.0,
        )

        assert result is None
        assert repo.list_calls == 0  # guarded before any discovery scan
        assert applier.applied == []

    def test_apply_failure_returns_none_and_does_not_queue(self) -> None:
        config = _config(interval_minutes=60)
        state = _board(queue=[42])
        repo = _HealthAnchorRepo([])
        applier = _FakeApplier(success=False)

        result = ensure_on_demand_health_review_anchor(
            state=state,
            config=config,
            repository_host=repo,
            action_applier=applier,
            queue_cache_store=_FakeStore(),
            tech_lead_authority=None,
            now=5_000_000.0,
        )

        assert result is None
        assert len(applier.applied) == 1  # attempted
        assert state.pending_tech_lead_reviews == []
        assert state.last_reviewed_board_fingerprint == ""
