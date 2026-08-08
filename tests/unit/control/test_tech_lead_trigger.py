"""Tests for the on-demand tech_lead dispatch owner (control/tech_lead_trigger.py)."""

from types import SimpleNamespace

import pytest

from issue_orchestrator.control.tech_lead_trigger import (
    HealthReviewResult,
    InvestigationResult,
    TechLeadOutcomeStatus,
    TechLeadTerminationOutcome,
    run_health_review,
    run_targeted_investigations,
)
from issue_orchestrator.control.tech_lead_run_admission import (
    TechLeadRunCoordinator,
)
from issue_orchestrator.control.tech_lead_run_ownership import TechLeadRunOwnership
from issue_orchestrator.domain.models import PendingTechLeadReview
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.run_ledger_store import (
    SingleInstanceRunLedgerStore,
)


def _clock(values):
    """A ``now`` callable that yields the given values, holding the last."""
    seq = list(values)
    state = {"i": 0}

    def now() -> float:
        i = min(state["i"], len(seq) - 1)
        state["i"] += 1
        return float(seq[i])

    return now


def _issue(number=5980, *, labels=("blocked-failed",)):
    return SimpleNamespace(
        number=number,
        title=f"Issue {number}",
        body="body",
        milestone=None,
        labels=list(labels),
    )


class _Session:
    def __init__(self, stable_id: str) -> None:
        self.key = SimpleNamespace(stable_id=lambda: stable_id)
        self.terminal_id = stable_id.replace(":", "-")


class _State:
    def __init__(self) -> None:
        self.active_sessions: list[_Session] = []
        self.pending_tech_lead_reviews: list = []
        self.paused = False


class _FakeAnchorHost:
    """The shared anchor lifecycle, as the real facade exposes it."""

    def __init__(self, state, anchor_number=900) -> None:
        self._state = state
        self._anchor_number = anchor_number
        self.calls = 0

    def ensure_health_review_anchor(self):
        self.calls += 1
        if self._anchor_number is None:
            return None
        item = PendingTechLeadReview(
            self._anchor_number,
            "Health Review",
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
        )
        self._state.pending_tech_lead_reviews.append(item)
        return item


class _FakeHost:
    """Minimal TechLeadDispatchHost fake: launch adds a session, tick drains it.

    ``request_tech_lead_run`` delegates to a REAL
    :class:`TechLeadRunCoordinator`, exactly as the production facade does: the
    point of #6994 round 1 F2 is that the one-shot CLI is admitted by the same
    owner as the dashboard, and a hand-written stub here would let that
    regress silently.
    """

    def __init__(
        self, *, issue, launch=True, ticks_to_complete=2, termination=None
    ) -> None:
        self.repository_host = SimpleNamespace(get_issue=lambda n: issue)
        self.state = _State()
        self.anchor_host = _FakeAnchorHost(self.state)
        config = Config()
        config.tech_lead_review_agent = "agent:tech-lead"
        self._coordinator = TechLeadRunCoordinator(
            state=self.state,
            config=config,
            repository_host=self.repository_host,
            anchor_host=self.anchor_host,
            ownership=TechLeadRunOwnership(
                SingleInstanceRunLedgerStore(lease_seconds=900),
                lease_seconds=900,
                renew_before_expiry_seconds=300,
            ),
            is_blocking_any=lambda labels: any(
                str(label).startswith("blocked") for label in labels
            ),
            events=SimpleNamespace(publish=lambda _event: None),
        )
        self._launch = launch
        self._ticks_to_complete = ticks_to_complete
        # The typed outcome the facade returns on terminate — defaults to clean,
        # overridden by tests that inject a failed/leaked cleanup.
        self._termination = termination or TechLeadTerminationOutcome()
        self.pause_calls = 0
        self.tick_count = 0
        self.launched: list = []
        self.killed: list[str] = []
        self._session: _Session | None = None

    def pause(self) -> None:
        self.pause_calls += 1

    def request_tech_lead_run(self, request):
        return self._coordinator.admit(request)

    def launch_tech_lead_session(self, tech_lead):
        self.launched.append(tech_lead)
        self.state.pending_tech_lead_reviews = [
            item
            for item in self.state.pending_tech_lead_reviews
            if item.issue_number != tech_lead.issue_number
        ]
        if not self._launch:
            return None
        self._session = _Session(f"tech-lead:{tech_lead.issue_number}")
        self.state.active_sessions.append(self._session)
        return self._session

    def tick(self) -> bool:
        self.tick_count += 1
        if (
            self._session is not None
            and self.tick_count >= self._ticks_to_complete
            and self._session in self.state.active_sessions
        ):
            self.state.active_sessions.remove(self._session)
        return True

    def terminate_tech_lead_session(self, session):
        # Faithful to the real facade (#6824 R7): terminate AND reconcile the
        # session out of active_sessions, returning the injected typed outcome.
        self.killed.append(session.terminal_id)
        self.state.active_sessions = [
            s for s in self.state.active_sessions if s.terminal_id != session.terminal_id
        ]
        return self._termination


