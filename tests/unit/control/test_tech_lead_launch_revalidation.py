"""A queued tech-lead investigation is revalidated before it launches (#6994).

Admission is not a standing licence to launch. A run can sit queued for many
ticks — behind the global barrier, behind capacity, behind an open provider
circuit — and in that window a human can close or unblock its subject. These
tests pin the end-to-end consequence: the planner withdraws such a run instead
of launching it, and the apply seam actually removes it from the queue.

Withdrawal (rather than holding) is the point. The pending queue is a failure
investigation's ONLY durable record, so a run that can never launch would
otherwise sit there forever, keeping the dashboard's "Tech lead queued"
affordance lit on an issue that has nothing left to investigate.
"""

from __future__ import annotations

from pathlib import Path

from issue_orchestrator.control.actions import ActionType, DropTechLeadAction
from issue_orchestrator.control.planner import Planner
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.control.session_manager import SessionType
from issue_orchestrator.control.workflows.tech_lead_workflow import TechLeadWorkflow
from issue_orchestrator.domain.models import (
    AgentConfig,
    DiscoveredFailure,
    Issue,
    OrchestratorState,
    PendingTechLeadReview,
)
from issue_orchestrator.domain.tech_lead_run import (
    REASON_ISSUE_CLOSED,
    REASON_NO_LONGER_BLOCKED,
)
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor
from issue_orchestrator.events import EventName
from issue_orchestrator.infra.config import Config
from tests.unit.test_planner import make_snapshot

TECH_LEAD_AGENT = "agent:tech-lead"


def _planner() -> Planner:
    from tests.unit.test_planner import InMemoryEventSink

    config = Config()
    config.tech_lead_review_agent = TECH_LEAD_AGENT
    config.agents[TECH_LEAD_AGENT] = AgentConfig(
        command="claude", prompt_path=Path("/tmp/tech-lead.md")
    )
    config.max_concurrent_sessions = 4
    return Planner(
        config=config,
        scheduler=Scheduler(config),
        tech_lead_workflow=TechLeadWorkflow(config, InMemoryEventSink()),
    )


def _investigation(number: int) -> PendingTechLeadReview:
    return PendingTechLeadReview(
        issue_number=number,
        title=f"Investigate #{number}",
        flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
        failure=DiscoveredFailure(number, f"Investigate #{number}", "timed_out"),
    )


def _health_review(anchor: int = 900) -> PendingTechLeadReview:
    return PendingTechLeadReview(
        issue_number=anchor,
        title="Health Review",
        flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
    )


def _issue(number: int, labels: list[str], state: str = "open") -> Issue:
    return Issue(
        number=number,
        title=f"Issue #{number}",
        labels=labels,
        state=state,
    )


def _blocked(number: int) -> Issue:
    return _issue(number, ["agent:backend", "blocked-failed"])


def _active_investigation_session(number: int):
    """A running targeted investigation, consuming the only reserved slot.

    Deliberately issue-scoped, not global: a global run would ALSO hold the
    queue back via the scope barrier, which would make a "nothing launched"
    assertion ambiguous about which rule did it.
    """
    from dataclasses import replace

    from issue_orchestrator.domain.tech_lead_session import TechLeadLaunchScope
    from tests.unit.test_planner import make_session

    return replace(
        make_session(_issue(number, [TECH_LEAD_AGENT])),
        agent_label=TECH_LEAD_AGENT,
        tech_lead_scope=TechLeadLaunchScope(
            flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION
        ),
    )


def _tech_lead_launches(plan) -> list[int]:
    return [
        action.number
        for action in plan.actions_of_type(ActionType.LAUNCH_SESSION)
        if getattr(action, "session_type", None) is SessionType.TECH_LEAD
    ]


def _withdrawals(plan) -> list[DropTechLeadAction]:
    return list(plan.actions_of_type(ActionType.DROP_TECH_LEAD))


# ---------------------------------------------------------------------------
# The planner's verdict
# ---------------------------------------------------------------------------


def test_a_still_blocked_subject_launches_and_is_not_withdrawn():
    plan = _planner().plan(
        make_snapshot(issues=[_blocked(42)], pending_tech_lead=[_investigation(42)])
    )

    assert _tech_lead_launches(plan) == [42]
    assert _withdrawals(plan) == []


def test_a_subject_unblocked_while_queued_never_launches():
    plan = _planner().plan(
        make_snapshot(
            issues=[_issue(42, ["agent:backend"])],
            pending_tech_lead=[_investigation(42)],
        )
    )

    assert _tech_lead_launches(plan) == []
    assert [(w.issue_number, w.reason) for w in _withdrawals(plan)] == [
        (42, REASON_NO_LONGER_BLOCKED)
    ]


def test_a_subject_closed_while_queued_never_launches():
    plan = _planner().plan(
        make_snapshot(
            issues=[_issue(42, ["agent:backend", "blocked-failed"], state="closed")],
            pending_tech_lead=[_investigation(42)],
        )
    )

    assert _tech_lead_launches(plan) == []
    assert [w.reason for w in _withdrawals(plan)] == [REASON_ISSUE_CLOSED]


