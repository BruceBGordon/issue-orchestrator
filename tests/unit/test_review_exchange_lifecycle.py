"""Tests for issue-scoped runtime lifecycle boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.review_exchange_lifecycle import (
    cancel_issue_review_exchange,
    terminate_issue_runtime,
)


class _FakeSessionManager:
    def __init__(self, running: set[str]) -> None:
        self.running = set(running)
        self.stopped: list[str] = []

    def exists(self, ref) -> bool:  # noqa: ANN001 - protocol-shaped fake
        return ref.name in self.running

    def stop(self, ref) -> None:  # noqa: ANN001 - protocol-shaped fake
        self.stopped.append(ref.name)
        self.running.discard(ref.name)


def _active_session(terminal_id: str):
    return SimpleNamespace(terminal_id=terminal_id)


class _FakePublishRetryAbandoner:
    def __init__(self) -> None:
        self.abandoned: list[int] = []

    def abandon_issue(self, issue_number: int) -> None:
        self.abandoned.append(issue_number)


def _canceller(pair_registry=None, job_supervisor=None):
    return lambda issue_number, reason: cancel_issue_review_exchange(
        issue_number=issue_number,
        reason=reason,
        pair_registry=pair_registry,
        job_supervisor=job_supervisor,
    )


def test_terminate_issue_runtime_abandons_publish_retry() -> None:
    """The shared boundary must also abandon in-flight publish retries."""
    publish_recovery = _FakePublishRetryAbandoner()

    terminate_issue_runtime(
        issue_number=230,
        reason="issue-completed",
        review_exchange_canceller=_canceller(),
        publish_recovery=publish_recovery,
    )

    assert publish_recovery.abandoned == [230]


def test_terminate_issue_runtime_without_publish_recovery_is_noop() -> None:
    """Omitting the abandoner keeps the boundary working (backward compatible)."""
    result = terminate_issue_runtime(
        issue_number=230,
        reason="issue-completed",
        review_exchange_canceller=_canceller(),
    )

    assert result.issue_number == 230


def test_terminate_issue_runtime_stops_issue_rework_and_hidden_exchange() -> None:
    pair_registry = Mock()
    job_supervisor = Mock()
    job_supervisor.cancel_matching.return_value = ["review-exchange:230:coding-1"]
    session_manager = _FakeSessionManager({"issue-230", "rework-230", "issue-999"})
    active_sessions = [
        _active_session("issue-230"),
        _active_session("rework-230"),
        _active_session("review-77"),
        _active_session("issue-999"),
    ]

    result = terminate_issue_runtime(
        issue_number=230,
        reason="reset-retry",
        review_exchange_canceller=_canceller(pair_registry, job_supervisor),
        session_manager=session_manager,
        active_sessions=active_sessions,
    )

    pair_registry.release.assert_called_once_with(230, reason="reset-retry")
    job_supervisor.cancel_matching.assert_called_once()
    job_supervisor.wait_until_stopped.assert_called_once_with(
        ("review-exchange:230:coding-1",)
    )
    predicate = job_supervisor.cancel_matching.call_args.args[0]
    assert predicate("review-exchange:230:coding-1")
    assert not predicate("review-exchange:231:coding-1")
    assert session_manager.stopped == ["issue-230", "rework-230"]
    assert result.stopped_session_ids == ("issue-230", "rework-230")
    assert result.cleared_active_session_ids == ("issue-230", "rework-230")
    assert result.cancelled_job_ids == ("review-exchange:230:coding-1",)
    assert [session.terminal_id for session in active_sessions] == [
        "review-77",
        "issue-999",
    ]


def test_cancelled_worker_must_stop_before_runtime_cleanup_can_continue() -> None:
    pair_registry = Mock()
    job_supervisor = Mock()
    job_supervisor.cancel_matching.return_value = ["review-exchange:230:coding-1"]
    job_supervisor.wait_until_stopped.return_value = False

    with pytest.raises(RuntimeError, match="did not stop before cleanup"):
        cancel_issue_review_exchange(
            issue_number=230,
            reason="session-cleanup",
            pair_registry=pair_registry,
            job_supervisor=job_supervisor,
        )

    pair_registry.release.assert_called_once_with(230, reason="session-cleanup")


def test_terminate_issue_runtime_clears_stale_active_session_records() -> None:
    session_manager = _FakeSessionManager(set())
    active_sessions = [_active_session("issue-230"), _active_session("issue-231")]

    result = terminate_issue_runtime(
        issue_number=230,
        reason="issue-completed",
        review_exchange_canceller=_canceller(),
        session_manager=session_manager,
        active_sessions=active_sessions,
    )

    assert session_manager.stopped == []
    assert result.stopped_session_ids == ()
    assert result.cleared_active_session_ids == ("issue-230",)
    assert [session.terminal_id for session in active_sessions] == ["issue-231"]


def test_terminate_issue_runtime_requires_session_manager_for_active_records() -> None:
    pair_registry = Mock()

    with pytest.raises(RuntimeError, match="without a SessionManager"):
        terminate_issue_runtime(
            issue_number=230,
            reason="reset-retry",
            review_exchange_canceller=_canceller(pair_registry),
            session_manager=None,
            active_sessions=[_active_session("issue-230")],
        )

    pair_registry.release.assert_not_called()