def _noop_sleep(_seconds: float) -> None:
    pass


def test_happy_path_launches_and_drives_to_completion() -> None:
    host = _FakeHost(issue=_issue(5980), ticks_to_complete=2)
    results = run_targeted_investigations(
        host, [5980], now=_clock([0, 1, 2, 3, 4]), sleep=_noop_sleep
    )
    assert host.pause_calls == 1  # planner paused exactly once, up front
    assert len(host.launched) == 1
    tech_lead = host.launched[0]
    assert tech_lead.issue_number == 5980
    assert tech_lead.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
    assert tech_lead.failure is not None
    assert tech_lead.failure.issue_number == 5980
    assert host.tick_count >= 2  # ticked until the session drained
    assert results == [
        InvestigationResult(
            5980, status=TechLeadOutcomeStatus.COMPLETED,
            detail="investigation completed for issue #5980",
        )
    ]


def test_issue_not_found_is_not_launched() -> None:
    host = _FakeHost(issue=None)
    results = run_targeted_investigations(
        host, [4242], now=_clock([0]), sleep=_noop_sleep
    )
    assert host.pause_calls == 1  # pause is up-front, before per-issue work
    assert host.launched == []  # never attempted a launch
    assert results[0].launched is False
    assert results[0].completed is False
    assert "issue_not_found" in results[0].detail


def test_launch_declined_reports_not_launched() -> None:
    host = _FakeHost(issue=_issue(5980), launch=False)
    results = run_targeted_investigations(
        host, [5980], now=_clock([0]), sleep=_noop_sleep
    )
    assert len(host.launched) == 1  # attempted
    assert results[0].launched is False
    assert results[0].completed is False
    assert "not started" in results[0].detail
    assert host.tick_count == 0  # no drive loop when launch declined


def test_timeout_when_session_never_completes() -> None:
    # ticks_to_complete huge => the session never drains; the clock jumps past
    # the deadline so the drive loop gives up.
    host = _FakeHost(issue=_issue(5980), ticks_to_complete=10_000)
    # now() calls, in order: observed_at, deadline-calc, check#1, check#2
    results = run_targeted_investigations(
        host, [5980], now=_clock([0, 0, 0, 9_999]), sleep=_noop_sleep, timeout_s=100
    )
    assert results[0].launched is True
    assert results[0].completed is False
    assert "timed out" in results[0].detail
    # F7: the timed-out session is EXPLICITLY terminated (not left dangling for
    # close() to kill), and the result says so.
    assert "terminated" in results[0].detail
    assert host.killed == ["tech-lead-5980"]
    assert host.state.active_sessions == []


def test_an_unblocked_subject_is_refused_by_the_shared_eligibility_rule() -> None:
    """One rule for every trigger, not a manual/automatic split (#6994 R1 F2).

    The CLI used to build its own queue item and launch an investigation of any
    issue an operator named. It now asks the same admission owner the dashboard
    does, so the "open AND still blocked" rule applies here too — a trigger-
    conditional exemption is exactly the cross-path drift that owner exists to
    remove.
    """
    host = _FakeHost(issue=_issue(5980, labels=("agent:backend",)), ticks_to_complete=1)
    results = run_targeted_investigations(
        host, [5980], now=_clock([0, 1, 2]), sleep=_noop_sleep
    )
    assert host.launched == []
    assert results[0].launched is False
    assert "no_longer_blocked" in results[0].detail


def test_blocking_label_prefers_real_blocked_label() -> None:
    host = _FakeHost(
        issue=_issue(5980, labels=("agent:backend", "blocked-failed")),
        ticks_to_complete=1,
    )
    run_targeted_investigations(
        host, [5980], now=_clock([0, 1, 2]), sleep=_noop_sleep
    )
    assert host.launched[0].failure.blocking_label == "blocked-failed"


def _health_anchor(number: int = 200) -> PendingTechLeadReview:
    return PendingTechLeadReview(
        number,
        "Health Review — walk the floor",
        flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
    )


