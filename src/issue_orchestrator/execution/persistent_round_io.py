"""PTY I/O helpers for persistent round sessions."""

from __future__ import annotations

import logging
import select
import time
from collections.abc import Callable
from typing import Protocol

from ..infra.terminal_recording import MirroredTerminalRecordingWriter

logger = logging.getLogger(__name__)


class _ReadablePersistentSession(Protocol):
    master_fd: int
    closed: bool
    log_writer: MirroredTerminalRecordingWriter | None
    output_observer: Callable[[bytes], None] | None


def drain_pty_output_until_quiet(
    session: _ReadablePersistentSession,
    *,
    quiet_seconds: float,
    max_wait_seconds: float | None = None,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Keep reading from a persistent PTY until output stays quiet.

    Returns True when the quiet window was actually observed, and False when
    ``max_wait_seconds`` expired with output still arriving. Callers that use
    this as a settle before an Enter need that distinction: submitting into a
    TUI that is still painting is how a prompt ends up stranded in the
    composer, and the caller is the only place that knows whether proceeding
    anyway is acceptable.

    ``max_wait_seconds`` is a BACKSTOP, not the mechanism. It defaults to the
    historical ``max(quiet_seconds, 1.0)`` so the response-drain call sites
    keep their behaviour, but that default is far too small for a settle: it
    made the cap SHORTER than the event it was supposed to bound. With
    ``quiet_seconds=0.3`` the cap was a flat one second, so the loop gave up
    one second after entry no matter how hard output was still streaming —
    and then the caller submitted anyway.

    That is #7104. Codex 0.153.4 boots an MCP server on startup and animates a
    spinner while it does, which keeps the PTY busy well past one second; the
    Enter landed mid-boot, was dropped, and the round died 600s later as
    ``reviewer_no_completion`` with the turn text still sitting in the
    composer. It reproduces with nothing else on the machine, so it is not the
    lane-starvation story the issue was originally filed under — starvation
    merely used to be required to lose a race that a longer boot now loses on
    its own.
    """
    limit = max_wait_seconds if max_wait_seconds is not None else max(quiet_seconds, 1.0)
    deadline = now() + quiet_seconds
    hard_cap = now() + limit
    while now() < deadline:
        if now() >= hard_cap:
            return False
        if session.closed:
            return True
        try:
            ready, _, _ = select.select([session.master_fd], [], [], 0)
        except OSError:
            logger.debug(
                "[send_round] quiet-drain skipped for closed fd=%d",
                session.master_fd,
            )
            return True
        if not ready:
            sleep(min(quiet_seconds / 4, 0.05))
            continue
        try:
            chunk = os_read(session.master_fd)
        except (BlockingIOError, OSError):
            sleep(min(quiet_seconds / 4, 0.05))
            continue
        if not chunk:
            return True
        if session.log_writer is not None:
            session.log_writer.write(chunk)
        if session.output_observer is not None:
            session.output_observer(chunk)
        deadline = now() + quiet_seconds
    return True


def os_read(fd: int) -> bytes:
    """Isolate the raw read for focused tests/monkeypatching if needed."""
    import os

    return os.read(fd, 4096)
