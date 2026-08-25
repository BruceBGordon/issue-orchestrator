"""Cross-boundary tests for exact-generation issue runtime termination."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.review_exchange_lifecycle import (
    GenerationTerminationPartialFailure,
    terminate_issue_session_generation,
)
from issue_orchestrator.domain.session_key import TaskKind
from issue_orchestrator.domain.tech_lead_session import TechLeadSessionGeneration


def _session(*, task: TaskKind, terminal_id: str, run_id: str):
    return SimpleNamespace(
        issue=SimpleNamespace(number=14),
        key=SimpleNamespace(task=task),
        terminal_id=terminal_id,
        run_assets=SimpleNamespace(run_id=run_id),
    )


def _target(run_id: str = "RUN-14") -> TechLeadSessionGeneration:
    return TechLeadSessionGeneration(
        issue_number=14,
        task_kind=TaskKind.CODE,
        terminal_id="issue-14",
        run_id=run_id,
    )


def _terminate(active_sessions, *, exists=True):
    kill_session = MagicMock()
    publish_recovery = MagicMock()
    result = terminate_issue_session_generation(
        target=_target(),
        reason="test kill",
        active_sessions=active_sessions,
        session_exists=MagicMock(return_value=exists),
        kill_session=kill_session,
        pair_registry=None,
        job_supervisor=None,
        publish_recovery=publish_recovery,
    )
    return result, kill_session, publish_recovery


def test_review_only_target_fails_closed_without_stopping_anything() -> None:
    active = [_session(task=TaskKind.REVIEW, terminal_id="review-99", run_id="RUN-14")]

    result, kill_session, publish_recovery = _terminate(active)

    assert result.termination is None
    assert result.stale_reason is not None
    assert "no active killable session" in result.stale_reason
    kill_session.assert_not_called()
    publish_recovery.abandon_issue.assert_not_called()
    assert len(active) == 1


def test_replacement_generation_fails_closed_without_stopping_it() -> None:
    replacement = _session(
        task=TaskKind.CODE,
        terminal_id="issue-14",
        run_id="RUN-REPLACEMENT",
    )
    active = [replacement]

    result, kill_session, publish_recovery = _terminate(active)

    assert result.termination is None
    assert result.stale_reason is not None and "replacement" in result.stale_reason
    kill_session.assert_not_called()
    publish_recovery.abandon_issue.assert_not_called()
    assert active == [replacement]


def test_exact_generation_is_the_terminal_stopped_and_cleared() -> None:
    observed = _session(task=TaskKind.CODE, terminal_id="issue-14", run_id="RUN-14")
    active = [observed]

    result, kill_session, publish_recovery = _terminate(active)

    assert result.stale_reason is None
    assert result.termination is not None
    assert result.termination.stopped_session_ids == ("issue-14",)
    assert result.termination.cleared_active_session_ids == ("issue-14",)
    kill_session.assert_called_once_with("issue-14")
    publish_recovery.abandon_issue.assert_called_once_with(14)
    assert active == []


def test_pair_failure_attempts_all_hidden_owners_and_keeps_kill_retryable() -> None:
    observed = _session(task=TaskKind.CODE, terminal_id="issue-14", run_id="RUN-14")
    active = [observed]
    pair_registry = MagicMock()
    pair_registry.release.side_effect = RuntimeError("pair registry unavailable")
    job_supervisor = MagicMock()
    job_supervisor.cancel_matching.return_value = ["review-exchange:14:coding-1"]
    publish_recovery = MagicMock()
    kill_session = MagicMock()

    with pytest.raises(RuntimeError, match="pair registry unavailable"):
        terminate_issue_session_generation(
            target=_target(),
            reason="test kill",
            active_sessions=active,
            session_exists=MagicMock(return_value=True),
            kill_session=kill_session,
            pair_registry=pair_registry,
            job_supervisor=job_supervisor,
            publish_recovery=publish_recovery,
        )

    job_supervisor.cancel_matching.assert_called_once()
    publish_recovery.abandon_issue.assert_called_once_with(14)
    kill_session.assert_not_called()
    assert active == [observed]


def test_publish_failure_keeps_terminal_and_active_row_retryable() -> None:
    observed = _session(task=TaskKind.CODE, terminal_id="issue-14", run_id="RUN-14")
    active = [observed]
    pair_registry = MagicMock()
    job_supervisor = MagicMock()
    job_supervisor.cancel_matching.return_value = []
    publish_recovery = MagicMock()
    publish_recovery.abandon_issue.side_effect = RuntimeError("sqlite unavailable")
    kill_session = MagicMock()

    with pytest.raises(RuntimeError, match="sqlite unavailable"):
        terminate_issue_session_generation(
            target=_target(),
            reason="test kill",
            active_sessions=active,
            session_exists=MagicMock(return_value=True),
            kill_session=kill_session,
            pair_registry=pair_registry,
            job_supervisor=job_supervisor,
            publish_recovery=publish_recovery,
        )

    pair_registry.release.assert_called_once_with(14, reason="test kill")
    job_supervisor.cancel_matching.assert_called_once()
    kill_session.assert_not_called()
    assert active == [observed]


def test_terminal_stop_failure_retains_exact_active_row_for_retry() -> None:
    observed = _session(task=TaskKind.CODE, terminal_id="issue-14", run_id="RUN-14")
    active = [observed]
    publish_recovery = MagicMock()
    kill_session = MagicMock(side_effect=RuntimeError("terminal stop failed"))

    with pytest.raises(RuntimeError, match="terminal stop failed"):
        terminate_issue_session_generation(
            target=_target(),
            reason="test kill",
            active_sessions=active,
            session_exists=MagicMock(return_value=True),
            kill_session=kill_session,
            pair_registry=None,
            job_supervisor=None,
            publish_recovery=publish_recovery,
        )

    publish_recovery.abandon_issue.assert_called_once_with(14)
    kill_session.assert_called_once_with("issue-14")
    assert active == [observed]


def test_stop_then_raise_reconciles_exact_active_row_and_surfaces_partial_failure() -> (
    None
):
    observed = _session(task=TaskKind.CODE, terminal_id="issue-14", run_id="RUN-14")
    active = [observed]
    running = True

    def stop_then_raise(_terminal_id: str) -> None:
        nonlocal running
        running = False
        raise RuntimeError("SESSION_STOPPED publish failed")

    with pytest.raises(
        GenerationTerminationPartialFailure,
        match="stopped, but its stop owner raised after commit",
    ) as raised:
        terminate_issue_session_generation(
            target=_target(),
            reason="test kill",
            active_sessions=active,
            session_exists=lambda _terminal_id: running,
            kill_session=stop_then_raise,
            pair_registry=None,
            job_supervisor=None,
            publish_recovery=MagicMock(),
        )

    assert raised.value.target == _target()
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert active == []


def test_multiple_killable_sessions_for_issue_are_ambiguous() -> None:
    active = [
        _session(task=TaskKind.CODE, terminal_id="issue-14", run_id="RUN-14"),
        _session(task=TaskKind.REWORK, terminal_id="rework-14", run_id="RUN-R"),
    ]

    result, kill_session, publish_recovery = _terminate(active)

    assert result.termination is None
    assert result.stale_reason is not None and "ambiguous" in result.stale_reason
    kill_session.assert_not_called()
    publish_recovery.abandon_issue.assert_not_called()
    assert len(active) == 2