class _FakeHealthHost:
    """Minimal TechLeadDispatchHost fake for the on-demand health-review driver.

    ``ensure_health_review_anchor`` returns a canned queued anchor (or None),
    ``launch_tech_lead_session`` adds a session, and ``tick`` drains it.
    """

    def __init__(
        self, *, anchor=None, launch=True, ticks_to_complete=2, termination=None
    ) -> None:
        self.state = _State()
        self._anchor = anchor
        config = Config()
        config.tech_lead_review_agent = "agent:tech-lead"
        self._coordinator = TechLeadRunCoordinator(
            state=self.state,
            config=config,
            repository_host=SimpleNamespace(get_issue=lambda _n: None),
            anchor_host=self,
            ownership=TechLeadRunOwnership(
                SingleInstanceRunLedgerStore(lease_seconds=900),
                lease_seconds=900,
                renew_before_expiry_seconds=300,
            ),
            is_blocking_any=lambda labels: any(
                str(label).startswith("blocked") for label in labels
            ),
            events=SimpleNamespace(publish=lambda _event: None),
        )
        self._launch = launch
        self._ticks_to_complete = ticks_to_complete
        self._termination = termination or TechLeadTerminationOutcome()
        self.pause_calls = 0
        self.ensure_calls = 0
        self.launched: list = []
        self.killed: list[str] = []
        self.tick_count = 0
        self._session: _Session | None = None

    def pause(self) -> None:
        self.pause_calls += 1

    def request_tech_lead_run(self, request):
        return self._coordinator.admit(request)

    def ensure_health_review_anchor(self):
        self.ensure_calls += 1
        if self._anchor is not None:
            self.state.pending_tech_lead_reviews.append(self._anchor)
        return self._anchor

    def launch_tech_lead_session(self, tech_lead):
        self.launched.append(tech_lead)
        if not self._launch:
            return None
        self._session = _Session(f"tech-lead:{tech_lead.issue_number}")
        self.state.active_sessions.append(self._session)
        return self._session

    def tick(self) -> bool:
        self.tick_count += 1
        if (
            self._session is not None
            and self.tick_count >= self._ticks_to_complete
            and self._session in self.state.active_sessions
        ):
            self.state.active_sessions.remove(self._session)
        return True

    def terminate_tech_lead_session(self, session):
        # Faithful to the real facade (#6824 R7): terminate AND reconcile.
        self.killed.append(session.terminal_id)
        self.state.active_sessions = [
            s for s in self.state.active_sessions if s.terminal_id != session.terminal_id
        ]
        return self._termination


def test_health_review_launches_and_drives_to_completion() -> None:
    host = _FakeHealthHost(anchor=_health_anchor(200), ticks_to_complete=2)
    result = run_health_review(
        host, now=_clock([0, 1, 2, 3, 4]), sleep=_noop_sleep
    )
    assert host.pause_calls == 1  # planner paused up front, before ensuring
    assert host.ensure_calls == 1  # anchor ensured once, after the pause
    assert len(host.launched) == 1
    launched = host.launched[0]
    assert launched.flavor is TechLeadSessionFlavor.HEALTH_REVIEW
    assert launched.issue_number == 200
    assert host.tick_count >= 2  # ticked until the session drained
    assert result == HealthReviewResult(
        200, status=TechLeadOutcomeStatus.COMPLETED,
        detail="health review completed for anchor #200",
    )


def test_health_review_not_launched_when_no_anchor() -> None:
    # ensure_health_review_anchor returns None (e.g. no tech lead agent configured).
    host = _FakeHealthHost(anchor=None)
    result = run_health_review(host, now=_clock([0]), sleep=_noop_sleep)
    assert host.pause_calls == 1  # pause is up-front, before ensuring the anchor
    assert host.launched == []  # never attempted a launch
    assert host.tick_count == 0
    assert result.anchor_issue_number is None
    assert result.launched is False
    assert result.completed is False
    assert "anchor_unavailable" in result.detail


def test_health_review_launch_declined_reports_not_launched() -> None:
    host = _FakeHealthHost(anchor=_health_anchor(200), launch=False)
    result = run_health_review(host, now=_clock([0]), sleep=_noop_sleep)
    assert len(host.launched) == 1  # attempted
    assert result.anchor_issue_number == 200
    assert result.launched is False
    assert result.completed is False
    assert "not started" in result.detail
    assert host.tick_count == 0  # no drive loop when launch declined


