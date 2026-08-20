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
        target_terminal_id="issue-14",
        target_session_type="code",
    )


def _executor(
    *,
    outcome: KillSessionRunOutcome | None = None,
) -> tuple[TechLeadKillSessionExecutor, MagicMock, MagicMock]:
    events = MagicMock()
    run_kill = MagicMock(return_value=outcome or KillSessionRunOutcome(success=True))
    executor = TechLeadKillSessionExecutor(
        events=events,
        run_kill=run_kill,
    )
    return executor, events, run_kill


def test_stale_reason_matches_the_approved_generation() -> None:
    # A complete, killable identity is accepted for the atomic boundary.
    assert (
        kill_hung_session_stale_reason(
            issue_number=14,
            target_session_id="RUN-14",
            target_terminal_id="issue-14",
            target_session_type="code",
        )
        is None
    )
    # A partial legacy identity fails closed before the boundary.
    unverified = kill_hung_session_stale_reason(
        issue_number=14,
        target_session_id="RUN-14",
        target_terminal_id="",
        target_session_type="",
    )
    assert unverified is not None and "unverified runtime" in unverified
    review_only = kill_hung_session_stale_reason(
        issue_number=14,
        target_session_id="RUN-14",
        target_terminal_id="review-14",
        target_session_type="review",
    )
    assert review_only is not None and "non-killable" in review_only


def test_executes_termination_and_publishes_executed_event() -> None:
    executor, events, run_kill = _executor(
        outcome=KillSessionRunOutcome(
            success=True, details={"stopped_session_ids": ["issue-14"]}
        ),
    )

    result = executor.apply(_action())

    assert result.success
    run_kill.assert_called_once()
    target, reason = run_kill.call_args[0]
    assert target.issue_number == 14
    assert target.terminal_id == "issue-14"
    assert target.run_id == "RUN-14"
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
        target_terminal_id="issue-14",
        target_session_type="code",
    )
    executor, _, run_kill = _executor()

    result = executor.apply(action)

    assert result.success
    assert "direct authority on anchor #99" in run_kill.call_args.args[1]


def test_stale_downgrade_posts_no_mutations() -> None:
    executor, events, run_kill = _executor(
        outcome=KillSessionRunOutcome(
            success=False,
            stale_reason="issue #14 has no active killable session",
        )
    )

    result = executor.apply(_action())

    assert not result.success
    assert result.details["mode"] == "stale_downgrade"
    run_kill.assert_called_once()
    [event] = [e.args[0] for e in events.publish.call_args_list]
    assert event.name == EventName.TECH_LEAD_ACTION_PROPOSED.value
    assert event.data["mode"] == "stale_downgrade"
    assert "no active killable session" in event.data["stale_reason"]


def test_replacement_session_is_not_killed() -> None:
    """R1 regression: the diagnosed session exited and a NEW one started for
    the same issue before approval. The kill must NOT touch the replacement."""
    executor, events, run_kill = _executor(
        outcome=KillSessionRunOutcome(
            success=False,
            stale_reason="replacement generation started",
        )
    )

    result = executor.apply(_action(target_session_id="RUN-14"))

    assert not result.success
    assert result.details["mode"] == "stale_downgrade"
    run_kill.assert_called_once()
    [event] = [e.args[0] for e in events.publish.call_args_list]
    assert event.name == EventName.TECH_LEAD_ACTION_PROPOSED.value
    assert "replacement" in event.data["stale_reason"]


def test_termination_owner_failure_fails_loudly() -> None:
    executor, events, run_kill = _executor(
        outcome=KillSessionRunOutcome(success=False, error="session manager down"),
    )

    result = executor.apply(_action())

    assert not result.success
    assert "session manager down" in (result.error or "")
    events.publish.assert_not_called()
