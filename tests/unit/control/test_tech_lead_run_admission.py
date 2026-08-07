"""The scope-conflict matrix for tech-lead runs has exactly one owner (#6994).

Before ``TechLeadRunCoordinator`` there was no such thing as a tech-lead "run":
the reaction model deduped against the pending queue, the workflow owned
paused/capacity, and the on-demand CLI owned nothing — so two paths could admit
the same logical work and nothing modelled the relationship BETWEEN runs.

These tests pin the matrix from issue #6994 verbatim, plus the launch-time
barrier that makes a global run exclusive rather than merely first in line.
Everything is deterministic: no sleeps, no eventually-assertions, no real clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

import pytest

from issue_orchestrator.control.tech_lead_run_admission import (
    TechLeadLaunchGate,
    TechLeadRunCoordinator,
    plan_tech_lead_launch_gate,
    plan_tech_lead_launch_revalidation,
)
from issue_orchestrator.domain.tech_lead_run import (
    BARRIER_GLOBAL_AWAITING_DRAIN,
    BARRIER_GLOBAL_RUN_ACTIVE,
    BARRIER_GLOBAL_RUN_QUEUED,
    REASON_CLAIMED_BY_PEER,
    REASON_ISSUE_CLOSED,
    REASON_ISSUE_NOT_FOUND,
    REASON_NO_LONGER_BLOCKED,
    REASON_NO_TECH_LEAD_AGENT,
    REASON_ORCHESTRATOR_PAUSED,
    REASON_RUN_CLAIM_UNAVAILABLE,
    GlobalBatchReviewScope,
    GlobalHealthReviewScope,
    IssueInvestigationScope,
    TechLeadRunOutcome,
    TechLeadRunRequest,
    TechLeadRunScopeKind,
    TechLeadRunTrigger,
)
from issue_orchestrator.control.tech_lead_run_ownership import TechLeadRunOwnership
from issue_orchestrator.domain.claim import RunClaim, RunClaimAcquisition
from issue_orchestrator.domain.models import (
    DiscoveredFailure,
    OrchestratorState,
    PendingTechLeadReview,
)
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchScope,
    TechLeadSessionFlavor,
)
from issue_orchestrator.events import EventName

TECH_LEAD_AGENT = "agent:tech-lead"
BLOCKING_LABEL = "blocked-failed"


# ---------------------------------------------------------------------------
# Deterministic doubles at the port boundaries the owner actually depends on
# ---------------------------------------------------------------------------


@dataclass
class FakeIssue:
    number: int
    title: str = "Focus issue"
    labels: tuple[str, ...] = (BLOCKING_LABEL,)
    state: str = "open"
    body: str = ""
    milestone: Optional[str] = None


class FakeRepositoryHost:
    def __init__(self, issues: Optional[dict[int, FakeIssue]] = None) -> None:
        self.issues = dict(issues or {})
        self.get_issue_calls: list[int] = []

    def get_issue(self, number: int) -> Optional[FakeIssue]:
        self.get_issue_calls.append(number)
        return self.issues.get(number)


class FakeSession:
    """The minimum an active tech-lead session presents to the coordinator."""

    def __init__(
        self,
        issue_number: int,
        *,
        agent_label: str = TECH_LEAD_AGENT,
        flavor: Optional[TechLeadSessionFlavor] = None,
        lease_id: Optional[str] = None,
    ) -> None:
        self.issue = FakeIssue(issue_number)
        self.agent_label = agent_label
        self.lease_id = lease_id
        self.tech_lead_scope = (
            TechLeadLaunchScope(flavor=flavor) if flavor is not None else None
        )


class FakeAnchorHost:
    """Stands in for the shared health-review anchor lifecycle."""

    def __init__(self, state: OrchestratorState, anchor_number: Optional[int] = 900):
        self._state = state
        self._anchor_number = anchor_number
        self.calls = 0

    def ensure_health_review_anchor(self) -> Optional[PendingTechLeadReview]:
        self.calls += 1
        if self._anchor_number is None:
            return None
        item = PendingTechLeadReview(
            self._anchor_number,
            "Health Review — walk the floor",
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
        )
        self._state.pending_tech_lead_reviews.append(item)
        return item


class RecordingEvents:
    def __init__(self) -> None:
        self.published: list[Any] = []

    def publish(self, event: Any) -> None:
        self.published.append(event)

    def names(self) -> list[str]:
        return [str(getattr(e, "name", "")) for e in self.published]

    def payloads(self) -> list[dict]:
        return [dict(getattr(e, "data", {}) or {}) for e in self.published]


class SharedRunClaimStore:
    """One in-memory compare-and-swap cell per run key, shared by N engines.

    Stands in for the GitHub ref the real store uses. Deterministic and
    ordering-explicit: a test drives two "engines" by interleaving their calls,
    so the race is reproduced exactly rather than raced for.
    """

    def __init__(self, *, unreachable: bool = False) -> None:
        self.holders: dict[str, RunClaim] = {}
        self.unreachable = unreachable
        self.acquired: list[str] = []
        self.released: list[str] = []

    def acquire(self, run_key: str) -> RunClaimAcquisition:
        if self.unreachable:
            return RunClaimAcquisition.unavailable("backing store unreachable")
        holder = self.holders.get(run_key)
        if holder is not None and not holder.is_expired():
            return RunClaimAcquisition.held_by(holder)
        lease_id = f"lease-{run_key}-{len(self.acquired)}"
        self.holders[run_key] = RunClaim(
            lease_id=lease_id,
            claimant=f"engine-{len(self.acquired)}",
            run_key=run_key,
            started_at=datetime(2026, 8, 7, 12, 0, 0),
            # Far-future expiry: lease EXPIRY is exercised explicitly by
            # ``hold(expired=True)``, so it must never depend on the wall clock
            # the suite happens to run at.
            expires_at=datetime(2999, 1, 1),
            priority=1,
        )
        self.acquired.append(run_key)
        return RunClaimAcquisition.acquired(lease_id)

    def renew(self, run_key: str, lease_id: str) -> bool:
        holder = self.holders.get(run_key)
        return holder is not None and holder.lease_id == lease_id

    def release(self, run_key: str, lease_id: str) -> None:
        holder = self.holders.get(run_key)
        if holder is not None and holder.lease_id == lease_id:
            del self.holders[run_key]
            self.released.append(run_key)

    def current(self, run_key: str) -> Optional[RunClaim]:
        return self.holders.get(run_key)

    def hold(self, run_key: str, *, claimant: str, expired: bool = False) -> None:
        """Seed a peer engine's live (or dead) ownership of a run."""
        started = datetime(2026, 8, 7, 12, 0, 0)
        self.holders[run_key] = RunClaim(
            lease_id=f"peer-{run_key}",
            claimant=claimant,
            run_key=run_key,
            started_at=started,
            expires_at=(
                datetime(1999, 1, 1) if expired else datetime(2999, 1, 1)
            ),
            priority=1,
        )


