"""Two queued globals take their turn in ONE order (#6994 round 5 F16/A9).

The local launch gate used to elect ``global_queued[0]`` — whichever global the
repository scan happened to return first — while the shared ledger elected by
its own durable order. Nothing made those agree, and a perfectly ordinary crash
recovery could leave them permanently at odds: the gate offered health, the
ledger insisted batch was ahead, every renewal preserved the disagreement, and
because a queued global is a barrier NOTHING in the repository launched again.

The fix gives both consumers one authority
(:func:`...domain.tech_lead_run.global_run_precedence`), so these tests recover
the queue in the WRONG order on purpose and prove the durable winner still goes
first, the loser goes next, and targeted work is barred only until the globals
drain.

Deterministic throughout: a hand-advanced clock, an explicit shared ledger, and
a tick that is a function call rather than a thread.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from issue_orchestrator.control.tech_lead_run_activity import (
    in_memory_run_activity,
)
from issue_orchestrator.control.tech_lead_launch_authority import (
    TechLeadLaunchAuthority,
)
from issue_orchestrator.control.tech_lead_launch_planning import (
    plan_tech_lead_launch_gate,
)
from issue_orchestrator.control.tech_lead_run_admission import live_run_scopes
from issue_orchestrator.domain.models import (
    DiscoveredFailure,
    OrchestratorState,
    PendingTechLeadReview,
)
from issue_orchestrator.domain.run_ledger import BARRIER_GLOBAL_RUN_QUEUED
from issue_orchestrator.domain.tech_lead_run import (
    GlobalBatchReviewScope,
    GlobalHealthReviewScope,
    IssueInvestigationScope,
    global_run_precedence,
)
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchScope,
    TechLeadSessionFlavor,
)
from issue_orchestrator.infra.config import Config

from .run_ledger_doubles import LEASE_SECONDS, FrozenClock, SharedRunLedger

TECH_LEAD_AGENT = "agent:tech-lead"
HEALTH = GlobalHealthReviewScope()
BATCH = GlobalBatchReviewScope()
FOCUS = IssueInvestigationScope(42)
HEALTH_ANCHOR = 900
BATCH_ANCHOR = 800


class FakeIssue:
    def __init__(self, number: int) -> None:
        self.number = number
        self.title = f"Issue #{number}"
        self.labels = ("blocked-failed",)
        self.state = "open"
        self.body = ""
        self.milestone = None


class FakeSession:
    def __init__(self, issue_number: int, flavor: TechLeadSessionFlavor) -> None:
        self.issue = FakeIssue(issue_number)
        self.agent_label = TECH_LEAD_AGENT
        self.terminal_id = f"tech-lead-{issue_number}"
        self.tech_lead_scope = TechLeadLaunchScope(flavor=flavor)
        # The launch authority opens the run's LOCAL record from these
        # (ADR-0033 / #6858).
        self.started_at = datetime(2026, 8, 9, 12, 0, 0)
        self.run_dir = Path(f"/tmp/run-{issue_number}")
        self.run_assets = SimpleNamespace(
            run_id=f"run-{issue_number}",
            session_name=f"tech-lead-{issue_number}",
        )


def _anchor(number: int, flavor: TechLeadSessionFlavor) -> PendingTechLeadReview:
    return PendingTechLeadReview(number, f"Review #{number}", flavor=flavor)


def _investigation(number: int) -> PendingTechLeadReview:
    return PendingTechLeadReview(
        number,
        f"Investigate #{number}",
        flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
        failure=DiscoveredFailure(
            issue_number=number,
            issue_title=f"Investigate #{number}",
            failure_reason="timed_out",
        ),
    )


class _Engine:
    """One Repository Engine: a queue, a ledger hold, and one tick's worth of work."""

    def __init__(
        self,
        pending: list[PendingTechLeadReview],
        *,
        shared: Optional[SharedRunLedger] = None,
        claimant: str = "engine-a",
    ) -> None:
        self.state = OrchestratorState()
        self.state.pending_tech_lead_reviews = list(pending)
        self.state.active_sessions = []
        self.config = Config()
        self.config.tech_lead_review_agent = TECH_LEAD_AGENT
        self.shared = shared or SharedRunLedger()
        self.ownership = self.shared.ownership(claimant)
        self.issues = {
            HEALTH_ANCHOR: FakeIssue(HEALTH_ANCHOR),
            BATCH_ANCHOR: FakeIssue(BATCH_ANCHOR),
            FOCUS.issue_number: FakeIssue(FOCUS.issue_number),
        }
        self.started: list[str] = []

    # -- one tick -------------------------------------------------------
    def reconcile(self) -> None:
        self.ownership.reconcile(
            live_run_scopes(
                self.config,
                self.state.pending_tech_lead_reviews,
                self.state.active_sessions,
            )
        )

    def tick(self) -> Optional[PendingTechLeadReview]:
        """Reconcile, gate, and try to launch — the planner's real sequence.

        Only the FIRST launchable item is attempted, exactly as the reserved
        tech-lead budget of one slot does in production. That is what makes the
        two-owner disagreement fatal rather than merely wasteful: if the gate
        nominates a run the ledger will refuse, no other run is ever tried.
        """
        self.reconcile()
        gate = plan_tech_lead_launch_gate(
            self.config,
            self.state.pending_tech_lead_reviews,
            self.state.active_sessions,
        )
        if not gate.launchable:
            return None
        candidate = gate.launchable[0]
        return candidate if self._authority().launch(candidate) else None

    def complete(self, number: int) -> None:
        """Finish the running session for ``number`` and hand its hold back."""
        from issue_orchestrator.control.tech_lead_run_admission import (
            scope_of_session,
        )

        for session in list(self.state.active_sessions):
            if session.issue.number != number:
                continue
            self.state.active_sessions.remove(session)
            scope = scope_of_session(session)
            if scope is not None:
                self.ownership.end_run(scope.run_key)

    # -- wiring ---------------------------------------------------------
    def _authority(self) -> TechLeadLaunchAuthority:
        return TechLeadLaunchAuthority(
            state=self.state,
            config=self.config,
            ownership=self.ownership,
            repository_host=SimpleNamespace(  # type: ignore[arg-type]
                get_issue=self.issues.get
            ),
            is_blocking_any=lambda labels: any(
                str(label).startswith("blocked") for label in labels
            ),
            events=SimpleNamespace(publish=lambda _e: None),  # type: ignore[arg-type]
            launch=self._start,
            activity=in_memory_run_activity(),
        )

    def _start(self, tech_lead: PendingTechLeadReview):
        self.state.pending_tech_lead_reviews = [
            item
            for item in self.state.pending_tech_lead_reviews
            if item.issue_number != tech_lead.issue_number
        ]
        session = FakeSession(tech_lead.issue_number, tech_lead.flavor)
        self.state.active_sessions.append(session)
        self.started.append(f"{tech_lead.flavor.value}:{tech_lead.issue_number}")
        return session

    def queued_numbers(self) -> list[int]:
        return sorted(
            item.issue_number for item in self.state.pending_tech_lead_reviews
        )


