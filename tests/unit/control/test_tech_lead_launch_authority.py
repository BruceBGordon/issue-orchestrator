"""Every tech-lead launch goes through ONE authority (#6994 round 2 F2 / A2).

Round 1 had two launch paths with two different authorities. The tick reached
the launch through the planner, which consulted the scope gate against a
snapshot taken BEFORE that same tick's actions ran; the one-shot CLI called
``launch_tech_lead_session`` directly and consulted nothing at all. Both are
represented here, because "the gate exists" and "the gate cannot be bypassed"
are different claims and only the second one is worth anything.

Everything is deterministic: a hand-advanced clock, an explicit shared ledger,
and a launch callable that RECORDS rather than launches — so "did the session
start?" is a fact, not an inference.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from issue_orchestrator.control.tech_lead_launch_authority import (
    TechLeadLaunchAuthority,
)
from issue_orchestrator.control.tech_lead_run_activity import (
    in_memory_run_activity,
)
from issue_orchestrator.domain.models import (
    DiscoveredFailure,
    OrchestratorState,
    PendingTechLeadReview,
)
from issue_orchestrator.domain.run_ledger import (
    BARRIER_GLOBAL_AWAITING_DRAIN,
    BARRIER_GLOBAL_RUN_ACTIVE,
)
from issue_orchestrator.domain.tech_lead_run import (
    GlobalHealthReviewScope,
    IssueInvestigationScope,
    REASON_ANCHOR_CLOSED,
    REASON_ANCHOR_UNREADABLE,
    REASON_ISSUE_CLOSED,
    REASON_NO_LONGER_BLOCKED,
)
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchScope,
    TechLeadSessionFlavor,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.infra.config import Config

from .run_ledger_doubles import SharedRunLedger

TECH_LEAD_AGENT = "agent:tech-lead"
BLOCKING_LABEL = "blocked-failed"
HEALTH = GlobalHealthReviewScope()


class FakeIssue:
    def __init__(
        self,
        number: int,
        *,
        state: str = "open",
        labels: tuple[str, ...] = (BLOCKING_LABEL,),
    ) -> None:
        self.number = number
        self.title = f"Issue #{number}"
        self.labels = labels
        self.state = state
        self.body = ""
        self.milestone = None


class FakeRepositoryHost:
    def __init__(self, issues: Optional[dict[int, FakeIssue]] = None) -> None:
        self.issues = dict(issues or {})
        self.reads: list[int] = []

    def get_issue(self, number: int) -> Optional[FakeIssue]:
        self.reads.append(number)
        return self.issues.get(number)


class UnreadableRepositoryHost:
    def get_issue(self, number: int):
        raise RuntimeError(f"GitHub is unreachable (#{number})")


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


class FakeSession:
    def __init__(
        self, issue_number: int, flavor: Optional[TechLeadSessionFlavor] = None
    ) -> None:
        self.issue = FakeIssue(issue_number)
        self.agent_label = TECH_LEAD_AGENT
        self.tech_lead_scope = (
            TechLeadLaunchScope(flavor=flavor) if flavor is not None else None
        )
        self.terminal_id = f"tech-lead-{issue_number}"
        self.key = SimpleNamespace(stable_id=lambda: f"tech_lead:{issue_number}")
        # The launch authority opens the run's LOCAL record from these
        # (ADR-0033 / #6858).
        self.started_at = datetime(2026, 8, 9, 12, 0, 0)
        self.run_dir = Path(f"/tmp/run-{issue_number}")
        self.run_assets = SimpleNamespace(
            run_id=f"run-{issue_number}",
            session_name=f"tech-lead-{issue_number}",
        )


def _config() -> Config:
    config = Config()
    config.tech_lead_review_agent = TECH_LEAD_AGENT
    return config


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


def _health_anchor(anchor: int = 900) -> PendingTechLeadReview:
    return PendingTechLeadReview(
        anchor, "Health Review", flavor=TechLeadSessionFlavor.HEALTH_REVIEW
    )


class _Harness:
    """One engine's launch authority over a shared ledger, with a fake launcher."""

    def __init__(
        self,
        *,
        pending: Optional[list[PendingTechLeadReview]] = None,
        active: Optional[list[FakeSession]] = None,
        issues: Optional[dict[int, FakeIssue]] = None,
        shared: Optional[SharedRunLedger] = None,
        repository_host: object = None,
        launch_fails: bool = False,
        claimant: str = "engine-a",
    ) -> None:
        self.state = OrchestratorState()
        self.state.pending_tech_lead_reviews = list(pending or [])
        self.state.active_sessions = list(active or [])
        self.config = _config()
        self.shared = shared or SharedRunLedger()
        self.ownership = self.shared.ownership(claimant)
        self.events = RecordingEvents()
        self.repository_host = (
            repository_host
            if repository_host is not None
            else FakeRepositoryHost(issues or {})
        )
        self.launched: list[PendingTechLeadReview] = []
        self._launch_fails = launch_fails
        self.activity = in_memory_run_activity()

    def _launch(self, tech_lead: PendingTechLeadReview):
        self.launched.append(tech_lead)
        if self._launch_fails:
            return None
        return FakeSession(tech_lead.issue_number, tech_lead.flavor)

    def authority(self) -> TechLeadLaunchAuthority:
        return TechLeadLaunchAuthority(
            state=self.state,
            config=self.config,
            ownership=self.ownership,
            repository_host=self.repository_host,  # type: ignore[arg-type]
            is_blocking_any=lambda labels: any(
                str(label).startswith("blocked") for label in labels
            ),
            events=self.events,  # type: ignore[arg-type]
            launch=self._launch,
            activity=self.activity,
        )

    def launch(self, tech_lead: PendingTechLeadReview):
        return self.authority().launch(tech_lead)

    def held_reasons(self) -> list[str]:
        return [
            payload["reason"]
            for payload in self.events.payloads(EventName.TECH_LEAD_RUN_HELD)
        ]