def _ownership(store: SharedRunClaimStore) -> TechLeadRunOwnership:
    return TechLeadRunOwnership(
        store,  # type: ignore[arg-type]
        lease_seconds=900,
        renew_before_expiry_seconds=300,
    )


def _config(agent: Optional[str] = TECH_LEAD_AGENT) -> Any:
    from issue_orchestrator.infra.config import Config

    config = Config()
    config.tech_lead_review_agent = agent
    return config


def _state(**kwargs: Any) -> OrchestratorState:
    state = OrchestratorState()
    for key, value in kwargs.items():
        setattr(state, key, value)
    return state


def _coordinator(
    state: OrchestratorState,
    *,
    config: Any = None,
    repository_host: Optional[FakeRepositoryHost] = None,
    anchor_host: Optional[FakeAnchorHost] = None,
    ownership: Optional[TechLeadRunOwnership] = None,
    events: Optional[RecordingEvents] = None,
) -> TechLeadRunCoordinator:
    config = config or _config()
    return TechLeadRunCoordinator(
        state=state,
        config=config,
        repository_host=repository_host or FakeRepositoryHost(),
        anchor_host=anchor_host or FakeAnchorHost(state),
        ownership=ownership or _ownership(SharedRunClaimStore()),
        is_blocking_any=lambda labels: any(
            str(label).startswith("blocked") for label in labels
        ),
        events=events or RecordingEvents(),  # type: ignore[arg-type]
    )


def _investigation(issue_number: int) -> PendingTechLeadReview:
    return PendingTechLeadReview(
        issue_number,
        f"Investigate #{issue_number}",
        flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
        failure=DiscoveredFailure(
            issue_number=issue_number,
            issue_title=f"Investigate #{issue_number}",
            failure_reason="timed_out",
        ),
    )


