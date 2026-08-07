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
    GlobalHealthReviewScope,
    IssueInvestigationScope,
    TechLeadRunOutcome,
    TechLeadRunRequest,
    TechLeadRunScopeKind,
    TechLeadRunTrigger,
)
from issue_orchestrator.domain.claim import Claim, ClaimFetchError
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


class FakeClaimManager:
    def __init__(self, claim: Optional[Claim] = None, raises: bool = False) -> None:
        self._claim = claim
        self._raises = raises

    def get_current_claim(self, issue_number: int) -> Optional[Claim]:
        if self._raises:
            raise ClaimFetchError("backing store unreachable")
        return self._claim


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
    open_anchor: Optional[int] = None,
    claim_manager: Any = None,
    events: Optional[RecordingEvents] = None,
    now: Optional[datetime] = None,
) -> TechLeadRunCoordinator:
    config = config or _config()
    return TechLeadRunCoordinator(
        state=state,
        config=config,
        repository_host=repository_host or FakeRepositoryHost(),
        anchor_host=anchor_host or FakeAnchorHost(state),
        discover_open_anchor=lambda _host, _config: open_anchor,
        is_blocking_any=lambda labels: any(
            str(label).startswith("blocked") for label in labels
        ),
        events=events or RecordingEvents(),  # type: ignore[arg-type]
        claim_manager=claim_manager,
        now=now,
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
    """Shared GitHub truth, not the in-process queue, is what proves this.

    Once the anchor launches, the pending item is gone — the only thing left
    that a peer (or this process after a restart) can see is the still-open
    marker-labelled anchor issue.
    """
    state = _state(
        active_sessions=[FakeSession(900, flavor=None)]  # restored: no stamped scope
    )
    anchor_host = FakeAnchorHost(state)
    admission = _coordinator(state, anchor_host=anchor_host, open_anchor=900).admit(
        _global_request()
    )

    assert admission.outcome is TechLeadRunOutcome.ALREADY_RUNNING
    assert admission.issue_number == 900
    assert anchor_host.calls == 0


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


def _claim(lease_id: str, claimant: str, *, expired: bool = False) -> Claim:
    base = datetime(2026, 8, 7, 12, 0, 0)
    return Claim(
        lease_id=lease_id,
        claimant=claimant,
        issue_number=42,
        started_at=base,
        expires_at=base - timedelta(minutes=5) if expired else base + timedelta(hours=1),
        priority=1,
    )


def test_a_peer_orchestrators_live_claim_is_a_typed_conflict():
    repo = FakeRepositoryHost({42: FakeIssue(42)})
    state = _state()
    admission = _coordinator(
        state,
        repository_host=repo,
        claim_manager=FakeClaimManager(_claim("peer-lease", "other-host")),
        now=datetime(2026, 8, 7, 12, 30, 0),
    ).admit(_issue_request(42))

    assert admission.outcome is TechLeadRunOutcome.CLAIM_CONFLICT
    assert admission.reason == REASON_CLAIMED_BY_PEER
    assert state.pending_tech_lead_reviews == []


def test_our_own_sessions_claim_is_not_a_conflict():
    repo = FakeRepositoryHost({42: FakeIssue(42)})
    state = _state(
        active_sessions=[
            FakeSession(7, agent_label="agent:backend", lease_id="our-lease")
        ]
    )
    admission = _coordinator(
        state,
        repository_host=repo,
        claim_manager=FakeClaimManager(_claim("our-lease", "this-host")),
        now=datetime(2026, 8, 7, 12, 30, 0),
    ).admit(_issue_request(42))

    assert admission.outcome is TechLeadRunOutcome.QUEUED


def test_an_expired_peer_claim_does_not_block_admission():
    repo = FakeRepositoryHost({42: FakeIssue(42)})
    admission = _coordinator(
        _state(),
        repository_host=repo,
        claim_manager=FakeClaimManager(_claim("stale", "dead-host", expired=True)),
        now=datetime(2026, 8, 7, 12, 30, 0),
    ).admit(_issue_request(42))

    assert admission.outcome is TechLeadRunOutcome.QUEUED


def test_an_unreadable_claim_store_defers_to_the_launch_time_claim_gate():
    """Fail-open HERE, fail-closed at the write boundary.

    Refusing on a transient GitHub blip would make an operator's request fail
    for a reason that is not about their request; the launch path's ``ClaimGate``
    still verifies ownership (fail-closed) before any mutation.
    """
    repo = FakeRepositoryHost({42: FakeIssue(42)})
    admission = _coordinator(
        _state(),
        repository_host=repo,
        claim_manager=FakeClaimManager(raises=True),
    ).admit(_issue_request(42))

    assert admission.outcome is TechLeadRunOutcome.QUEUED


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