# ---------------------------------------------------------------------------
# The ordering authority itself
# ---------------------------------------------------------------------------


def test_the_turn_order_is_a_pure_function_of_run_identity():
    """Not of reservation time: two engines' wall clocks are not comparable."""
    assert global_run_precedence(HEALTH.run_key) < global_run_precedence(
        BATCH.run_key
    )


def test_a_targeted_run_takes_no_turn_in_the_global_order():
    try:
        global_run_precedence(FOCUS.run_key)
    except ValueError as exc:
        assert "whole-repository" in str(exc)
    else:  # pragma: no cover - the guard is the point
        raise AssertionError("a targeted run has no place in the global order")


# ---------------------------------------------------------------------------
# The deadlock this replaces
# ---------------------------------------------------------------------------


def test_recovery_in_the_WRONG_order_still_launches_the_durable_winner():
    """The exact F16 sequence, with the recovered queue reversed on purpose.

    The repository scan makes no ordering guarantee, so startup can recover
    ``[batch, health]``. The old gate would have nominated batch while the
    ledger insisted health was ahead, and the pair would have renewed that
    disagreement forever.
    """
    engine = _Engine([_anchor(BATCH_ANCHOR, TechLeadSessionFlavor.BATCH_REVIEW),
                      _anchor(HEALTH_ANCHOR, TechLeadSessionFlavor.HEALTH_REVIEW),
                      _investigation(FOCUS.issue_number)])

    launched = engine.tick()

    assert launched is not None
    assert engine.started == [f"health_review:{HEALTH_ANCHOR}"]
    # The loser is HELD, not lost.
    assert engine.queued_numbers() == sorted([BATCH_ANCHOR, FOCUS.issue_number])