# ---------------------------------------------------------------------------
# The CLI's two directions (round 2 F2)
# ---------------------------------------------------------------------------


def test_the_direct_launch_path_obeys_a_QUEUED_global_barrier():
    """The CLI's targeted investigation is not exempt from the barrier."""
    investigation = _investigation(42)
    harness = _Harness(
        pending=[investigation, _health_anchor()],
        issues={42: FakeIssue(42)},
    )

    session = harness.launch(investigation)

    assert session is None
    assert harness.launched == [], "no session may start behind a queued global"
    assert harness.held_reasons() == ["global_run_queued"]
    # Held, not withdrawn: it retries on a later tick.
    assert harness.state.pending_tech_lead_reviews == [investigation, _health_anchor()]


def test_the_direct_launch_path_makes_a_GLOBAL_run_wait_for_drain():
    """The CLI's health review waits for active targeted work, as the gate says."""
    anchor = _health_anchor()
    harness = _Harness(
        pending=[anchor],
        active=[FakeSession(42, TechLeadSessionFlavor.FAILURE_INVESTIGATION)],
        issues={900: FakeIssue(900, labels=())},
    )

    session = harness.launch(anchor)

    assert session is None
    assert harness.launched == []
    assert harness.held_reasons() == [BARRIER_GLOBAL_AWAITING_DRAIN]


def test_an_uncontested_run_actually_launches():
    """The gate must not be a wall: the ordinary path still starts a session."""
    investigation = _investigation(42)
    harness = _Harness(pending=[investigation], issues={42: FakeIssue(42)})

    session = harness.launch(investigation)

    assert session is not None
    assert harness.launched == [investigation]
    assert harness.held_reasons() == []


# ---------------------------------------------------------------------------
# The within-tick bypass (round 2 F2, second half)
# ---------------------------------------------------------------------------


