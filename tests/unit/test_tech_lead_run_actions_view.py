"""The dashboard's tech-lead affordances project live run state (#6994).

The projection is the producer half of the command surface: the route decides
whether a run may start, and this decides what the operator sees before they
click. Both must read the SAME notion of "a global run" — a projection that
disagreed would show an enabled button the server then refuses (or hide one it
would have accepted), which is exactly the drift the run-admission owner exists
to prevent.
"""

from __future__ import annotations

from typing import Any, Optional

from issue_orchestrator.domain.models import (
    DiscoveredFailure,
    OrchestratorState,
    PendingTechLeadReview,
)
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchScope,
    TechLeadSessionFlavor,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.view_models.tech_lead_run_actions import (
    STATUS_IDLE,
    STATUS_QUEUED,
    STATUS_RUNNING,
    TechLeadRunActionsView,
    read_tech_lead_run_actions,
)


def _payload(view: TechLeadRunActionsView) -> dict:
    """The exact serialization ``dashboard_data`` publishes."""
    return view.model_dump(mode="json", by_alias=True)

TECH_LEAD_AGENT = "agent:tech-lead"


class FakeIssue:
    def __init__(self, number: int) -> None:
        self.number = number
        self.title = f"Issue {number}"
        self.labels: tuple[str, ...] = ()


class FakeSession:
    def __init__(
        self,
        issue_number: int,
        *,
        agent_label: str = TECH_LEAD_AGENT,
        flavor: Optional[TechLeadSessionFlavor] = None,
    ) -> None:
        self.issue = FakeIssue(issue_number)
        self.agent_label = agent_label
        self.lease_id = None
        self.tech_lead_scope = (
            TechLeadLaunchScope(flavor=flavor) if flavor is not None else None
        )


def _config(agent: Optional[str] = TECH_LEAD_AGENT) -> Config:
    config = Config()
    config.tech_lead_review_agent = agent
    return config


def _state(**kwargs: Any) -> OrchestratorState:
    state = OrchestratorState()
    for key, value in kwargs.items():
        setattr(state, key, value)
    return state


def _investigation(number: int) -> PendingTechLeadReview:
    return PendingTechLeadReview(
        number,
        f"Investigate #{number}",
        flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
        failure=DiscoveredFailure(number, f"Investigate #{number}", "timed_out"),
    )


def _health_review(anchor: int = 900) -> PendingTechLeadReview:
    return PendingTechLeadReview(
        anchor, "Health Review", flavor=TechLeadSessionFlavor.HEALTH_REVIEW
    )


def test_idle_board_enables_both_actions_with_no_status_text():
    view = read_tech_lead_run_actions(_config(), _state())

    assert view.configured is True
    assert view.paused is False
    assert view.global_status == STATUS_IDLE
    assert view.global_status_label == ""
    assert view.queued_issue_numbers == ()
    assert view.running_issue_numbers == ()
    assert view.global_barrier_active is False


def test_missing_tech_lead_agent_keeps_the_feature_discoverable_but_disabled():
    view = read_tech_lead_run_actions(_config(agent=None), _state())

    assert view.configured is False


def test_paused_engine_is_reported_so_the_ui_does_not_promise_a_run():
    view = read_tech_lead_run_actions(_config(), _state(paused=True))

    assert view.paused is True


def test_a_queued_health_review_reads_as_queued_with_non_colour_text():
    view = read_tech_lead_run_actions(
        _config(), _state(pending_tech_lead_reviews=[_health_review()])
    )

    assert view.global_status == STATUS_QUEUED
    assert view.global_status_label == "Tech lead queued"
    assert view.global_barrier_active is True
    # The anchor is not a board card, so it never shows as a per-issue run.
    assert view.queued_issue_numbers == ()


def test_a_running_health_review_reads_as_running():
    view = read_tech_lead_run_actions(
        _config(),
        _state(
            active_sessions=[
                FakeSession(900, flavor=TechLeadSessionFlavor.HEALTH_REVIEW)
            ]
        ),
    )

    assert view.global_status == STATUS_RUNNING
    assert view.global_status_label == "Tech lead running"
    assert view.global_barrier_active is True
    assert view.running_issue_numbers == ()


def test_targeted_runs_are_reported_per_issue():
    view = read_tech_lead_run_actions(
        _config(),
        _state(
            pending_tech_lead_reviews=[_investigation(42)],
            active_sessions=[
                FakeSession(73, flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION)
            ],
        ),
    )

    assert view.queued_issue_numbers == (42,)
    assert view.running_issue_numbers == (73,)
    assert view.issue_status(42) == STATUS_QUEUED
    assert view.issue_status(73) == STATUS_RUNNING
    assert view.issue_status(7) == STATUS_IDLE
    assert view.global_barrier_active is False


def test_non_tech_lead_sessions_are_not_reported_as_tech_lead_runs():
    view = read_tech_lead_run_actions(
        _config(),
        _state(active_sessions=[FakeSession(42, agent_label="agent:backend")]),
    )

    assert view.running_issue_numbers == ()
    assert view.global_status == STATUS_IDLE


def test_a_missing_engine_projects_the_disabled_empty_state():
    assert read_tech_lead_run_actions(None, None) == TechLeadRunActionsView.empty()


def test_the_payload_is_the_camel_case_shape_the_dashboard_reads():
    view = read_tech_lead_run_actions(
        _config(), _state(pending_tech_lead_reviews=[_investigation(42)])
    )

    assert _payload(view) == {
        "configured": True,
        "paused": False,
        "globalStatus": STATUS_IDLE,
        "globalStatusLabel": "",
        "queuedIssueNumbers": [42],
        "runningIssueNumbers": [],
        "globalBarrierActive": False,
    }


def test_the_dashboard_data_payload_carries_the_projection():
    """The producer -> command-payload half of the boundary.

    ``dashboard_data`` is what the browser reads on load; without this the
    projection could exist server-side and never reach the two actions.
    """
    from issue_orchestrator.view_models.dashboard import DashboardViewModel

    fields = DashboardViewModel.__dataclass_fields__
    assert "tech_lead_runs" in fields

    view = read_tech_lead_run_actions(
        _config(), _state(active_sessions=[
            FakeSession(900, flavor=TechLeadSessionFlavor.HEALTH_REVIEW)
        ])
    )
    payload = _payload(view)
    assert payload["globalStatus"] == STATUS_RUNNING
    assert payload["globalBarrierActive"] is True