def _health_review(anchor: int = 900) -> PendingTechLeadReview:
    return PendingTechLeadReview(
        anchor, "Health Review", flavor=TechLeadSessionFlavor.HEALTH_REVIEW
    )


def _issue_request(number: int) -> TechLeadRunRequest:
    return TechLeadRunRequest(
        scope=IssueInvestigationScope(number), trigger=TechLeadRunTrigger.DASHBOARD
    )


def _global_request(
    trigger: TechLeadRunTrigger = TechLeadRunTrigger.DASHBOARD,
) -> TechLeadRunRequest:
    return TechLeadRunRequest(scope=GlobalHealthReviewScope(), trigger=trigger)


# ---------------------------------------------------------------------------
# The conflict matrix from #6994, row by row
# ---------------------------------------------------------------------------


def test_issue_request_is_admitted_when_nothing_conflicts():
    state = _state()
    repo = FakeRepositoryHost({42: FakeIssue(42)})
    admission = _coordinator(state, repository_host=repo).admit(_issue_request(42))

    assert admission.outcome is TechLeadRunOutcome.QUEUED
    assert admission.run_key == "issue:42"
    assert admission.scope_kind is TechLeadRunScopeKind.ISSUE
    assert admission.issue_number == 42
    assert not admission.behind_global_barrier
    assert [item.issue_number for item in state.pending_tech_lead_reviews] == [42]


@pytest.mark.parametrize("repeat", [2, 3])
def test_repeated_issue_requests_coalesce_onto_one_run(repeat: int):
    state = _state()
    repo = FakeRepositoryHost({42: FakeIssue(42)})
    coordinator = _coordinator(state, repository_host=repo)

    outcomes = [coordinator.admit(_issue_request(42)).outcome for _ in range(repeat)]

    assert outcomes[0] is TechLeadRunOutcome.QUEUED
    assert all(o is TechLeadRunOutcome.ALREADY_QUEUED for o in outcomes[1:])
    assert len(state.pending_tech_lead_reviews) == 1


def test_issue_request_deduplicates_against_a_running_investigation():
    state = _state(
        active_sessions=[
            FakeSession(42, flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION)
        ]
    )
    admission = _coordinator(state).admit(_issue_request(42))

    assert admission.outcome is TechLeadRunOutcome.ALREADY_RUNNING
    assert state.pending_tech_lead_reviews == []


def test_a_different_issue_is_admitted_while_another_investigation_runs():
    state = _state(
        active_sessions=[
            FakeSession(73, flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION)
        ]
    )
    repo = FakeRepositoryHost({42: FakeIssue(42)})
    admission = _coordinator(state, repository_host=repo).admit(_issue_request(42))

    assert admission.outcome is TechLeadRunOutcome.QUEUED
    assert not admission.behind_global_barrier


def test_issue_request_queues_behind_a_queued_global_run():
    state = _state(pending_tech_lead_reviews=[_health_review()])
    repo = FakeRepositoryHost({42: FakeIssue(42)})
    admission = _coordinator(state, repository_host=repo).admit(_issue_request(42))

    assert admission.outcome is TechLeadRunOutcome.QUEUED
    assert admission.behind_global_barrier is True


def test_issue_request_queues_behind_an_active_global_run():
    state = _state(
        active_sessions=[FakeSession(900, flavor=TechLeadSessionFlavor.HEALTH_REVIEW)]
    )
    repo = FakeRepositoryHost({42: FakeIssue(42)})
    admission = _coordinator(state, repository_host=repo).admit(_issue_request(42))

    assert admission.outcome is TechLeadRunOutcome.QUEUED
    assert admission.behind_global_barrier is True


def test_global_request_is_admitted_and_reuses_the_shared_anchor_lifecycle():
    state = _state()
    anchor_host = FakeAnchorHost(state, anchor_number=900)
    admission = _coordinator(state, anchor_host=anchor_host).admit(_global_request())

    assert admission.outcome is TechLeadRunOutcome.QUEUED
    assert admission.run_key == "global:health_review"
    assert admission.scope_kind is TechLeadRunScopeKind.GLOBAL_HEALTH_REVIEW
    assert admission.issue_number == 900
    assert anchor_host.calls == 1


