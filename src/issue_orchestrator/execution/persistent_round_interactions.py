"""Interaction helpers for persistent PTY sessions."""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .session_interactions import (
    SessionInteractionHandler,
    builtin_session_interaction_rules,
)

_STARTUP_INTERACTION_TIMEOUT_SECONDS = 3.0
_STARTUP_INTERACTION_POLL_SECONDS = 0.05


class _WritablePersistentSession(Protocol):
    master_fd: int
    closed: bool


@dataclass
class PersistentInteractionState:
    handler: SessionInteractionHandler
    prepared: bool = False

    def observe(self, data: bytes) -> None:
        self.handler.on_output(data)


def persistent_interaction_state(
    command: list[str],
) -> PersistentInteractionState | None:
    rules = builtin_session_interaction_rules(shlex.join(command))
    if not rules:
        return None
    label = command[0].rsplit("/", 1)[-1] if command else "persistent-agent"
    handler = SessionInteractionHandler(session_name=label, rules=rules)
    return PersistentInteractionState(handler=handler)


def bind_interaction_sender(
    session: _WritablePersistentSession,
    state: PersistentInteractionState,
) -> None:
    state.handler.bind_sender(lambda text: _send_interaction_line(session, text))


def prepare_startup_interactions(
    state: PersistentInteractionState | None,
    *,
    # `object`, not `None`: the caller only needs the side effect, and
    # `drain_pty_output_until_quiet` now reports whether it observed quiet.
    # Demanding None would force every caller to wrap a useful return away.
    drain_output: Callable[[], object],
    now: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    if state is None or state.prepared:
        return

    deadline = now() + _STARTUP_INTERACTION_TIMEOUT_SECONDS
    while now() < deadline:
        drain_output()
        if state.handler.all_rules_fired:
            break
        sleep(min(_STARTUP_INTERACTION_POLL_SECONDS, max(deadline - now(), 0.0)))
    state.prepared = True


def _send_interaction_line(session: _WritablePersistentSession, text: str) -> bool:
    if session.closed:
        return False
    payload = f"{text}\r".encode("utf-8")
    try:
        written = os.write(session.master_fd, payload)
    except (BlockingIOError, OSError):
        return False
    return written == len(payload)
