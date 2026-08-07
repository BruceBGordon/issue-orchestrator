"""What a terminal with an unreadable pending-work claim means (#6999 F6).

A restarted terminal whose stored claim cannot be read is the one restoration
outcome that must not be handled quietly. The session is alive and doing queued
work nobody can now name: admitting it would let its completion settle as
holding no claim and silently discard that request, and dropping it without a
word would leave a running agent nobody is watching.

So it is quarantined — kept out of active-session tracking — and announced
twice, because the two halves answer different questions. The needs-human label
and comment are DURABLE: they survive the restart that produced the problem and
are what an operator finds tomorrow. The event is what the dashboard reacts to
now. Neither alone is enough (#6771 round 3).
"""

from __future__ import annotations

import logging

from ..events import EventName
from ..ports import EventSink, make_trace_event
from .in_flight_work import QuarantinedSession
from .session_launcher import SessionLauncher

logger = logging.getLogger(__name__)


def escalate_unreadable_claim(
    quarantined: QuarantinedSession,
    *,
    session_launcher: SessionLauncher,
    events: EventSink,
) -> None:
    """Surface a quarantined terminal to a human, durably and immediately."""
    session = quarantined.session
    issue_number = session.issue.number
    logger.error(
        "[WORK] Quarantined %s for issue #%d: its pending-work claim could not "
        "be read (%s). It is NOT being tracked, so its completion cannot settle "
        "as claimless and discard the queued work it is holding.",
        session.terminal_id,
        issue_number,
        quarantined.error,
    )
    session_launcher.escalate_issue_needs_human(
        issue_number=issue_number,
        reason="pending-work claim unreadable",
        comment=(
            "**Session quarantined: its pending-work claim is unreadable**\n\n"
            f"Terminal `{session.terminal_id}` is still running, but the "
            "orchestrator cannot read which queued request it took at launch, "
            "so it is not being tracked — tracking it would let its completion "
            "be recorded as holding no work and silently discard that "
            "request.\n\n"
            f"Error: {quarantined.error}\n\n"
            "A human needs to decide what this session was doing, re-queue it "
            "if necessary, and stop the terminal."
        ),
        context="pending_work_claim_unreadable",
        event_data={
            "issue_number": issue_number,
            "session_name": session.terminal_id,
            "reason": f"pending-work claim unreadable: {quarantined.error}",
        },
    )
    events.publish(make_trace_event(
        EventName.SESSION_CLAIM_UNREADABLE,
        {
            "issue_number": issue_number,
            "session_name": session.terminal_id,
            "run_id": session.run_assets.identity.run_id,
            "error": quarantined.error,
        },
    ))


__all__ = ["escalate_unreadable_claim"]
