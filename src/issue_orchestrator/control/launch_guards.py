"""Reasons a session launch is refused before any work happens.

Pure control policy over explicit arguments — no dependency bundle, no
infra — so these rules can be read and tested without standing up a
launcher, and so every launch flavor applies the same ones.

They live together because they share a contract: return a
``LaunchResult`` describing the refusal, or ``None`` to proceed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from .session_launch_types import LaunchResult
from .transition_log import log_transition

if TYPE_CHECKING:
    from ..domain.models import Session
    from ..ports.agent_callback_endpoint import AgentCallbackEndpoint


def callback_endpoint_not_ready(
    endpoint: "AgentCallbackEndpoint",
) -> LaunchResult | None:
    """Defer while the agent callback endpoint is unresolved.

    Every flavor spawns an agent with the same callback-dependent
    completion environment, so every flavor must observe this rule — it
    previously lived inside one launcher's precondition helper, which
    review, retrospective-review and rework never reach (#6924 F7-R3).

    Retryable: the next tick launches once the server has published, or
    a run mode has declared that it serves no Control API.
    """
    if endpoint.is_ready():
        return None
    return LaunchResult(
        None, False,
        "Agent callback endpoint not published yet; deferring launch",
    )


def retrospective_session_conflict(
    session_name: str,
    issue_number: int,
    active_sessions: list["Session"],
    *,
    session_exists: Callable[[str], bool],
) -> LaunchResult | None:
    """Whether a retrospective review for this issue is already live.

    Two conflicts with different queue semantics: an in-flight session
    drops the request, while a lingering terminal keeps it queued for a
    later tick.
    """
    if any(s.terminal_id == session_name for s in active_sessions):
        log_transition(
            "retrospective-review", issue_number, "QUEUED", "SKIP",
            "already in active_sessions",
        )
        return LaunchResult(None, False, "Already in active sessions")
    if session_exists(session_name):
        log_transition(
            "retrospective-review", issue_number, "QUEUED", "SKIP",
            "terminal session already running",
        )
        return LaunchResult(
            None, False, "Terminal session already running", keep_queued=True
        )
    return None
