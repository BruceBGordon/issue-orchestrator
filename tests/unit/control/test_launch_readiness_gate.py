"""Every launch flavor defers while the callback endpoint is unresolved.

An agent spawned before the endpoint is published gets a completion
environment with no way to reach the orchestrator: every callback it
makes fails, and the round runner reads the resulting silence as an
unresponsive agent — which is how completed, validated work ended up
stranded (#6913, #6924).

The rule first lived only in ``_check_launch_preconditions``, which
review, retrospective-review and rework launches never reach, so those
three flavors could still launch into the unresolved window (F7-R3).
These tests pin the behaviour per path — that the launcher actually
defers — not merely that the endpoint object reports unready.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.session_launcher import SessionLauncher
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    PendingRetrospectiveReview,
    PendingReview,
    PendingRework,
)
from issue_orchestrator.infra.agent_callback_endpoint import (
    RuntimeAgentCallbackEndpoint,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import NullBoardSnapshotProvider
from tests.agent_phase_scheduler_helpers import host_agent_phase_command_scheduler

DEFER_REASON = "Agent callback endpoint not published yet"


def _launcher(endpoint) -> SessionLauncher:
    return SessionLauncher(
        config=Config(),
        events=MagicMock(),
        repository_host=MagicMock(),
        action_applier=MagicMock(),
        session_manager=MagicMock(),
        worktree_manager=MagicMock(),
        working_copy=MagicMock(),
        command_runner=MagicMock(),
        session_output=MagicMock(),
        manifest_downloader=MagicMock(),
        tech_lead_authority=MagicMock(),
        session_exists_fn=lambda name: False,
        create_session_fn=lambda *args: True,
        get_issue_machine=lambda issue: MagicMock(state="AVAILABLE"),
        get_session_machine=lambda *args: MagicMock(),
        get_review_machine=lambda *args: MagicMock(),
        board_snapshot_provider=NullBoardSnapshotProvider(),
        agent_callback_endpoint=endpoint,
        agent_phase_command_scheduler=host_agent_phase_command_scheduler(),
    )


@pytest.fixture
def unresolved_endpoint() -> RuntimeAgentCallbackEndpoint:
    """Neither published nor declared — the window the gate guards."""
    return RuntimeAgentCallbackEndpoint()


def _pending(flavor: str):
    """Concrete domain values, not mocks.

    Mock-shaped input lets a launcher fail for unrelated reasons and
    still satisfy an assertion about which error string is absent, which
    is what made the earlier version of the "proceeds" test hollow (N5).
    """
    if flavor == "review":
        return PendingReview(
            issue_key=FakeIssueKey(name="4058"),
            pr_number=99,
            pr_url="https://github.com/owner/repo/pull/99",
            branch_name="4058-fix",
            _issue_number=4058,
        )
    if flavor == "retrospective_review":
        return PendingRetrospectiveReview(
            issue_key=FakeIssueKey(name="4058"),
            issue_number=4058,
            issue_title="Already finished",
            agent_label="agent:backend",
            trigger_label="lack-of-review-redo",
        )
    return PendingRework(
        issue_key=FakeIssueKey(name="4058"),
        agent_type="agent:backend",
        issue_number=4058,
        pr_number=99,
    )


@pytest.mark.parametrize(
    "flavor",
    ["review", "retrospective_review", "rework"],
)
def test_flavor_defers_while_endpoint_unresolved(
    unresolved_endpoint: RuntimeAgentCallbackEndpoint, flavor: str
) -> None:
    """Each flavor that bypassed the precondition helper now defers."""
    launcher = _launcher(unresolved_endpoint)
    call = {
        "review": launcher.launch_review_session,
        "retrospective_review": launcher.launch_retrospective_review_session,
        "rework": launcher.launch_rework_session,
    }[flavor]

    result = call(_pending(flavor), [])

    assert result.session is None, f"{flavor} launched with no callback endpoint"
    assert result.success is False
    assert DEFER_REASON in (result.reason or ""), result.reason


@pytest.mark.parametrize(
    "flavor",
    ["review", "retrospective_review", "rework"],
)
def test_flavor_proceeds_past_the_gate_once_resolved(
    unresolved_endpoint: RuntimeAgentCallbackEndpoint, flavor: str
) -> None:
    """The gate must not be a permanent block.

    Asserting only that one error string is absent would pass even if
    the launcher blew up on mock-shaped data, so this pins the *next*
    boundary each flavor reaches: with no reviewer configured, review
    and retrospective-review must refuse for that reason, and rework
    must get past the gate to its own agent-config check.
    """
    unresolved_endpoint.declare_unavailable()
    launcher = _launcher(unresolved_endpoint)
    call = {
        "review": launcher.launch_review_session,
        "retrospective_review": launcher.launch_retrospective_review_session,
        "rework": launcher.launch_rework_session,
    }[flavor]

    result = call(_pending(flavor), [])

    assert DEFER_REASON not in (result.reason or ""), (
        f"{flavor} still blocked on readiness after the endpoint resolved"
    )
    assert result.success is False
    expected = {
        "review": "No code review agent configured",
        "retrospective_review": "No code review agent configured",
        "rework": "No agent config",
    }[flavor]
    assert expected in (result.reason or ""), (
        f"{flavor} did not reach its next boundary; got {result.reason!r}"
    )