def test_repeated_global_requests_coalesce_onto_one_run():
    state = _state()
    anchor_host = FakeAnchorHost(state, anchor_number=900)
    coordinator = _coordinator(state, anchor_host=anchor_host)

    first = coordinator.admit(_global_request())
    second = coordinator.admit(_global_request())

    assert first.outcome is TechLeadRunOutcome.QUEUED
    assert second.outcome is TechLeadRunOutcome.ALREADY_QUEUED
    # The anchor lifecycle is driven exactly once: a coalesced request must not
    # mint (or re-queue) a second whole-board run.
    assert anchor_host.calls == 1
    assert len(state.pending_tech_lead_reviews) == 1


def test_global_request_deduplicates_against_a_running_health_review():
    """The launched run's stamped scope IS its identity.

    Once the anchor launches, the pending item is gone; what remains is the
    session's ``tech_lead_scope``, which a restart now rebuilds from the
    anchor's marker label (#6994 round 1 F3) rather than losing.
    """
    state = _state(
        active_sessions=[
            FakeSession(900, flavor=TechLeadSessionFlavor.HEALTH_REVIEW)
        ]
    )
    anchor_host = FakeAnchorHost(state)
    admission = _coordinator(state, anchor_host=anchor_host).admit(_global_request())

    assert admission.outcome is TechLeadRunOutcome.ALREADY_RUNNING
    assert admission.issue_number == 900
    assert anchor_host.calls == 0


def test_a_restored_global_run_without_a_stamp_is_still_treated_as_global():
    """Fail toward the barrier, never away from it (#6994 round 1 F3).

    A tech-lead session whose flavor could not be established must not be read
    as issue-scoped: the cost of being wrong that way is targeted work running
    concurrently with an exclusive whole-repository review.
    """
    state = _state(active_sessions=[FakeSession(900, flavor=None)])
    admission = _coordinator(
        state, repository_host=FakeRepositoryHost({42: FakeIssue(42)})
    ).admit(_issue_request(42))

    assert admission.outcome is TechLeadRunOutcome.QUEUED
    assert admission.behind_global_barrier is True


def test_global_request_is_admitted_while_a_targeted_run_is_active():
    """Row: "Global health review | Any other tech-lead run active | Queue once."""
    state = _state(
        active_sessions=[
            FakeSession(73, flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION)
        ]
    )
    anchor_host = FakeAnchorHost(state)
    admission = _coordinator(state, anchor_host=anchor_host).admit(_global_request())

    assert admission.outcome is TechLeadRunOutcome.QUEUED
    assert anchor_host.calls == 1


def test_automatic_and_manual_requests_for_one_issue_produce_one_run():
    state = _state()
    repo = FakeRepositoryHost({42: FakeIssue(42)})
    coordinator = _coordinator(state, repository_host=repo)

    automatic = coordinator.admit(
        TechLeadRunRequest(
            scope=IssueInvestigationScope(42),
            trigger=TechLeadRunTrigger.AUTOMATIC_FAILURE,
            failure=DiscoveredFailure(42, "Focus issue", "failed"),
            title="Focus issue",
        )
    )
    manual = coordinator.admit(_issue_request(42))

    assert automatic.outcome is TechLeadRunOutcome.QUEUED
    assert manual.outcome is TechLeadRunOutcome.ALREADY_QUEUED
    assert len(state.pending_tech_lead_reviews) == 1
    # The automatic path already held its typed context, so admission spent no
    # GitHub read on it (GitHub API discipline).
    assert repo.get_issue_calls == []


# ---------------------------------------------------------------------------
# Eligibility revalidation and engine/config preconditions
# ---------------------------------------------------------------------------


def test_missing_tech_lead_agent_is_reported_not_configured():
    state = _state()
    admission = _coordinator(state, config=_config(agent=None)).admit(
        _issue_request(42)
    )

    assert admission.outcome is TechLeadRunOutcome.NOT_CONFIGURED
    assert admission.reason == REASON_NO_TECH_LEAD_AGENT
    assert state.pending_tech_lead_reviews == []


def test_paused_engine_refuses_rather_than_promising_a_run():
    state = _state(paused=True)
    admission = _coordinator(state).admit(_global_request())

    assert admission.outcome is TechLeadRunOutcome.PAUSED
    assert admission.reason == REASON_ORCHESTRATOR_PAUSED


def test_unknown_issue_is_not_eligible():
    admission = _coordinator(_state(), repository_host=FakeRepositoryHost()).admit(
        _issue_request(42)
    )

    assert admission.outcome is TechLeadRunOutcome.NOT_ELIGIBLE
    assert admission.reason == REASON_ISSUE_NOT_FOUND


