"""Shared review-exchange round failure reasons and display text."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any


class RoundFailureReason(str, enum.Enum):
    """Machine reasons for a role round that failed before completion."""

    NO_COMPLETION = "no_completion"
    TIMEOUT = "timeout"
    PROCESS_EXITED_BEFORE_RESPONSE = "process_exited_before_response"
    INVALID_RESPONSE = "invalid_response"
    SESSION_CLOSED = "session_closed"
    PROMPT_WRITE_FAILED = "prompt_write_failed"
    PROMPT_NOT_ACCEPTED = "prompt_not_accepted"
    ROUND_ERROR = "round_error"
    #: The round was still in flight when the whole exchange was torn down —
    #: a supervisor wall-clock deadline or an operator cancellation. The round
    #: never declared anything itself; the teardown declared it for it.
    ABANDONED_BY_TEARDOWN = "abandoned_by_teardown"
    UNKNOWN = "unknown"


# Reasons that mean the role's *process* is dead or unusable for the next
# prompt — the turn itself can be retried by respawning a fresh process in the
# same worktree (the one-shot reviewer that exits cleanly between rounds is the
# canonical case). This is deliberately narrower than "the round failed":
#   - TIMEOUT / INVALID_RESPONSE: the process is alive and responded, just not
#     usefully. Respawning would kill a working process and could mask a stuck
#     agent, so these are NOT recoverable by respawn.
#   - PROMPT_NOT_ACCEPTED: the process stayed live but stopped emitting after
#     prompt delivery, so the next reliable owner action is a fresh process.
#   - NO_COMPLETION / ROUND_ERROR / UNKNOWN: ambiguous; not safe to auto-retry.
PROCESS_UNUSABLE_FAILURE_REASONS: frozenset[RoundFailureReason] = frozenset({
    RoundFailureReason.PROCESS_EXITED_BEFORE_RESPONSE,
    RoundFailureReason.SESSION_CLOSED,
    RoundFailureReason.PROMPT_WRITE_FAILED,
    RoundFailureReason.PROMPT_NOT_ACCEPTED,
})


def is_process_unusable_failure(reason: Any) -> bool:
    """True when a round failed because the role process is dead/unusable.

    These failures are recoverable by respawning the role in place and
    retrying the same turn; see ``PROCESS_UNUSABLE_FAILURE_REASONS``.
    """
    return coerce_round_failure_reason(reason) in PROCESS_UNUSABLE_FAILURE_REASONS


@dataclass(frozen=True)
class RoundFailurePresentation:
    """Human text for one round-failure reason across all surfaces."""

    chapter_label: str
    narrative_phrase: str


_ROUND_FAILURE_PRESENTATION: dict[RoundFailureReason, RoundFailurePresentation] = {
    RoundFailureReason.NO_COMPLETION: RoundFailurePresentation(
        chapter_label="did not complete",
        narrative_phrase="did not complete",
    ),
    RoundFailureReason.TIMEOUT: RoundFailurePresentation(
        chapter_label="timed out",
        narrative_phrase="timed out",
    ),
    RoundFailureReason.PROCESS_EXITED_BEFORE_RESPONSE: RoundFailurePresentation(
        chapter_label="exited before responding",
        narrative_phrase="exited before responding",
    ),
    RoundFailureReason.INVALID_RESPONSE: RoundFailurePresentation(
        chapter_label="returned invalid response",
        narrative_phrase="returned invalid response",
    ),
    RoundFailureReason.SESSION_CLOSED: RoundFailurePresentation(
        chapter_label="session was closed before responding",
        narrative_phrase="session was closed before responding",
    ),
    RoundFailureReason.PROMPT_WRITE_FAILED: RoundFailurePresentation(
        chapter_label="prompt delivery failed",
        narrative_phrase="prompt delivery failed",
    ),
    RoundFailureReason.PROMPT_NOT_ACCEPTED: RoundFailurePresentation(
        chapter_label="did not accept the prompt",
        narrative_phrase="did not accept the prompt",
    ),
    RoundFailureReason.ROUND_ERROR: RoundFailurePresentation(
        chapter_label="round failed",
        narrative_phrase="round failed",
    ),
    RoundFailureReason.ABANDONED_BY_TEARDOWN: RoundFailurePresentation(
        chapter_label="was abandoned when the exchange was torn down",
        narrative_phrase="was abandoned when the exchange was torn down",
    ),
    RoundFailureReason.UNKNOWN: RoundFailurePresentation(
        chapter_label="did not complete",
        narrative_phrase="did not complete",
    ),
}


def coerce_round_failure_reason(value: Any) -> RoundFailureReason:
    """Return a known round-failure reason, or ``UNKNOWN`` for legacy data."""
    if isinstance(value, RoundFailureReason):
        return value
    if isinstance(value, str) and value:
        try:
            return RoundFailureReason(value)
        except ValueError:
            return RoundFailureReason.UNKNOWN
    return RoundFailureReason.UNKNOWN


def round_failure_reason_value(reason: RoundFailureReason) -> str:
    """Return the stable event/artifact value for a typed reason."""
    return reason.value


def round_failure_chapter_label(reason: Any) -> str:
    """Return the label fragment used in recording chapter sidecars."""
    return _ROUND_FAILURE_PRESENTATION[
        coerce_round_failure_reason(reason)
    ].chapter_label


def round_failure_narrative_phrase(reason: Any) -> str:
    """Return the user-facing narrative phrase for a round failure."""
    return _ROUND_FAILURE_PRESENTATION[
        coerce_round_failure_reason(reason)
    ].narrative_phrase
