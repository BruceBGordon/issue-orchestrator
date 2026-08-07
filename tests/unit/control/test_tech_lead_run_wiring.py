"""Every trigger path is wired to the SAME admission owner (#6994 round 1 F2).

An owner nothing is routed through is not an owner. These tests pin the wiring
itself — that the reactive apply seam, the periodic/storm anchor intake, and the
composition root each reach the one coordinator with the one cross-instance
run-ownership store — because a path that quietly bypasses it looks perfectly
healthy in isolation and only misbehaves when a second Repository Engine exists.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Optional

from issue_orchestrator.control.actions import (
    CreateTechLeadIssueAction,
    QueueTechLeadAction,
)
from issue_orchestrator.control.health_review_trigger import (
    HEALTH_REVIEW_MARKER_LABEL,
)
from issue_orchestrator.control.tech_lead_run_ownership import TechLeadRunOwnership
from issue_orchestrator.control.tech_lead_run_wiring import (
    admit_planned_tech_lead_investigation,
    intake_owned_tech_lead_anchor,
)
from issue_orchestrator.domain.claim import RunClaim, RunClaimAcquisition
from issue_orchestrator.domain.models import (
    DiscoveredFailure,
    OrchestratorState,
)
from issue_orchestrator.domain.tech_lead_run import (
    GlobalHealthReviewScope,
    IssueInvestigationScope,
    REASON_CLAIMED_BY_PEER,
    TechLeadRunOutcome,
)
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadCreationOrigin,
    TechLeadSessionFlavor,
)
from issue_orchestrator.infra.config import Config

TECH_LEAD_AGENT = "agent:tech-lead"


class _Store:
    """One shared CAS cell per run key, seedable with a peer's ownership."""

    def __init__(self) -> None:
        self.holders: dict[str, RunClaim] = {}

    def hold(self, run_key: str, claimant: str = "peer-engine") -> None:
        self.holders[run_key] = RunClaim(
            lease_id=f"peer-{run_key}",
            claimant=claimant,
            run_key=run_key,
            started_at=datetime(2026, 8, 7, 12, 0, 0),
            expires_at=datetime(2999, 1, 1),
            priority=1,
        )

    def acquire(self, run_key: str) -> RunClaimAcquisition:
        holder = self.holders.get(run_key)
        if holder is not None:
            return RunClaimAcquisition.held_by(holder)
        self.hold(run_key, claimant="this-engine")
        return RunClaimAcquisition.acquired(self.holders[run_key].lease_id)

    def renew(self, run_key: str, lease_id: str) -> bool:
        holder = self.holders.get(run_key)
        return holder is not None and holder.lease_id == lease_id

    def release(self, run_key: str, lease_id: str) -> None:
        holder = self.holders.get(run_key)
        if holder is not None and holder.lease_id == lease_id:
            del self.holders[run_key]

    def current(self, run_key: str) -> Optional[RunClaim]:
        return self.holders.get(run_key)


class _Tick:
    """The apply-seam shape ``TechLeadTickDependencies`` describes."""

    def __init__(self, store: _Store, state: OrchestratorState) -> None:
        config = Config()
        config.tech_lead_review_agent = TECH_LEAD_AGENT
        self.state = state
        self.config = config
        self.repository_host = SimpleNamespace(get_issue=lambda _n: None)
        self.events = SimpleNamespace(publish=lambda _event: None)
        self.action_applier = None
        self.queue_cache_store = None
        self.tech_lead_authority = None
        self.run_ownership = TechLeadRunOwnership(
            store,  # type: ignore[arg-type]
            lease_seconds=900,
            renew_before_expiry_seconds=300,
        )


def _failure(number: int) -> DiscoveredFailure:
    return DiscoveredFailure(
        issue_number=number,
        issue_title=f"Investigate #{number}",
        failure_reason="timed_out",
    )


def _queue_action(number: int) -> QueueTechLeadAction:
    return QueueTechLeadAction(
        issue_number=number,
        title=f"Investigate #{number}",
        failure=_failure(number),
        reason="session failed",
    )


# ---------------------------------------------------------------------------
# The reactive failure path
# ---------------------------------------------------------------------------


def test_the_reactive_path_arbitrates_against_a_peer_engine():
    """The automatic path must not be the one that skips coordination.

    Before round 1 F2 the reactive handler reached the coordinator but was wired
    without any cross-instance claim, so the production path that produces MOST
    investigations performed no peer arbitration at all.
    """
    store = _Store()
    store.hold(IssueInvestigationScope(42).run_key)
    tick = _Tick(store, OrchestratorState())

    admission = admit_planned_tech_lead_investigation(_queue_action(42), tick)

    assert admission.outcome is TechLeadRunOutcome.CLAIM_CONFLICT
    assert admission.reason == REASON_CLAIMED_BY_PEER
    assert tick.state.pending_tech_lead_reviews == []


