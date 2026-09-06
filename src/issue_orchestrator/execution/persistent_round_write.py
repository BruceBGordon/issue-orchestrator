"""Writing to a persistent PTY: full writes, timeout diagnostics, submission.

Split out of ``persistent_round_runner`` because it is one job with one
subject — getting bytes into an agent's stdin and saying something useful when
that fails — and because the runner was over its line budget with it inside.

The two-write submit contract lives here. Do NOT collapse it into a single
batched write or into ``\n``: ``\n`` never submits to a raw-mode TUI (the
tixmeup #277/#290 hang), and codex treats a ``\r`` batched with the prompt
text as a literal newline in its input box. Only an Enter arriving as its own
write, after the echo settles, submits.
"""

from __future__ import annotations

import errno
import logging
import os
import select
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from ..domain.review_exchange_failures import RoundFailureReason
from ..infra.terminal_recording import MirroredTerminalRecordingWriter
from .persistent_round_failures import (
    PersistentRoundError,
    PersistentRoundTimeoutError,
)
from .composer_readiness import await_ready_before_typing
from .persistent_round_io import drain_pty_output_until_quiet

logger = logging.getLogger(__name__)

_PTY_WRITE_HEARTBEAT_SECONDS = 5.0
# Echo-settle between the prompt text and the standalone Enter. Covers the
# ECHO only, and its backstop stays SHORT: readiness is gated upstream by
# composer_readiness, which also records why no quiet window can answer
# "is the agent busy" for a TUI that repaints at ~10Hz forever. A generous
# backstop here stalls every round instead — at 60s the Enter arrived a minute
# late to a PTY that had stopped accepting it (0/5 in the micro harness).
_ENTER_SETTLE_QUIET_SECONDS = 0.3
_ENTER_SETTLE_MAX_WAIT_SECONDS = 2.0


class WritableSession(Protocol):
    """What writing needs — narrower than PersistentSession, and narrow enough
    that this module never imports the runner back."""

    master_fd: int
    closed: bool
    proc: Any
    log_writer: MirroredTerminalRecordingWriter | None


def _write_full(
    fd: int,
    payload: bytes,
    *,
    deadline: float,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    role_label: str | None = None,
    pid: int | None = None,
    heartbeat_seconds: float = _PTY_WRITE_HEARTBEAT_SECONDS,
    drain_output: Callable[[], int] | None = None,
) -> int:
    """Write all of ``payload`` to a non-blocking fd, looping on partial writes.

    The PTY master fd is non-blocking (``open_persistent_session`` sets
    ``os.set_blocking(master_fd, False)``). On a non-blocking fd
    ``os.write`` can return *fewer* bytes than requested when the
    kernel's PTY input buffer is nearly full. The previous
    single-call ``os.write(fd, payload)`` ignored the return value;
    any unwritten suffix was silently dropped, the agent got a
    truncated prompt, and the round hung waiting for a response that
    would never arrive (#6160 e2e regression).

    Loops until the full payload is on the wire, retrying with a
    short backoff on ``BlockingIOError`` (kernel buffer momentarily
    full) and on zero-byte writes. Raises
    :class:`PersistentRoundTimeoutError` if the deadline expires
    before the buffer drains enough to accept the rest.
    """
    written = 0
    backoff = 0.005
    started_at = now()
    last_heartbeat = started_at
    label = role_label or f"fd={fd}"
    while written < len(payload):
        current = now()
        if current > deadline:
            raise PersistentRoundTimeoutError(
                f"Could not write {len(payload)} bytes to PTY fd={fd} role={label} "
                f"within deadline ({written} bytes accepted before timeout)"
            )
        try:
            n = os.write(fd, payload[written:])
        except BlockingIOError:
            n = 0  # kernel buffer momentarily full — same backoff as a 0-byte write
        except OSError as exc:
            raise PersistentRoundError(
                f"Could not write prompt to PTY fd={fd} role={label}: {exc}",
                failure_reason=RoundFailureReason.PROMPT_WRITE_FAILED,
            ) from exc
        if n == 0:
            _drain_during_write_backoff(drain_output)
            if current - last_heartbeat >= heartbeat_seconds:
                logger.info(
                    "[send_round] waiting for PTY write role=%s pid=%s fd=%d "
                    "elapsed=%.1fs deadline_in=%.1fs written=%d remaining=%d",
                    label,
                    pid if pid is not None else "n/a",
                    fd,
                    current - started_at,
                    deadline - current,
                    written,
                    len(payload) - written,
                )
                last_heartbeat = current
            sleep(backoff)
            backoff = min(backoff * 2, 0.1)
            continue
        written += n
        backoff = 0.005
        if written < len(payload):
            logger.debug(
                "[send_round] partial PTY write fd=%d wrote=%d total=%d remaining=%d",
                fd, n, written, len(payload) - written,
            )
    return written


def _drain_during_write_backoff(drain_output: Callable[[], int] | None) -> None:
    if drain_output is None:
        return
    drained = drain_output()
    if drained:
        logger.debug(
            "[send_round] drained %d PTY output byte(s) while write was blocked",
            drained,
        )