def test_closed_issue_is_rejected_at_revalidation():
    repo = FakeRepositoryHost({42: FakeIssue(42, state="closed")})
    admission = _coordinator(_state(), repository_host=repo).admit(_issue_request(42))

    assert admission.outcome is TechLeadRunOutcome.NOT_ELIGIBLE
    assert admission.reason == REASON_ISSUE_CLOSED


def test_no_longer_blocked_issue_is_rejected_at_revalidation():
    repo = FakeRepositoryHost({42: FakeIssue(42, labels=("agent:backend",))})
    state = _state()
    admission = _coordinator(state, repository_host=repo).admit(_issue_request(42))

    assert admission.outcome is TechLeadRunOutcome.NOT_ELIGIBLE
    assert admission.reason == REASON_NO_LONGER_BLOCKED
    assert state.pending_tech_lead_reviews == []


def test_failed_anchor_preparation_is_reported_as_failed():
    state = _state()
    anchor_host = FakeAnchorHost(state, anchor_number=None)
    admission = _coordinator(state, anchor_host=anchor_host).admit(_global_request())

    assert admission.outcome is TechLeadRunOutcome.FAILED


# ---------------------------------------------------------------------------
# Cross-instance coordination
# ---------------------------------------------------------------------------


def _two_engines(store: SharedRunClaimStore, issues: dict[int, FakeIssue]):
    """Two independent engines over ONE shared coordination store.

    Each has its own state and its own ownership bookkeeping — exactly the
    production shape — so an interleaved admission is arbitrated only by the
    shared compare-and-swap, never by anything in-process.
    """
    engines = []
    for _ in range(2):
        state = _state()
        engines.append(
            (
                state,
                _coordinator(
                    state,
                    repository_host=FakeRepositoryHost(dict(issues)),
                    anchor_host=FakeAnchorHost(state),
                    ownership=_ownership(store),
                ),
            )
        )
    return engines


def test_two_engines_interleaving_one_issue_produce_exactly_one_queued_run():
    """The check-then-act gap, reproduced deterministically (#6994 R1 F1).

    Both engines observe "not running, not queued" BEFORE either writes — the
    interleaving that used to make both answer ``queued``. Only the engine that
    wins the shared claim may enqueue; the loser gets a typed conflict.
    """
    store = SharedRunClaimStore()
    (state_a, engine_a), (state_b, engine_b) = _two_engines(store, {42: FakeIssue(42)})

    first = engine_a.admit(_issue_request(42))
    second = engine_b.admit(_issue_request(42))

    assert first.outcome is TechLeadRunOutcome.QUEUED
    assert second.outcome is TechLeadRunOutcome.CLAIM_CONFLICT
    assert second.reason == REASON_CLAIMED_BY_PEER
    assert [item.issue_number for item in state_a.pending_tech_lead_reviews] == [42]
    assert state_b.pending_tech_lead_reviews == []


def test_two_engines_interleaving_a_global_request_create_one_anchor():
    """The scan-then-create gap: ownership is taken BEFORE the anchor exists.

    Previously both engines scanned, both found no open anchor, and both created
    one. The loser must not reach the anchor lifecycle at all.
    """
    store = SharedRunClaimStore()
    (state_a, engine_a), (state_b, engine_b) = _two_engines(store, {})
    anchor_a = FakeAnchorHost(state_a)
    anchor_b = FakeAnchorHost(state_b)
    engine_a = _coordinator(state_a, anchor_host=anchor_a, ownership=_ownership(store))
    engine_b = _coordinator(state_b, anchor_host=anchor_b, ownership=_ownership(store))

    first = engine_a.admit(_global_request())
    second = engine_b.admit(_global_request())

    assert first.outcome is TechLeadRunOutcome.QUEUED
    assert second.outcome is TechLeadRunOutcome.CLAIM_CONFLICT
    assert anchor_a.calls == 1
    assert anchor_b.calls == 0, "the losing engine must not create a second anchor"


def test_a_peer_claim_on_a_different_run_does_not_block_this_one():
    store = SharedRunClaimStore()
    store.hold(IssueInvestigationScope(73).run_key, claimant="other-host")
    state = _state()
    admission = _coordinator(
        state,
        repository_host=FakeRepositoryHost({42: FakeIssue(42)}),
        ownership=_ownership(store),
    ).admit(_issue_request(42))

    assert admission.outcome is TechLeadRunOutcome.QUEUED