def test_the_reactive_path_queues_and_owns_the_run_when_uncontested():
    store = _Store()
    tick = _Tick(store, OrchestratorState())

    admission = admit_planned_tech_lead_investigation(_queue_action(42), tick)

    assert admission.outcome is TechLeadRunOutcome.QUEUED
    assert [i.issue_number for i in tick.state.pending_tech_lead_reviews] == [42]
    assert store.current(IssueInvestigationScope(42).run_key) is not None


def test_the_reactive_path_spends_no_github_read_on_its_own_context():
    """The planned action already carries typed failure context."""
    reads: list[int] = []
    store = _Store()
    tick = _Tick(store, OrchestratorState())
    tick.repository_host = SimpleNamespace(
        get_issue=lambda n: reads.append(n) or None  # type: ignore[func-returns-value]
    )

    admit_planned_tech_lead_investigation(_queue_action(42), tick)

    assert reads == []


# ---------------------------------------------------------------------------
# The periodic / storm anchor path
# ---------------------------------------------------------------------------


def _anchor_action(flavor: TechLeadSessionFlavor) -> CreateTechLeadIssueAction:
    return CreateTechLeadIssueAction(
        reason="periodic health review",
        title="Health Review — walk the floor",
        body="",
        labels=(TECH_LEAD_AGENT,)
        + ((HEALTH_REVIEW_MARKER_LABEL,)
           if flavor is TechLeadSessionFlavor.HEALTH_REVIEW else ()),
        flavor=flavor,
        origin=TechLeadCreationOrigin.authors_anchor(),
    )


def test_a_created_health_anchor_is_not_queued_when_a_peer_owns_the_run():
    """The timer path obeys whole-repository exclusivity too."""
    store = _Store()
    store.hold(GlobalHealthReviewScope().run_key)
    tick = _Tick(store, OrchestratorState())

    queued = intake_owned_tech_lead_anchor(
        _anchor_action(TechLeadSessionFlavor.HEALTH_REVIEW), 900, tick
    )

    assert queued is False
    assert tick.state.pending_tech_lead_reviews == []


def test_a_created_health_anchor_is_queued_and_owned_when_uncontested():
    store = _Store()
    tick = _Tick(store, OrchestratorState())

    queued = intake_owned_tech_lead_anchor(
        _anchor_action(TechLeadSessionFlavor.HEALTH_REVIEW), 900, tick
    )

    assert queued is True
    assert [i.issue_number for i in tick.state.pending_tech_lead_reviews] == [900]
    assert store.current(GlobalHealthReviewScope().run_key) is not None


def test_a_batch_anchor_claims_its_OWN_global_identity_not_the_health_one():
    """Health and batch are distinct global runs that serialize (F2).

    A peer running the health review must not silently suppress this engine's
    batch audit, and vice versa.
    """
    store = _Store()
    store.hold(GlobalHealthReviewScope().run_key)
    tick = _Tick(store, OrchestratorState())

    queued = intake_owned_tech_lead_anchor(
        _anchor_action(TechLeadSessionFlavor.BATCH_REVIEW), 800, tick
    )

    assert queued is True
    assert [i.issue_number for i in tick.state.pending_tech_lead_reviews] == [800]
    assert store.current("global:batch_review") is not None


# ---------------------------------------------------------------------------
# Composition root
# ---------------------------------------------------------------------------


def test_the_tick_seam_defaults_to_single_instance_ownership_not_to_none():
    """No branch on "is coordination wired?" anywhere in the apply seam."""
    from issue_orchestrator.control.tech_lead_run_ownership import (
        single_instance_run_ownership,
    )

    ownership = single_instance_run_ownership()

    assert ownership.claim("issue:42").owned is True
    assert ownership.owns("issue:42") is True


def test_claims_disabled_still_yields_a_usable_ownership_owner():
    """"No Nulls": the apply seam never branches on whether claims are wired."""
    from issue_orchestrator.domain.lease_config import LeaseConfig
    from issue_orchestrator.entrypoints.bootstrap import _create_claim_components

    config = Config()
    config.claims.enabled = False
    events = SimpleNamespace(publish=lambda _event: None)

    _gate, _renewer, lease, _manager, ownership = _create_claim_components(
        config, None, events  # type: ignore[arg-type]
    )

    assert isinstance(lease, LeaseConfig)
    assert ownership.claim(GlobalHealthReviewScope().run_key).owned is True


def test_claims_enabled_backs_run_ownership_with_the_shared_ref_store():
    """Both key spaces come from ONE decision, so they cannot disagree."""
    from issue_orchestrator.adapters.github.ref_claim_adapter import (
        GitHubRefRunClaimAdapter,
    )
    from issue_orchestrator.entrypoints.bootstrap import _create_claim_components

    config = Config()
    config.claims.enabled = True
    github = SimpleNamespace(http_client=SimpleNamespace(), add_label=lambda *a: None)
    events = SimpleNamespace(publish=lambda _event: None)

    *_rest, ownership = _create_claim_components(
        config, github, events  # type: ignore[arg-type]
    )

    store = ownership._store  # noqa: SLF001 - composition assertion
    assert isinstance(store, GitHubRefRunClaimAdapter)