def test_the_second_global_launches_once_the_first_completes():
    engine = _Engine([_anchor(BATCH_ANCHOR, TechLeadSessionFlavor.BATCH_REVIEW),
                      _anchor(HEALTH_ANCHOR, TechLeadSessionFlavor.HEALTH_REVIEW),
                      _investigation(FOCUS.issue_number)])
    engine.tick()

    # A tick while the first global runs starts nothing else.
    assert engine.tick() is None
    assert engine.started == [f"health_review:{HEALTH_ANCHOR}"]

    engine.complete(HEALTH_ANCHOR)
    engine.tick()

    assert engine.started == [
        f"health_review:{HEALTH_ANCHOR}",
        f"batch_review:{BATCH_ANCHOR}",
    ]
    assert engine.queued_numbers() == [FOCUS.issue_number]


def test_targeted_work_is_barred_only_until_the_globals_drain():
    engine = _Engine([_anchor(BATCH_ANCHOR, TechLeadSessionFlavor.BATCH_REVIEW),
                      _anchor(HEALTH_ANCHOR, TechLeadSessionFlavor.HEALTH_REVIEW),
                      _investigation(FOCUS.issue_number)])
    engine.tick()
    engine.complete(HEALTH_ANCHOR)
    engine.tick()
    assert engine.queued_numbers() == [FOCUS.issue_number]

    engine.complete(BATCH_ANCHOR)
    engine.tick()

    assert engine.started[-1] == f"failure_investigation:{FOCUS.issue_number}"
    assert engine.queued_numbers() == []


def test_the_disagreement_cannot_survive_repeated_ticks():
    """Whatever the recovered order, ticking always makes progress."""
    for recovered in (
        [_anchor(BATCH_ANCHOR, TechLeadSessionFlavor.BATCH_REVIEW),
         _anchor(HEALTH_ANCHOR, TechLeadSessionFlavor.HEALTH_REVIEW)],
        [_anchor(HEALTH_ANCHOR, TechLeadSessionFlavor.HEALTH_REVIEW),
         _anchor(BATCH_ANCHOR, TechLeadSessionFlavor.BATCH_REVIEW)],
    ):
        engine = _Engine(recovered)
        engine.tick()
        assert engine.started == [f"health_review:{HEALTH_ANCHOR}"], (
            "the winner must not depend on recovered list order"
        )
        engine.complete(HEALTH_ANCHOR)
        engine.tick()
        assert engine.started[-1] == f"batch_review:{BATCH_ANCHOR}"


def test_an_EXPIRED_ledger_rebuilt_in_run_key_order_still_elects_the_same_winner():
    """Reconciliation re-reserves in run-key order; that must not decide turns.

    Run keys sort ``global:batch_review`` before ``global:health_review``, so a
    rebuild after every hold lapsed reserves batch first. If reservation order
    were the authority, the rebuild alone would flip the winner.
    """
    clock = FrozenClock()
    shared = SharedRunLedger(clock)
    engine = _Engine(
        [_anchor(HEALTH_ANCHOR, TechLeadSessionFlavor.HEALTH_REVIEW),
         _anchor(BATCH_ANCHOR, TechLeadSessionFlavor.BATCH_REVIEW)],
        shared=shared,
    )
    engine.reconcile()
    assert set(shared.live_keys()) == {HEALTH.run_key, BATCH.run_key}

    # Every hold lapses; a fresh engine rebuilds the ledger from its queue.
    clock.advance(LEASE_SECONDS + 1)
    restarted = _Engine(
        [_anchor(BATCH_ANCHOR, TechLeadSessionFlavor.BATCH_REVIEW),
         _anchor(HEALTH_ANCHOR, TechLeadSessionFlavor.HEALTH_REVIEW)],
        shared=shared,
        claimant="engine-b",
    )

    restarted.tick()

    assert restarted.started == [f"health_review:{HEALTH_ANCHOR}"]


def test_a_peer_holding_the_LOSING_global_does_not_block_the_winner():
    """Cross-engine: the same authority on both sides, so no engine stalls."""
    shared = SharedRunLedger()
    peer = shared.ownership("engine-b")
    assert peer.claim(BATCH).owned  # the peer queued the later-turn global

    engine = _Engine(
        [_anchor(HEALTH_ANCHOR, TechLeadSessionFlavor.HEALTH_REVIEW)],
        shared=shared,
    )

    engine.tick()

    assert engine.started == [f"health_review:{HEALTH_ANCHOR}"]
    assert peer.begin_run(BATCH).barrier_reason in {
        BARRIER_GLOBAL_RUN_QUEUED,
        "global_run_awaiting_drain",
    }
