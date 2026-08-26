"""PTY I/O helpers for persistent round sessions."""

from __future__ import annotations

import json
import logging
import select
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from ..infra.terminal_recording import MirroredTerminalRecordingWriter

logger = logging.getLogger(__name__)


class _ReadablePersistentSession(Protocol):
    master_fd: int
    closed: bool
    log_writer: MirroredTerminalRecordingWriter | None
    output_observer: Callable[[bytes], None] | None


def drain_pty_output(session: _ReadablePersistentSession) -> int:
    """Read all currently available PTY output and return its byte count."""
    drained = 0
    while True:
        if session.closed:
            return drained
        try:
            ready, _, _ = select.select([session.master_fd], [], [], 0)
        except OSError:
            logger.debug(
                "[send_round] PTY drain skipped for closed fd=%d",
                session.master_fd,
            )
            return drained
        if not ready:
            return drained
        try:
            chunk = os_read(session.master_fd)
        except (BlockingIOError, OSError):
            return drained
        if not chunk:
            return drained
        drained += len(chunk)
        if session.log_writer is not None:
            session.log_writer.write(chunk)
        if session.output_observer is not None:
            session.output_observer(chunk)


def safe_recording_size(session: _ReadablePersistentSession) -> int | None:
    """Return the current role-recording size when it is available."""
    log_writer = session.log_writer
    if log_writer is None:
        return None
    recording_path = getattr(log_writer, "recording_path", None)
    if recording_path is None or not recording_path.exists():
        return None
    try:
        return recording_path.stat().st_size
    except OSError:
        return None


def try_read_response(response_file: Path) -> dict[str, Any] | None:
    """Return a complete JSON object, tolerating absent or partial writes."""
    if not response_file.exists():
        return None
    try:
        text = response_file.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def drain_pty_output_until_quiet(
    session: _ReadablePersistentSession,
    *,
    quiet_seconds: float,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Keep reading from a persistent PTY until output stays quiet."""
    deadline = now() + quiet_seconds
    hard_cap = now() + max(quiet_seconds, 1.0)
    while now() < deadline and now() < hard_cap:
        if session.closed:
            return
        try:
            ready, _, _ = select.select([session.master_fd], [], [], 0)
        except OSError:
            logger.debug(
                "[send_round] quiet-drain skipped for closed fd=%d",
                session.master_fd,
            )
            return
        if not ready:
            sleep(min(quiet_seconds / 4, 0.05))
            continue
        try:
            chunk = os_read(session.master_fd)
        except (BlockingIOError, OSError):
            sleep(min(quiet_seconds / 4, 0.05))
            continue
        if not chunk:
            return
        if session.log_writer is not None:
            session.log_writer.write(chunk)
        if session.output_observer is not None:
            session.output_observer(chunk)
        deadline = now() + quiet_seconds


def os_read(fd: int) -> bytes:
    """Isolate the raw read for focused tests/monkeypatching if needed."""
    import os

    return os.read(fd, 4096)