def _write_prompt_with_timeout_diagnostics(
    session: WritableSession,
    payload: bytes,
    *,
    response_file: Path,
    write_deadline: float,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    role_label: str,
    timeout_seconds: float,
    write_timeout_seconds: float,
) -> int:
    try:
        return _write_full(
            session.master_fd, payload,
            deadline=write_deadline,
            now=now,
            sleep=sleep,
            role_label=role_label,
            pid=session.proc.pid,
            drain_output=lambda: drain_pty_output(session),
        )
    except PersistentRoundTimeoutError as exc:
        recording_size = safe_recording_size(session)
        logger.warning(
            "[send_round] prompt write timeout role=%s pid=%d alive=%s "
            "closed=%s response_file=%s prompt_bytes=%d write_timeout=%.1fs "
            "timeout=%.1fs recording_bytes=%s "
            "likely_stale_persistent_session=True error=%s",
            role_label,
            session.proc.pid,
            session.proc.poll() is None,
            session.closed,
            response_file,
            len(payload),
            write_timeout_seconds,
            timeout_seconds,
            recording_size if recording_size is not None else "n/a",
            exc,
        )
        raise


def submit_prompt_with_enter(
    session: WritableSession,
    payload: bytes,
    *,
    response_file: Path,
    write_deadline: float,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    label: str,
    timeout_seconds: float,
    write_timeout_seconds: float,
    read_response: Callable[[], dict[str, Any] | None],
) -> tuple[int, dict[str, Any] | None]:
    """Write the prompt, let the echo settle, then submit with a standalone Enter.

    Two-write contract (TestPromptSubmissionTerminator; do NOT regress to a
    single batched write or to ``\\n``): ``\\n`` never submits to a raw-mode
    TUI (the tixmeup #277/#290 hang), and codex treats a ``\\r`` batched with
    the prompt text as a literal newline in its input box — only an Enter
    arriving as its own write after the echo settles submits. claude accepts
    either form.

    Returns ``(bytes_written, recovered_response)``. ``recovered_response``
    is non-None when the agent answered from the prompt write alone and
    exited before the Enter landed (one-shot agents: the dead PTY raises on
    the Enter write). The response file is authoritative — the same tolerance
    as the poll loop's exited-after-answering path.
    """
    # Never type at an agent that is mid-turn: the Enter would not submit and
    # the prompt would strand in the composer (#7104). See composer_readiness
    # for why the SCREEN answers this and byte activity provably cannot.
    readiness = await_ready_before_typing(
        session, round_timeout_seconds=timeout_seconds, now=now, sleep=sleep
    )
    if readiness is not None and not readiness.ready:
        # Write anyway — a doomed prompt beats failing a round that might work
        # — but name the cause now rather than leaving a 600s mystery timeout.
        logger.warning(
            "[send_round] %s role=%s pid=%d",
            readiness.describe(), label, session.proc.pid,
        )

    written = _write_prompt_with_timeout_diagnostics(
        session, payload,
        response_file=response_file, write_deadline=write_deadline,
        now=now, sleep=sleep, role_label=label,
        timeout_seconds=timeout_seconds,
        write_timeout_seconds=write_timeout_seconds,
    )
    settled = drain_pty_output_until_quiet(
        session,
        quiet_seconds=_ENTER_SETTLE_QUIET_SECONDS,
        max_wait_seconds=_ENTER_SETTLE_MAX_WAIT_SECONDS,
        now=now,
        sleep=sleep,
    )
    if not settled:
        # Expected against a TUI that repaints forever: codex never produces a
        # 0.3s gap, so this fires on every successful round (measured: 10/10
        # micro-harness passes all reported it). Readiness is guarded upstream
        # by ComposerGate, so this is a debug detail about the echo settle, not
        # a warning — logging it louder would train people to ignore the line.
        logger.debug(
            "[send_round] TUI never went quiet within %.0fs; submitting Enter "
            "anyway and the prompt may strand in the composer (#7104) "
            "role=%s pid=%d",
            _ENTER_SETTLE_MAX_WAIT_SECONDS, label, session.proc.pid,
        )
    try:
        written += _write_prompt_with_timeout_diagnostics(
            session, b"\r",
            response_file=response_file, write_deadline=write_deadline,
            now=now, sleep=sleep, role_label=label,
            timeout_seconds=timeout_seconds,
            write_timeout_seconds=write_timeout_seconds,
        )
    except PersistentRoundError:
        recovered = read_response()
        if recovered is None:
            raise
        logger.info(
            "[send_round] enter write failed but agent already answered "
            "role=%s pid=%d", label, session.proc.pid,
        )
        return written, recovered
    return written, None


def safe_recording_size(session: WritableSession) -> int | None:
    """Best-effort read of the role recording's current size in bytes.

    Used by ``send_round``'s heartbeat to surface "is the agent
    producing output at all" — non-zero growth between heartbeats
    means the agent is alive and emitting; zero growth means it
    hasn't even started rendering its prompt yet (or the TUI is
    wedged on a startup dialog with no auto-responder).
    """
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


def drain_pty_output(session: WritableSession) -> int:
    """Read everything currently available on the master fd into the log.

    When no log writer is configured (tests that don't care about
    output), the chunks are discarded — they've been read off the PTY,
    which is what matters to free the buffer. Returns the total number
    of bytes drained on this call so the caller can surface
    agent-is-alive evidence in heartbeat logs.
    """
    drained = 0
    while True:
        if session.closed:
            return drained
        try:
            ready, _, _ = select.select([session.master_fd], [], [], 0)
        except OSError:
            logger.debug(
                "[send_round] PTY drain skipped for closed fd=%d pid=%d",
                session.master_fd,
                session.proc.pid,
            )
            return drained
        if not ready:
            return drained
        try:
            chunk = os.read(session.master_fd, 4096)
        except (BlockingIOError, OSError):
            return drained
        if not chunk:
            return drained
        drained += len(chunk)
        if session.log_writer is not None:
            session.log_writer.write(chunk)
        if session.output_observer is not None:
            session.output_observer(chunk)


