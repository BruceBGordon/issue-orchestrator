"""Tests for the publish-failure status-reason summary.

The generic "Push or PR creation failed" text used to show on blocked cards
regardless of the real cause. This module tests the pure summariser that
now extracts a human-readable reason from the underlying error strings.
"""

from __future__ import annotations

from issue_orchestrator.control.completion_history_status import (
    _summarize_publish_failure,
)
from issue_orchestrator.control.completion_types import (
    ERROR_PREFIX_CREATE_PR,
    ERROR_PREFIX_PUSH,
)
from issue_orchestrator.domain.models import RequestedAction


def test_stage_prefixes_match_requested_action_values() -> None:
    """publish.failed payloads stage = RequestedAction.value.

    The push-branch / create-PR failure paths report the stage via two
    different source strings today (ERROR_PREFIX_* on the non-exception
    path, action.value on the exception path). They MUST agree — downstream
    consumers dispatch on ``stage`` and would silently break if the two
    diverged.
    """
    assert ERROR_PREFIX_PUSH == RequestedAction.PUSH_BRANCH.value
    assert ERROR_PREFIX_CREATE_PR == RequestedAction.CREATE_PR.value


def test_push_failure_surfaces_underlying_error() -> None:
    errors = [
        "push_branch: Push failed: git command timed out: rc=-1 cmd=git ... "
        "push --force-with-lease -u origin feature-branch\nSTDOUT: ... "
    ]
    summary = _summarize_publish_failure(errors)

    assert summary.startswith("Push failed: ")
    assert "git command timed out" in summary
    # Collapsed to a single line for card rendering.
    assert "\n" not in summary
    # Stays within the card-friendly cap.
    assert len(summary) <= 160


def test_create_pr_failure_uses_friendly_label() -> None:
    errors = [
        "create_pr: pull request \"#230\" already exists: https://github.com/o/r/pull/5"
    ]
    summary = _summarize_publish_failure(errors)

    assert summary.startswith("PR creation failed: ")
    assert "already exists" in summary


def test_unknown_prefix_falls_back_to_raw_message() -> None:
    errors = ["unexpected: something odd happened"]
    summary = _summarize_publish_failure(errors)

    # Unknown stage prefix — no friendly label, but the message still shows.
    assert "something odd happened" in summary
    assert "Push failed" not in summary
    assert "PR creation failed" not in summary


def test_empty_input_keeps_legacy_text() -> None:
    assert _summarize_publish_failure([]) == "Push or PR creation failed"


def test_empty_message_after_prefix_keeps_legacy_text() -> None:
    # Defensive: if the error string is exactly "push_branch:" with no body,
    # we shouldn't emit "Push failed: " with a trailing space — fall back
    # to the legacy generic.
    assert _summarize_publish_failure(["push_branch:"]) == "Push or PR creation failed"


def test_long_message_is_truncated_with_ellipsis() -> None:
    long_tail = "error " * 80  # ~480 chars
    errors = [f"push_branch: Push failed: {long_tail}"]
    summary = _summarize_publish_failure(errors)

    assert summary.startswith("Push failed: ")
    assert len(summary) <= 160
    assert summary.endswith("…")
