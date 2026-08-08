"""Restart and ownership-loss reconciliation (#6994 round 2 F4).

Round 1 collapsed three different facts into one word. A restarted engine with
no lease bookkeeping re-acquired every recovered run, and ANY refusal — a live
peer, its OWN unexpired pre-crash lease, or a GitHub read that simply failed —
came back as "lost". The consequences were both directions of wrong: a
recovered global anchor was withdrawn and never retried (stranded until the next
restart), while an ACTIVE session whose ownership had genuinely gone to a peer
was left running, because only queue entries were ever removed.

So reconciliation is typed now, and each status has its own consequence:

* ``CONTENDED`` — retain and retry until the hold lapses or is adopted;
* ``LOST`` — withdraw the queued run AND stop the running session;
* ``UNAVAILABLE`` — change nothing; a transport failure is not evidence.

Every case below is deterministic: one shared ledger cell, a hand-advanced
clock, and recorded terminations instead of real ones.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

from issue_orchestrator.control.tech_lead_run_wiring import (
    reconcile_orchestrator_tech_lead_ownership,
)
from issue_orchestrator.domain.models import (
    DiscoveredFailure,
    OrchestratorState,
    PendingTechLeadReview,
)
from issue_orchestrator.domain.tech_lead_run import (
    GlobalHealthReviewScope,
    IssueInvestigationScope,
)
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchScope,
    TechLeadSessionFlavor,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.infra.config import Config

from .run_ledger_doubles import LEASE_SECONDS, FrozenClock, SharedRunLedger

TECH_LEAD_AGENT = "agent:tech-lead"
HEALTH = GlobalHealthReviewScope()
# Past the renewal threshold (lease 900s, renew 300s before expiry) but still
# inside the lease, so reconcile RENEWS rather than re-acquires.
RENEWAL_DUE_SECONDS = LEASE_SECONDS - 200


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
        self.tech_lead_scope = TechLeadLaunchScope(flavor=flavor)


class RecordingEvents:
    def __init__(self) -> None:
        self.published: list[object] = []

    def publish(self, event) -> None:
        self.published.append(event)

    def payloads(self, event_type: EventName) -> list[dict]:
        return [
            dict(getattr(e, "data", {}) or {})
            for e in self.published
            if getattr(e, "event_type", None) is event_type
        ]

    def ownership_statuses(self) -> dict[str, str]:
        return {
            payload["run_key"]: payload["status"]
            for payload in self.payloads(
                EventName.TECH_LEAD_RUN_OWNERSHIP_CHANGED
            )
        }


class FakeOrchestrator:
    """The facade shape ``TechLeadFacadeHost`` describes, and nothing more."""

    def __init__(self, shared: SharedRunLedger, claimant: str = "engine-a") -> None:
        self.state = OrchestratorState()
        self.config = Config()
        self.config.tech_lead_review_agent = TECH_LEAD_AGENT
        self.events = RecordingEvents()
        self.ownership = shared.ownership(claimant)
        self.terminated: list[FakeSession] = []
        self.deps = SimpleNamespace(
            repository_host=SimpleNamespace(get_issue=lambda _n: None),
            run_ownership=self.ownership,
            events=self.events,
        )

    def ensure_health_review_anchor(self) -> Optional[PendingTechLeadReview]:
        return None

    def launch_queued_tech_lead_session(self, tech_lead):  # pragma: no cover
        raise AssertionError("reconciliation must not launch anything")

    def terminate_tech_lead_session(self, session) -> object:
        self.terminated.append(session)
        self.state.active_sessions = [
            s for s in self.state.active_sessions if s is not session
        ]
        return SimpleNamespace(clean=True)


def _health_anchor(anchor: int = 900) -> PendingTechLeadReview:
    return PendingTechLeadReview(
        anchor, "Health Review", flavor=TechLeadSessionFlavor.HEALTH_REVIEW
    )


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


def _peer_takes_over(shared: SharedRunLedger, clock: FrozenClock, scope) -> None:
    """Let this engine's lease lapse and a peer legitimately take the run."""
    clock.advance(LEASE_SECONDS + 1)
    assert shared.ownership("engine-b").claim(scope).owned


# ---------------------------------------------------------------------------
# Restart: contention is retained, not withdrawn
# ---------------------------------------------------------------------------


def test_a_recovered_global_behind_an_unexpired_lease_is_RETAINED_and_retried():
    """The stranding bug, stated as a test.

    Startup requeues the open anchor; a pre-crash lease is still live. Round 1
    read that as loss and deleted the queue entry, so the anchor sat open and
    unqueued until somebody restarted the engine again.
    """
    clock = FrozenClock()
    shared = SharedRunLedger(clock)
    assert shared.ownership("engine-b").claim(HEALTH).owned
    orchestrator = FakeOrchestrator(shared)
    orchestrator.state.pending_tech_lead_reviews = [_health_anchor()]

    reconcile_orchestrator_tech_lead_ownership(orchestrator)

    assert [i.issue_number for i in orchestrator.state.pending_tech_lead_reviews] == [
        900
    ]
    assert orchestrator.events.ownership_statuses() == {HEALTH.run_key: "contended"}


def test_a_retained_run_is_owned_once_the_holders_lease_lapses():
    """"Retry until adoption or lease expiry" has to actually converge."""
    clock = FrozenClock()
    shared = SharedRunLedger(clock)
    assert shared.ownership("engine-b").claim(HEALTH).owned
    orchestrator = FakeOrchestrator(shared)
    orchestrator.state.pending_tech_lead_reviews = [_health_anchor()]
    reconcile_orchestrator_tech_lead_ownership(orchestrator)

    clock.advance(LEASE_SECONDS + 1)
    orchestrator.events.published.clear()
    reconcile_orchestrator_tech_lead_ownership(orchestrator)

    assert orchestrator.events.ownership_statuses() == {}
    assert orchestrator.ownership.owns(HEALTH.run_key)
    assert [i.issue_number for i in orchestrator.state.pending_tech_lead_reviews] == [
        900
    ]