def test_a_withdrawn_run_is_not_reported_as_a_capacity_skip():
    """The skip must name the rule that actually stopped the run.

    "No capacity" would invite raising ``tech_lead.max_concurrent``, which
    cannot release a run whose subject no longer exists.
    """
    plan = _planner().plan(
        make_snapshot(
            issues=[_issue(42, ["agent:backend"])],
            pending_tech_lead=[_investigation(42)],
        )
    )

    reasons = {item.reason for item in plan.skipped if item.item_type == "tech_lead"}
    assert reasons == {REASON_NO_LONGER_BLOCKED}


def test_only_the_ineligible_run_is_withdrawn():
    plan = _planner().plan(
        make_snapshot(
            issues=[_blocked(42), _issue(73, ["agent:backend"])],
            pending_tech_lead=[_investigation(42), _investigation(73)],
        )
    )

    assert _tech_lead_launches(plan) == [42]
    assert [w.issue_number for w in _withdrawals(plan)] == [73]


def test_a_run_held_behind_the_global_barrier_is_still_withdrawn():
    """The barrier is exactly the window this rule exists for.

    A held run does not launch this tick, so without withdrawal it would keep
    waiting for a subject that is already gone — for as long as the global run
    takes.
    """
    plan = _planner().plan(
        make_snapshot(
            issues=[_issue(42, ["agent:backend"])],
            pending_tech_lead=[_investigation(42), _health_review()],
        )
    )

    assert _tech_lead_launches(plan) == [900]
    assert [w.issue_number for w in _withdrawals(plan)] == [42]


def test_a_run_is_withdrawn_even_on_a_tick_with_no_tech_lead_slot():
    """Withdrawal is not a capacity decision.

    With ``tech_lead.max_concurrent: 1`` a single active run leaves zero slots.
    Gating revalidation on a free slot would strand a run whose subject is
    already gone for exactly as long as the other run takes — which is the
    window this rule exists for.
    """
    planner = _planner()
    planner.config.tech_lead.max_concurrent = 1
    active = _active_investigation_session(73)

    plan = planner.plan(
        make_snapshot(
            issues=[_issue(42, ["agent:backend"]), _blocked(73)],
            active_sessions=[active],
            pending_tech_lead=[_investigation(42)],
        )
    )

    assert _tech_lead_launches(plan) == []
    assert [w.issue_number for w in _withdrawals(plan)] == [42]


def test_a_global_run_is_never_withdrawn_by_subject_eligibility():
    """The anchor issue carries no blocking label; the board still needs auditing."""
    plan = _planner().plan(
        make_snapshot(issues=[_issue(900, [])], pending_tech_lead=[_health_review()])
    )

    assert _tech_lead_launches(plan) == [900]
    assert _withdrawals(plan) == []


def test_a_subject_absent_from_the_filtered_board_still_launches():
    """Absence is not evidence: the board is filtered, and tech-lead work
    deliberately inherits labels the board filter excludes."""
    plan = _planner().plan(
        make_snapshot(issues=[_blocked(73)], pending_tech_lead=[_investigation(42)])
    )

    assert _tech_lead_launches(plan) == [42]
    assert _withdrawals(plan) == []


# ---------------------------------------------------------------------------
# The apply seam actually removes the run
# ---------------------------------------------------------------------------


class _RecordingEvents:
    def __init__(self) -> None:
        self.published: list[object] = []

    def publish(self, event: object) -> None:
        self.published.append(event)


def _apply_withdrawal(state: OrchestratorState, action: DropTechLeadAction) -> list:
    from issue_orchestrator.control.tech_lead_run_wiring import (
        withdraw_revalidated_tech_lead_run,
    )

    events = _RecordingEvents()

    class _Tick:
        pass

    tick = _Tick()
    tick.state = state  # type: ignore[attr-defined]
    tick.events = events  # type: ignore[attr-defined]
    withdraw_revalidated_tech_lead_run(action, tick)  # type: ignore[arg-type]
    return events.published


def test_applying_a_withdrawal_removes_the_queued_run():
    state = OrchestratorState()
    state.pending_tech_lead_reviews.extend([_investigation(42), _investigation(73)])

    _apply_withdrawal(
        state,
        DropTechLeadAction(
            issue_number=42, reason=REASON_NO_LONGER_BLOCKED, detail="recovered"
        ),
    )

    assert [i.issue_number for i in state.pending_tech_lead_reviews] == [73]


def test_a_withdrawal_is_published_with_its_machine_readable_reason():
    state = OrchestratorState()
    state.pending_tech_lead_reviews.append(_investigation(42))

    published = _apply_withdrawal(
        state,
        DropTechLeadAction(
            issue_number=42, reason=REASON_ISSUE_CLOSED, detail="Issue #42 is closed."
        ),
    )

    assert [getattr(e, "name", None) for e in published] == [
        EventName.TECH_LEAD_RUN_WITHDRAWN
    ]
    payload = dict(getattr(published[0], "data", {}) or {})
    assert payload["issue_number"] == 42
    assert payload["reason"] == REASON_ISSUE_CLOSED
    assert payload["run_key"] == "issue:42"