def test_a_global_anchor_created_THIS_TICK_stops_an_already_planned_launch():
    """The gate reads LIVE state, not the plan-time snapshot.

    A tick can create and queue a global barrier and then execute a targeted
    launch that was planned from the pre-action snapshot. The authority re-asks
    against the queue as it is at the instant of launch, which is the only way
    that ordering can be safe.
    """
    investigation = _investigation(42)
    harness = _Harness(pending=[investigation], issues={42: FakeIssue(42)})

    # ... the tick's CreateTechLeadIssueAction lands and queues the anchor.
    harness.state.pending_tech_lead_reviews.append(_health_anchor())

    assert harness.launch(investigation) is None
    assert harness.launched == []
    assert harness.held_reasons() == ["global_run_queued"]


def test_a_peers_running_global_stops_this_engines_launch():
    """Local state cannot see a peer's runs; the shared ledger can."""
    shared = SharedRunLedger()
    peer = shared.ownership("engine-b")
    assert peer.claim(HEALTH).owned
    assert peer.begin_run(HEALTH).started

    investigation = _investigation(42)
    harness = _Harness(
        pending=[investigation], issues={42: FakeIssue(42)}, shared=shared
    )

    assert harness.launch(investigation) is None
    assert harness.launched == []
    assert harness.held_reasons() == [BARRIER_GLOBAL_RUN_ACTIVE]


def test_an_unreadable_coordination_store_fails_CLOSED_at_launch():
    """Starting on ignorance is what the ledger exists to prevent."""
    shared = SharedRunLedger()
    shared.unavailable = True
    investigation = _investigation(42)
    harness = _Harness(
        pending=[investigation], issues={42: FakeIssue(42)}, shared=shared
    )

    assert harness.launch(investigation) is None
    assert harness.launched == []
    assert harness.held_reasons() == ["run_claim_unavailable"]


# ---------------------------------------------------------------------------
# Launch-time subject revalidation
# ---------------------------------------------------------------------------


def test_a_subject_closed_while_queued_is_withdrawn_rather_than_launched():
    investigation = _investigation(42)
    harness = _Harness(
        pending=[investigation], issues={42: FakeIssue(42, state="closed")}
    )

    assert harness.launch(investigation) is None
    assert harness.launched == []
    assert harness.state.pending_tech_lead_reviews == []
    withdrawn = harness.events.payloads(EventName.TECH_LEAD_RUN_WITHDRAWN)
    assert [w["reason"] for w in withdrawn] == [REASON_ISSUE_CLOSED]


def test_a_subject_unblocked_while_queued_is_withdrawn_rather_than_launched():
    investigation = _investigation(42)
    harness = _Harness(
        pending=[investigation], issues={42: FakeIssue(42, labels=("enhancement",))}
    )

    assert harness.launch(investigation) is None
    withdrawn = harness.events.payloads(EventName.TECH_LEAD_RUN_WITHDRAWN)
    assert [w["reason"] for w in withdrawn] == [REASON_NO_LONGER_BLOCKED]


def test_an_unreadable_subject_keeps_its_run_rather_than_cancelling_it():
    """Only POSITIVE evidence withdraws; a GitHub outage is not evidence."""
    investigation = _investigation(42)
    harness = _Harness(
        pending=[investigation], repository_host=UnreadableRepositoryHost()
    )

    assert harness.launch(investigation) is not None
    assert harness.launched == [investigation]


def test_a_global_anchor_is_never_subject_to_blocked_label_eligibility():
    """An anchor is not a blocked work item — but it MUST still be open (F9)."""
    anchor = _health_anchor()
    repo = FakeRepositoryHost({900: FakeIssue(900, labels=())})
    harness = _Harness(pending=[anchor], repository_host=repo)

    assert harness.launch(anchor) is not None
    # Read once, to prove the anchor is still open — never for blocked labels.
    assert repo.reads == [900]


# ---------------------------------------------------------------------------
# Compensation
# ---------------------------------------------------------------------------


def test_a_launch_that_produced_no_session_hands_the_exclusive_hold_back():
    """Otherwise every other tech-lead run waits out a lease for nothing."""
    anchor = _health_anchor()
    harness = _Harness(
        pending=[anchor],
        launch_fails=True,
        issues={900: FakeIssue(900, labels=())},
    )

    assert harness.launch(anchor) is None
    assert harness.launched == [anchor]
    assert harness.shared.live_keys() == ()