def test_a_dead_peers_expired_run_claim_is_recovered_not_respected():
    """Stale-claim recovery: an expired lease means the owner died.

    Leaving the run un-takeable would strand it forever, so an expired holder is
    no holder at all.
    """
    store = SharedRunClaimStore()
    store.hold(IssueInvestigationScope(42).run_key, claimant="dead-host", expired=True)
    state = _state()
    admission = _coordinator(
        state,
        repository_host=FakeRepositoryHost({42: FakeIssue(42)}),
        ownership=_ownership(store),
    ).admit(_issue_request(42))

    assert admission.outcome is TechLeadRunOutcome.QUEUED
    assert [item.issue_number for item in state.pending_tech_lead_reviews] == [42]


def test_an_unreadable_claim_store_fails_closed_rather_than_admitting():
    """Ignorance is not permission (#6994 round 1 F1).

    Admitting when ownership cannot be established is exactly what produces the
    duplicate run this step exists to prevent, and "queued" is an answer the
    operator cannot discover to be false. A typed failure is retryable.
    """
    state = _state()
    admission = _coordinator(
        state,
        repository_host=FakeRepositoryHost({42: FakeIssue(42)}),
        ownership=_ownership(SharedRunClaimStore(unreachable=True)),
    ).admit(_issue_request(42))

    assert admission.outcome is TechLeadRunOutcome.FAILED
    assert admission.reason == REASON_RUN_CLAIM_UNAVAILABLE
    assert state.pending_tech_lead_reviews == []


def test_repeated_requests_reuse_one_claim_instead_of_churning_the_store():
    store = SharedRunClaimStore()
    state = _state()
    coordinator = _coordinator(
        state,
        repository_host=FakeRepositoryHost({42: FakeIssue(42)}),
        ownership=_ownership(store),
    )

    coordinator.admit(_issue_request(42))
    coordinator.admit(_issue_request(42))
    coordinator.admit(_issue_request(42))

    assert store.acquired == [IssueInvestigationScope(42).run_key]


def test_ownership_is_handed_back_when_the_run_leaves_the_queue():
    """Per-tick reconcile releases a run that no longer exists.

    Without it, a peer would have to wait out the whole lease before it could
    investigate the same subject.
    """
    store = SharedRunClaimStore()
    state = _state()
    coordinator = _coordinator(
        state,
        repository_host=FakeRepositoryHost({42: FakeIssue(42)}),
        ownership=_ownership(store),
    )
    coordinator.admit(_issue_request(42))

    state.pending_tech_lead_reviews.clear()
    coordinator.reconcile_ownership()

    assert store.released == [IssueInvestigationScope(42).run_key]
    assert store.current(IssueInvestigationScope(42).run_key) is None


def test_a_run_whose_claim_a_peer_took_is_reported_so_the_tick_withdraws_it():
    """A restarted engine must not launch work a peer now owns.

    The queue survives the restart (it is rebuilt from the recovered run), but
    the lease bookkeeping does not — so reconcile has to re-establish ownership,
    and when a peer holds it the run comes back as LOST.
    """
    store = SharedRunClaimStore()
    state = _state(pending_tech_lead_reviews=[_investigation(42)])
    store.hold(IssueInvestigationScope(42).run_key, claimant="other-host")

    # A fresh coordinator + fresh ownership IS the restarted process.
    restarted = _coordinator(state, ownership=_ownership(store))

    assert restarted.reconcile_ownership() == (IssueInvestigationScope(42).run_key,)


def test_a_run_recovered_after_restart_reclaims_its_shared_ownership():
    """Restart recovery: the queue survives, so ownership must be re-established.

    A restarted engine holds no lease bookkeeping, but it still has the run. If
    the shared claim is free (its own lease expired with the old process), it
    takes it back rather than launching unowned work.
    """
    store = SharedRunClaimStore()
    state = _state(pending_tech_lead_reviews=[_investigation(42)])
    coordinator = _coordinator(state, ownership=_ownership(store))

    assert coordinator.reconcile_ownership() == ()
    assert store.acquired == [IssueInvestigationScope(42).run_key]


