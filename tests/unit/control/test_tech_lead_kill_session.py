"""Tests for the tech_lead kill_hung_session execution owner (#6778)."""

from unittest.mock import MagicMock

from issue_orchestrator.control.actions import KillHungSessionAction
from issue_orchestrator.control.tech_lead_kill_session import (
    KillSessionRunOutcome,
    TechLeadKillSessionExecutor,
    kill_hung_session_stale_reason,
)
from issue_orchestrator.events import EventName


def _action(*, target_session_id: str = "RUN-14") -> KillHungSessionAction:
    return KillHungSessionAction(
        issue_number=14,
        rationale="Session hung for 90 minutes.",
        proposal_id="A3",
        finding_ids=("T1",),
        anchor_issue_number=501,
        proposal_issue_number=501,
        target_session_id=target_session_id,
    )


def _executor(
    *,
    live_session_id: str | None,
    outcome: KillSessionRunOutcome | None = None,
) -> tuple[TechLeadKillSessionExecutor, MagicMock, MagicMock]:
    events = MagicMock()
    run_kill = MagicMock(return_value=outcome or KillSessionRunOutcome(success=True))
    executor = TechLeadKillSessionExecutor(
        events=events,
        active_session_run_id=lambda _n: live_session_id,
        run_kill=run_kill,
    )
    return executor, events, run_kill


def test_stale_reason_matches_the_approved_generation() -> None:
    # Same generation still live -> not stale.
    assert (
        kill_hung_session_stale_reason(
            issue_number=14, active_session_id="RUN-14", approved_session_id="RUN-14"
        )
        is None
    )
    # No live session at all.
    gone = kill_hung_session_stale_reason(
        issue_number=14, active_session_id=None, approved_session_id="RUN-14"
    )
    assert gone is not None and "no active session" in gone
    # A replacement generation is running -> approval does NOT apply (#6779 R1).
    replaced = kill_hung_session_stale_reason(
        issue_number=14, active_session_id="RUN-99", approved_session_id="RUN-14"
    )
    assert replaced is not None and "replacement" in replaced
    # No recorded identity -> refuse to kill an unverified session.
    unverified = kill_hung_session_stale_reason(
        issue_number=14, active_session_id="RUN-99", approved_session_id=""
    )
    assert unverified is not None and "unverified" in unverified


def test_executes_termination_and_publishes_executed_event() -> None:
    executor, events, run_kill = _executor(
        live_session_id="RUN-14",  # matches the approved generation
        outcome=KillSessionRunOutcome(
            success=True, details={"stopped_session_ids": ["issue-14"]}
        ),
    )

    result = executor.apply(_action())

    assert result.success
    run_kill.assert_called_once()
    issue_number, reason = run_kill.call_args[0]
    assert issue_number == 14
    assert "A3" in reason and "#501" in reason
    [event] = [e.args[0] for e in events.publish.call_args_list]
    assert event.name == EventName.TECH_LEAD_ACTION_EXECUTED.value
    assert event.data["proposal_type"] == "kill_hung_session"
    assert event.data["target_number"] == 14
    assert event.data["issue_number"] == 501  # the proposal issue surface
    assert event.data["finding_ids"] == ["T1"]  # R6 provenance
    assert event.data["boundary"] == {"stopped_session_ids": ["issue-14"]}


def test_direct_execute_identifies_authority_source_without_proposal_issue() -> None:
    action = KillHungSessionAction(
        issue_number=14,
        rationale="Session hung for 90 minutes.",
        proposal_id="A3",
        anchor_issue_number=99,
        target_session_id="RUN-14",
    )
    executor, _, run_kill = _executor(live_session_id="RUN-14")

    result = executor.apply(action)

    assert result.success
    assert "direct authority on anchor #99" in run_kill.call_args.args[1]


def test_stale_downgrade_posts_no_mutations() -> None:
    executor, events, run_kill = _executor(live_session_id=None)

    result = executor.apply(_action())

    assert not result.success
    assert result.details["mode"] == "stale_downgrade"
    run_kill.assert_not_called()
    [event] = [e.args[0] for e in events.publish.call_args_list]
    assert event.name == EventName.TECH_LEAD_ACTION_PROPOSED.value
    assert event.data["mode"] == "stale_downgrade"
    assert "no active session" in event.data["stale_reason"]


def test_replacement_session_is_not_killed() -> None:
    """R1 regression: the diagnosed session exited and a NEW one started for
    the same issue before approval. The kill must NOT touch the replacement."""
    executor, events, run_kill = _executor(live_session_id="RUN-REPLACEMENT")

    result = executor.apply(_action(target_session_id="RUN-14"))

    assert not result.success
    assert result.details["mode"] == "stale_downgrade"
    run_kill.assert_not_called()
    [event] = [e.args[0] for e in events.publish.call_args_list]
    assert event.name == EventName.TECH_LEAD_ACTION_PROPOSED.value
    assert "replacement" in event.data["stale_reason"]


def test_termination_owner_failure_fails_loudly() -> None:
    executor, events, run_kill = _executor(
        live_session_id="RUN-14",
        outcome=KillSessionRunOutcome(success=False, error="session manager down"),
    )

    result = executor.apply(_action())

    assert not result.success
    assert "session manager down" in (result.error or "")
    events.publish.assert_not_called()