def test_a_restarted_engine_ADOPTS_its_own_unexpired_lease():
    """A fresh process with the same identity re-adopts rather than contends."""
    clock = FrozenClock()
    shared = SharedRunLedger(clock)
    # The pre-crash process held it under this engine's claimant identity.
    assert shared.ownership("engine-a").claim(HEALTH).owned

    restarted = FakeOrchestrator(shared, claimant="engine-a")
    restarted.state.pending_tech_lead_reviews = [_health_anchor()]
    reconcile_orchestrator_tech_lead_ownership(restarted)

    assert restarted.ownership.owns(HEALTH.run_key)
    assert restarted.events.ownership_statuses() == {}


# ---------------------------------------------------------------------------
# Definitive loss: withdraw the queue entry AND stop the session
# ---------------------------------------------------------------------------


def test_a_definitively_lost_queued_run_leaves_the_queue():
    clock = FrozenClock()
    shared = SharedRunLedger(clock)
    orchestrator = FakeOrchestrator(shared)
    orchestrator.state.pending_tech_lead_reviews = [_health_anchor()]
    reconcile_orchestrator_tech_lead_ownership(orchestrator)

    _peer_takes_over(shared, clock, HEALTH)
    orchestrator.events.published.clear()
    reconcile_orchestrator_tech_lead_ownership(orchestrator)

    assert orchestrator.state.pending_tech_lead_reviews == []
    assert orchestrator.events.ownership_statuses() == {HEALTH.run_key: "lost"}


def test_an_ACTIVE_global_session_that_cannot_prove_ownership_is_TERMINATED():
    """Withdrawing a queue entry was never enough: the run already started."""
    clock = FrozenClock()
    shared = SharedRunLedger(clock)
    orchestrator = FakeOrchestrator(shared)
    session = FakeSession(900, TechLeadSessionFlavor.HEALTH_REVIEW)
    orchestrator.state.active_sessions = [session]
    reconcile_orchestrator_tech_lead_ownership(orchestrator)

    _peer_takes_over(shared, clock, HEALTH)
    reconcile_orchestrator_tech_lead_ownership(orchestrator)

    assert orchestrator.terminated == [session]


def test_an_ACTIVE_targeted_session_that_cannot_prove_ownership_is_TERMINATED():
    clock = FrozenClock()
    shared = SharedRunLedger(clock)
    orchestrator = FakeOrchestrator(shared)
    session = FakeSession(42, TechLeadSessionFlavor.FAILURE_INVESTIGATION)
    orchestrator.state.active_sessions = [session]
    reconcile_orchestrator_tech_lead_ownership(orchestrator)

    _peer_takes_over(shared, clock, IssueInvestigationScope(42))
    reconcile_orchestrator_tech_lead_ownership(orchestrator)

    assert orchestrator.terminated == [session]


# ---------------------------------------------------------------------------
# Store outage: not evidence of anything
# ---------------------------------------------------------------------------


def test_a_renewal_store_outage_neither_withdraws_nor_terminates_anything():
    """The unsafe path round 1 reached whenever GitHub hiccupped."""
    clock = FrozenClock()
    shared = SharedRunLedger(clock)
    orchestrator = FakeOrchestrator(shared)
    orchestrator.state.pending_tech_lead_reviews = [_investigation(42)]
    orchestrator.state.active_sessions = [
        FakeSession(900, TechLeadSessionFlavor.HEALTH_REVIEW)
    ]
    reconcile_orchestrator_tech_lead_ownership(orchestrator)

    clock.advance(RENEWAL_DUE_SECONDS)
    shared.unavailable = True
    orchestrator.events.published.clear()
    reconcile_orchestrator_tech_lead_ownership(orchestrator)

    assert [i.issue_number for i in orchestrator.state.pending_tech_lead_reviews] == [
        42
    ]
    assert orchestrator.terminated == []
    assert set(orchestrator.events.ownership_statuses().values()) == {"unavailable"}


def test_an_outage_that_clears_leaves_ownership_intact():
    """The lease is KEPT across the outage, so recovery costs no re-acquisition."""
    clock = FrozenClock()
    shared = SharedRunLedger(clock)
    orchestrator = FakeOrchestrator(shared)
    orchestrator.state.pending_tech_lead_reviews = [_health_anchor()]
    reconcile_orchestrator_tech_lead_ownership(orchestrator)

    clock.advance(RENEWAL_DUE_SECONDS)
    shared.unavailable = True
    reconcile_orchestrator_tech_lead_ownership(orchestrator)
    shared.unavailable = False
    orchestrator.events.published.clear()
    reconcile_orchestrator_tech_lead_ownership(orchestrator)

    assert orchestrator.events.ownership_statuses() == {}
    assert orchestrator.ownership.owns(HEALTH.run_key)


def test_a_run_that_ended_hands_its_hold_back_to_the_shared_ledger():
    """A completed run must not make peers wait out its lease."""
    clock = FrozenClock()
    shared = SharedRunLedger(clock)
    orchestrator = FakeOrchestrator(shared)
    orchestrator.state.pending_tech_lead_reviews = [_health_anchor()]
    reconcile_orchestrator_tech_lead_ownership(orchestrator)
    assert shared.live_keys() == (HEALTH.run_key,)

    orchestrator.state.pending_tech_lead_reviews = []
    reconcile_orchestrator_tech_lead_ownership(orchestrator)

    assert shared.live_keys() == ()