def test_a_batch_review_does_not_deduplicate_against_a_queued_health_review():
    """Two global flavors are two identities that serialize (#6994 R1 F2).

    Collapsing them made one operator's request silently disappear into the
    other's run.
    """
    state = _state(pending_tech_lead_reviews=[_health_review()])
    admission = _coordinator(state).admit(
        TechLeadRunRequest(
            scope=GlobalBatchReviewScope(), trigger=TechLeadRunTrigger.DASHBOARD
        )
    )

    assert admission.outcome is not TechLeadRunOutcome.ALREADY_QUEUED
    assert admission.scope_kind is TechLeadRunScopeKind.GLOBAL_BATCH_REVIEW
    assert admission.run_key == "global:batch_review"


def test_a_second_global_flavor_is_reported_as_waiting_behind_the_first():
    batch = PendingTechLeadReview(
        800, "Tech Lead Batch Review", flavor=TechLeadSessionFlavor.BATCH_REVIEW
    )
    state = _state(pending_tech_lead_reviews=[batch])
    admission = _coordinator(state).admit(_global_request())

    assert admission.outcome is TechLeadRunOutcome.QUEUED
    assert admission.behind_global_barrier is True


# ---------------------------------------------------------------------------
# The launch-time global barrier
# ---------------------------------------------------------------------------


def test_targeted_runs_all_launch_when_no_global_run_exists():
    pending = [_investigation(42), _investigation(73)]
    gate = plan_tech_lead_launch_gate(_config(), pending, [])

    assert list(gate.launchable) == pending
    assert gate.held == ()
    assert gate.barrier_reason is None


def test_a_queued_global_run_holds_every_targeted_run_back():
    health = _health_review()
    targeted = _investigation(42)
    gate = plan_tech_lead_launch_gate(_config(), [targeted, health], [])

    assert list(gate.launchable) == [health]
    assert list(gate.held) == [targeted]
    assert gate.barrier_reason == BARRIER_GLOBAL_RUN_QUEUED


def test_a_queued_global_run_waits_for_active_tech_lead_work_to_drain():
    health = _health_review()
    active = [FakeSession(73, flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION)]
    gate = plan_tech_lead_launch_gate(_config(), [health], active)

    assert gate.launchable == ()
    assert list(gate.held) == [health]
    assert gate.barrier_reason == BARRIER_GLOBAL_AWAITING_DRAIN


def test_an_active_global_run_holds_queued_targeted_work_back():
    targeted = _investigation(42)
    active = [FakeSession(900, flavor=TechLeadSessionFlavor.HEALTH_REVIEW)]
    gate = plan_tech_lead_launch_gate(_config(), [targeted], active)

    assert gate.launchable == ()
    assert list(gate.held) == [targeted]
    assert gate.barrier_reason == BARRIER_GLOBAL_RUN_ACTIVE


def test_queued_targeted_work_resumes_once_the_global_run_completes():
    targeted = _investigation(42)
    config = _config()
    active = [FakeSession(900, flavor=TechLeadSessionFlavor.HEALTH_REVIEW)]

    blocked = plan_tech_lead_launch_gate(config, [targeted], active)
    drained = plan_tech_lead_launch_gate(config, [targeted], [])

    assert blocked.launchable == ()
    assert list(drained.launchable) == [targeted]
    assert drained.held == ()


def test_the_launch_gate_never_holds_work_without_naming_the_rule():
    """A held run must always carry the reason it is held — the invariant that
    keeps a queued-but-idle run explicable instead of looking like a stall."""
    with pytest.raises(ValueError):
        TechLeadLaunchGate((), (_investigation(42),), None)
    with pytest.raises(ValueError):
        TechLeadLaunchGate((_investigation(42),), (), BARRIER_GLOBAL_RUN_QUEUED)


# ---------------------------------------------------------------------------
# Launch-time revalidation: admission is not a standing licence to launch
# ---------------------------------------------------------------------------


def _blocking_any(labels) -> bool:
    return any(str(label).startswith("blocked") for label in labels)


def _revalidate(pending, board):
    return plan_tech_lead_launch_revalidation(pending, board, _blocking_any)


def test_a_still_blocked_subject_keeps_its_queued_run():
    queued = _investigation(42)

    result = _revalidate([queued], [FakeIssue(42)])

    assert list(result.still_eligible) == [queued]
    assert result.withdrawn == ()


def test_a_subject_unblocked_while_queued_is_withdrawn_before_launch():
    queued = _investigation(42)

    result = _revalidate([queued], [FakeIssue(42, labels=("agent:backend",))])

    assert result.still_eligible == ()
    assert [w.item for w in result.withdrawn] == [queued]
    assert result.withdrawn[0].reason == REASON_NO_LONGER_BLOCKED


