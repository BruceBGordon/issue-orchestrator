"""Helpers for active session tracking."""

import logging

from ..domain.models import Session

logger = logging.getLogger(__name__)


def append_unique_active_sessions(
    active_sessions: list[Session],
    incoming: list[Session],
) -> list[Session]:
    """Append sessions while preserving unique terminal identity.

    Returns the sessions that were actually added, so callers do not need to
    re-derive reporting state from the mutated active-session list.
    """
    existing_ids = {s.terminal_id for s in active_sessions}
    added: list[Session] = []
    for session in incoming:
        if session.terminal_id in existing_ids:
            logger.warning(
                "[ACTIVE_SESSIONS] Duplicate terminal suppressed: %s (issue=%s)",
                session.terminal_id,
                session.issue.number,
            )
            continue
        active_sessions.append(session)
        existing_ids.add(session.terminal_id)
        added.append(session)
    return added


def has_active_terminal(active_sessions: list[Session], terminal_id: str) -> bool:
    """Return whether a terminal id is still active in the live session list."""
    return any(session.terminal_id == terminal_id for session in active_sessions)