def test_the_refusal_reason_is_published_not_only_logged():
    """A queued-but-idle run must be machine-readable."""
    investigation = _investigation(42)
    harness = _Harness(
        pending=[investigation, _health_anchor()], issues={42: FakeIssue(42)}
    )

    harness.launch(investigation)

    [payload] = harness.events.payloads(EventName.TECH_LEAD_RUN_HELD)
    assert payload["run_key"] == IssueInvestigationScope(42).run_key
    assert payload["issue_number"] == 42
    assert payload["reason"] == "global_run_queued"
    assert payload["retained"] is True
    assert payload["detail"]


# ---------------------------------------------------------------------------
# Recovered whole-repository runs (#6994 round 2 F9)
#
# Every engine requeues the same open anchor at startup and a contended copy is
# deliberately retained. That is only safe if the loser proves the DURABLE
# anchor is still open before launching — otherwise, the moment the winner
# finishes and releases the hold, the loser starts a second audit.
# ---------------------------------------------------------------------------


def test_a_recovered_anchor_a_peer_already_COMPLETED_is_never_relaunched():
    """The full F9 chain, one beat at a time.

    Both engines recover the same open anchor at startup. Engine B loses the
    contest and RETAINS its copy (round 2 F4). Engine A runs the review,
    completes it — closing the anchor — and releases the hold. Engine B then
    legitimately acquires the now-free run, and must still refuse to launch it.
    """
    shared = SharedRunLedger()
    winner = shared.ownership("engine-a")
    anchor = _health_anchor()
    open_anchor = FakeIssue(900, labels=())
    loser = _Harness(
        pending=[anchor],
        issues={900: open_anchor},
        shared=shared,
        claimant="engine-b",
    )

    # Engine A wins the recovered run and starts it.
    assert winner.claim(HEALTH).owned
    assert winner.begin_run(HEALTH).started

    # Engine B reconciles: contended, so it RETAINS its queued copy.
    reconciliation = loser.ownership.reconcile([HEALTH])
    assert reconciliation.contended == (HEALTH.run_key,)
    assert reconciliation.lost == ()

    # Engine A completes: the anchor is closed and the hold handed back.
    loser.repository_host.issues[900] = FakeIssue(900, state="closed", labels=())
    winner.end_run(HEALTH.run_key)

    # Engine B now acquires the free run — and must still refuse to run it.
    assert loser.ownership.reconcile([HEALTH]).outcomes[0].status.value == "owned"

    assert loser.launch(anchor) is None
    assert loser.launched == [], "a completed whole-repository run must not rerun"
    assert loser.state.pending_tech_lead_reviews == []
    withdrawn = loser.events.payloads(EventName.TECH_LEAD_RUN_WITHDRAWN)
    assert [w["reason"] for w in withdrawn] == [REASON_ANCHOR_CLOSED]
    # ...and the hold goes back, so nothing else waits on a run that is over.
    assert shared.live_keys() == ()


def test_an_UNREADABLE_anchor_holds_the_global_run_rather_than_launching_it():
    """Fail closed: a duplicate whole-repository audit is the expensive mistake."""
    anchor = _health_anchor()
    harness = _Harness(pending=[anchor], repository_host=UnreadableRepositoryHost())

    assert harness.launch(anchor) is None
    assert harness.launched == []
    assert harness.held_reasons() == [REASON_ANCHOR_UNREADABLE]
    # Held, not withdrawn: the anchor may well still be open.
    assert [i.issue_number for i in harness.state.pending_tech_lead_reviews] == [900]


def test_an_ABSENT_anchor_also_holds_rather_than_launching():
    anchor = _health_anchor()
    harness = _Harness(pending=[anchor], issues={})

    assert harness.launch(anchor) is None
    assert harness.held_reasons() == [REASON_ANCHOR_UNREADABLE]