def test_a_subject_closed_while_queued_is_withdrawn_before_launch():
    queued = _investigation(42)

    result = _revalidate([queued], [FakeIssue(42, state="closed")])

    assert result.still_eligible == ()
    assert [w.reason for w in result.withdrawn] == [REASON_ISSUE_CLOSED]


def test_a_subject_absent_from_the_filtered_board_is_not_withdrawn():
    """Absence is not evidence.

    The board is filtered by agent label, milestone, and
    ``filtering.exclude_labels`` — which ``tech_lead.inherit_labels``
    deliberately re-admits for tech-lead work. Withdrawing on absence would
    silently cancel legitimate investigations of every issue the board filter
    happens not to carry.
    """
    queued = _investigation(42)

    result = _revalidate([queued], [FakeIssue(73)])

    assert list(result.still_eligible) == [queued]
    assert result.withdrawn == ()


def test_a_global_run_is_never_withdrawn_by_subject_eligibility():
    """A health-review anchor is not a blocked work item.

    Its anchor issue carries no blocking label, so applying the per-issue rule
    to it would cancel every board health review the moment it was revalidated.
    """
    health = _health_review()

    result = _revalidate([health], [FakeIssue(900, labels=())])

    assert list(result.still_eligible) == [health]
    assert result.withdrawn == ()


def test_revalidation_withdraws_only_the_ineligible_run():
    keep = _investigation(42)
    drop = _investigation(73)

    result = _revalidate(
        [keep, drop], [FakeIssue(42), FakeIssue(73, labels=("agent:backend",))]
    )

    assert list(result.still_eligible) == [keep]
    assert [w.item for w in result.withdrawn] == [drop]


def test_admission_and_revalidation_share_one_eligibility_rule():
    """The same subject must get the same verdict from both entry points.

    They are asked at different times about the same logical run; if they could
    disagree, a run refused at request time could still launch (or the reverse).
    """
    recovered = FakeIssue(42, labels=("agent:backend",))
    admission = _coordinator(
        _state(), repository_host=FakeRepositoryHost({42: recovered})
    ).admit(_issue_request(42))

    revalidated = _revalidate([_investigation(42)], [recovered])

    assert admission.outcome is TechLeadRunOutcome.NOT_ELIGIBLE
    assert admission.reason == REASON_NO_LONGER_BLOCKED
    assert [w.reason for w in revalidated.withdrawn] == [admission.reason]


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


def test_every_admission_decision_emits_one_canonical_event():
    events = RecordingEvents()
    state = _state()
    repo = FakeRepositoryHost({42: FakeIssue(42)})
    coordinator = _coordinator(state, repository_host=repo, events=events)

    coordinator.admit(_issue_request(42))
    coordinator.admit(_issue_request(42))

    assert events.names() == [
        str(EventName.TECH_LEAD_RUN_REQUESTED),
        str(EventName.TECH_LEAD_RUN_REQUESTED),
    ]
    first, second = events.payloads()
    assert first["run_key"] == "issue:42"
    assert first["scope_kind"] == "issue"
    assert first["issue_number"] == 42
    assert first["trigger"] == "dashboard"
    assert first["outcome"] == "queued"
    assert second["outcome"] == "already_queued"
    # The deferral/deduplication reason is machine-readable, not log-only.
    assert second["reason"]


# ---------------------------------------------------------------------------
# Typed request invariants
# ---------------------------------------------------------------------------


def test_failure_context_without_a_title_is_rejected():
    with pytest.raises(ValueError):
        TechLeadRunRequest(
            scope=IssueInvestigationScope(42),
            trigger=TechLeadRunTrigger.AUTOMATIC_FAILURE,
            failure=DiscoveredFailure(42, "t", "failed"),
        )


def test_a_global_scope_cannot_carry_a_single_triggering_failure():
    with pytest.raises(ValueError):
        TechLeadRunRequest(
            scope=GlobalHealthReviewScope(),
            trigger=TechLeadRunTrigger.AUTOMATIC_FAILURE,
            failure=DiscoveredFailure(42, "t", "failed"),
            title="t",
        )


def test_issue_scope_requires_a_positive_issue_number():
    with pytest.raises(ValueError):
        IssueInvestigationScope(0)