def test_health_review_times_out_when_session_never_completes() -> None:
    host = _FakeHealthHost(anchor=_health_anchor(200), ticks_to_complete=10_000)
    # now() calls, in order: ensure, deadline-calc, check#1, check#2
    result = run_health_review(
        host, now=_clock([0, 0, 0, 9_999]), sleep=_noop_sleep, timeout_s=100
    )
    assert result.anchor_issue_number == 200
    assert result.launched is True
    assert result.completed is False
    assert "timed out" in result.detail
    # F7: the timed-out session is explicitly terminated.
    assert "terminated" in result.detail
    assert host.killed == ["tech-lead-200"]
    assert host.state.active_sessions == []


# --- Discriminated status invariant (#6824 R7) -----------------------------


def test_status_derives_launched_and_completed_booleans() -> None:
    # The booleans consumers read are DERIVED from the single status field, so
    # the two can never disagree.
    not_launched = InvestigationResult(
        1, status=TechLeadOutcomeStatus.NOT_LAUNCHED, detail="x"
    )
    assert (not_launched.launched, not_launched.completed) == (False, False)

    completed = InvestigationResult(1, status=TechLeadOutcomeStatus.COMPLETED, detail="x")
    assert completed.launched is True and completed.completed is True

    timed_out = HealthReviewResult(
        1, status=TechLeadOutcomeStatus.TIMED_OUT, detail="x",
        termination=TechLeadTerminationOutcome(),
    )
    assert timed_out.launched is True and timed_out.completed is False


def test_timed_out_without_termination_is_unrepresentable() -> None:
    # The lying "launched-but-incomplete with no termination" state cannot be
    # constructed — __post_init__ fails fast, and the message names the state.
    with pytest.raises(ValueError, match="iff status is TIMED_OUT.*termination=None"):
        InvestigationResult(1, status=TechLeadOutcomeStatus.TIMED_OUT, detail="x")
    with pytest.raises(ValueError, match="iff status is TIMED_OUT.*termination=None"):
        HealthReviewResult(1, status=TechLeadOutcomeStatus.TIMED_OUT, detail="x")


def test_non_timeout_with_stray_termination_is_rejected() -> None:
    # A completed / not-launched outcome must not carry a termination.
    with pytest.raises(ValueError, match="iff status is TIMED_OUT.*termination=present"):
        InvestigationResult(
            1, status=TechLeadOutcomeStatus.COMPLETED, detail="x",
            termination=TechLeadTerminationOutcome(),
        )
    with pytest.raises(ValueError, match="iff status is TIMED_OUT.*termination=present"):
        HealthReviewResult(
            1, status=TechLeadOutcomeStatus.NOT_LAUNCHED, detail="x",
            termination=TechLeadTerminationOutcome(),
        )


def test_unclean_facade_termination_survives_into_investigation_result() -> None:
    # Producer-side: when the facade's terminate reports an INCOMPLETE cleanup
    # (leaked scratch worktree), that unclean outcome must survive the drive loop
    # into the command's InvestigationResult — not be flattened to a clean status.
    leaked = TechLeadTerminationOutcome(
        worktree_removed=False, leaked_worktree="/wt/repo-tech-lead-5980-abc"
    )
    host = _FakeHost(
        issue=_issue(5980), ticks_to_complete=10_000, termination=leaked
    )
    results = run_targeted_investigations(
        host, [5980], now=_clock([0, 0, 0, 9_999]), sleep=_noop_sleep, timeout_s=100
    )
    result = results[0]
    assert result.status is TechLeadOutcomeStatus.TIMED_OUT
    assert result.launched is True and result.completed is False
    assert result.termination is leaked  # the exact facade outcome, untouched
    assert result.termination.clean is False
    assert result.termination.leaked_worktree == "/wt/repo-tech-lead-5980-abc"
    assert "cleanup INCOMPLETE" in result.detail


def test_unclean_facade_termination_survives_into_health_review_result() -> None:
    # Producer-side, health-review counterpart.
    leaked = TechLeadTerminationOutcome(
        terminal_stopped=False, worktree_removed=False,
        leaked_worktree="/wt/repo-tech-lead-200-xyz",
    )
    host = _FakeHealthHost(
        anchor=_health_anchor(200), ticks_to_complete=10_000, termination=leaked
    )
    result = run_health_review(
        host, now=_clock([0, 0, 0, 9_999]), sleep=_noop_sleep, timeout_s=100
    )
    assert result.status is TechLeadOutcomeStatus.TIMED_OUT
    assert result.launched is True and result.completed is False
    assert result.termination is leaked
    assert result.termination.clean is False
    assert result.termination.leaked_worktree == "/wt/repo-tech-lead-200-xyz"
    assert "cleanup INCOMPLETE" in result.detail
