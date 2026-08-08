"""Action generation for rejected agent completion records."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..domain.models import Session
from .actions import Action, AddCommentAction, AddLabelAction, RemoveLabelAction
from .completion_record_validation import CompletionRecordLoadFailure
from .needs_human_block import NeedsHumanCause

if TYPE_CHECKING:
    from .label_manager import LabelManager
    from .reconciliation import ExpectedState

_INVALID_RECORD_KIND = "invalid_completion_record"


def invalid_record_failure_reason(detail: Mapping[str, Any] | None) -> str | None:
    """Return a display reason for rejected-record detail."""
    if not _is_invalid_record(detail):
        return None
    return _field(detail, "failure_reason") or "Completion record rejected"


def invalid_record_event_fields(detail: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return SESSION_FAILED enrichment fields for rejected-record detail."""
    if not _is_invalid_record(detail):
        return {}
    payload: dict[str, Any] = {"failure_kind": _INVALID_RECORD_KIND}
    for key in (
        "completion_load_failure",
        "completion_parse_error",
        "completion_path_absolute",
        "diagnostic_path",
    ):
        value = _field(detail, key)
        if value:
            payload[key] = value
    diagnostic = _field(detail, "diagnostic_path")
    if diagnostic:
        payload["artifacts"] = [
            {
                "type": "diagnostic",
                "label": "Invalid Completion Diagnostic",
                "value": diagnostic,
            }
        ]
    return payload


def invalid_record_allows_interrupted_retry(detail: Mapping[str, Any] | None) -> bool:
    """Return True when a rejected record is consistent with interrupted output."""
    failure = _field(detail, "completion_load_failure")
    return failure in {
        CompletionRecordLoadFailure.INVALID_JSON.value,
        CompletionRecordLoadFailure.UNREADABLE.value,
    }


def failure_event_reason(
    *,
    expired: bool,
    timeout_minutes: int,
    detail: Mapping[str, Any] | None,
) -> str:
    """Return the SESSION_FAILED display reason with invalid-record specificity."""
    invalid = invalid_record_failure_reason(detail)
    return (
        f"Exceeded {timeout_minutes} min timeout"
        if expired
        else invalid or "Session ended without PR or status update"
    )


def invalid_record_actions(
    *,
    session: Session,
    expected: "ExpectedState",
    labels: "LabelManager",
    detail: Mapping[str, Any] | None,
    diagnostic_path: str | None,
) -> list[Action] | None:
    """Return actions for a rejected record, or None for ordinary failures."""
    if not _is_invalid_record(detail):
        return None

    issue_number = session.issue.number
    in_progress_label = labels.in_progress
    issue_work = session.terminal_id.startswith("issue-")
    session_kind = session.terminal_id.split("-", 1)[0]
    failure = _field(detail, "completion_load_failure") or "invalid_schema"
    parse_error = (
        _field(detail, "completion_parse_error")
        or _field(detail, "failure_reason")
        or "Completion record rejected"
    )
    diagnostic = _non_empty_str(diagnostic_path) or _field(detail, "diagnostic_path")

    comment = _comment(
        title=(
            "Completion Record Rejected"
            if issue_work
            else f"{session_kind.capitalize()} Completion Record Rejected"
        ),
        failure=failure,
        parse_error=parse_error,
        completion_path=_field(detail, "completion_path_absolute"),
        diagnostic_path=diagnostic,
        runtime_minutes=session.runtime_minutes,
        session_id=session.terminal_id,
        needs_human_label=labels.needs_human if issue_work else None,
    )

    if issue_work:
        return [
            AddLabelAction(
                issue_number=issue_number,
                label=labels.needs_human,
                reason="Completion record could not be accepted by orchestrator",
                needs_human_cause=NeedsHumanCause.SESSION_LIFECYCLE,
                expected=expected,
            ),
            AddCommentAction(
                number=issue_number,
                comment=comment,
                reason="Notify about rejected completion record",
                expected=expected,
            ),
            RemoveLabelAction(
                issue_number=issue_number,
                label=in_progress_label,
                reason="Invalid completion record - releasing claim",
                expected=expected,
            ),
        ]

    return [
        AddCommentAction(
            number=issue_number,
            comment=comment,
            reason=f"Notify about rejected {session_kind} completion record",
            expected=expected,
        ),
    ]


def _is_invalid_record(detail: Mapping[str, Any] | None) -> bool:
    return _field(detail, "failure_kind") == _INVALID_RECORD_KIND


def _field(detail: Mapping[str, Any] | None, key: str) -> str | None:
    if detail is None:
        return None
    return _non_empty_str(detail.get(key))


def _non_empty_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _comment(
    *,
    title: str,
    failure: str,
    parse_error: str,
    completion_path: str | None,
    diagnostic_path: str | None,
    runtime_minutes: float,
    session_id: str,
    needs_human_label: str | None,
) -> str:
    lines = [
        f"**{title}**",
        "",
        _comment_intro(failure),
        "",
        f"- Failure: `{failure}`",
        f"- Error: {parse_error}",
    ]
    _append_optional(lines, "Completion record", completion_path)
    _append_optional(lines, "Diagnostic", diagnostic_path)
    lines.extend(
        [
            f"- Runtime: {runtime_minutes:.1f} minutes",
            f"- Session: `{session_id}`",
            "",
        ]
    )
    lines.append(_terminal_note(needs_human_label))
    return "\n".join(lines)


def _comment_intro(failure: str) -> str:
    if failure == CompletionRecordLoadFailure.UNREADABLE.value:
        return (
            "The agent did call the completion command, but the orchestrator "
            "could not read the completion JSON from disk."
        )
    return (
        "The agent did call the completion command, but the orchestrator "
        "could not accept the completion JSON."
    )


def _append_optional(lines: list[str], label: str, value: str | None) -> None:
    if value:
        lines.append(f"- {label}: `{value}`")


def _terminal_note(needs_human_label: str | None) -> str:
    if needs_human_label:
        return (
            f"This issue has been marked as `{needs_human_label}` because "
            "the orchestrator could not safely apply the agent's requested "
            "outcome.\nRemove the label after correcting or rerunning the task."
        )
    return (
        "The PR remains pending because the orchestrator could not safely apply "
        "the agent's requested outcome."
    )
