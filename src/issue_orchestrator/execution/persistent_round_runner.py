"""Persistent-PTY round runner for review-exchange.

Replaces the single-shot ``interactive_round`` flow for review-exchange
agents. One agent process is attached to a master/slave PTY pair at
exchange start and stays alive across all rounds. Each round:

  - ``send_round`` deletes any stale response file, writes the prompt to
    the master fd, then submits it with a standalone ``\r`` (Enter) once
    the echo settles (see ``send_round``), and polls for the response file.
  - PTY output is captured continuously into a single recording — the
    session viewer plays one ``terminal-recording.jsonl`` per role
    spanning the whole exchange, instead of N per-phase files.

At exchange end, ``close_session`` sends ``SIGTERM`` to the agent's
process group and waits for it to exit. Closing the master fd alone is
not reliable — the spike showed Claude-shaped TUIs do not always exit
cleanly on stdin EOF.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import select
import shutil
import signal
import struct
import subprocess
import termios
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..domain.exchange_kill_evidence import RoundIdleDetector
from ..domain.review_exchange_failures import RoundFailureReason
from ..infra.shutdown_signals import child_signal_reset_preexec
from ..infra.terminal_recording import MirroredTerminalRecordingWriter
from .persistent_round_failures import (
    PersistentRoundError,
    PersistentRoundTimeoutError,
)
from .persistent_round_io import drain_pty_output_until_quiet
from .persistent_round_interactions import (
    PersistentInteractionState,
    bind_interaction_sender,
    persistent_interaction_state,
    prepare_startup_interactions,
)

logger = logging.getLogger(__name__)

_DEFAULT_PTY_COLS = 120
_DEFAULT_PTY_ROWS = 40
_DEFAULT_POLL_INTERVAL_SECONDS = 0.1
_DEFAULT_RESPONSE_DRAIN_SECONDS = 0.1
_DEFAULT_TERMINATE_GRACE_SECONDS = 5.0
_DEFAULT_PTY_WRITE_TIMEOUT_SECONDS = 30.0
_DEFAULT_PROMPT_ACCEPTANCE_IDLE_SECONDS = 120.0
# Echo-settle window between writing the prompt text and the standalone
# Enter ("\r") that submits it — see the two-write contract in send_round.
#
# NOT a readiness signal, and it cannot be made into one. Measured from a
# stranded codex 0.153.4 reviewer recording, 6074 output frames over 600s:
#
#     inter-frame gap  p50=0.104s  p90=0.106s  p99=0.106s  max=0.42s
#     gaps >1.0s: 0
#
# The TUI repaints at ~10Hz continuously, including after the agent said it
# was waiting for a turn — so no quiet window distinguishes "idle at an empty
# composer" from "mid-turn". Below the 0.42s worst case this is satisfied by
# chance gaps (which is what it does today, i.e. barely a wait at all); above
# it, it is never satisfied and every round pays the full backstop before
# submitting anyway. Raising it was tried and is worse: a 60s stall per round
# ending in "Could not write 1 bytes to PTY within deadline".
#
# Gating injection on a READY COMPOSER is the fix, read from the rendered
# screen the way execution/composer_state.py already classifies it after the
# fact. Tracked in #7104; this constant stays where it was until then.
_ENTER_SETTLE_QUIET_SECONDS = 0.3
# Backstop on that settle, NOT the mechanism.
#
# This used to be an implicit `max(quiet_seconds, 1.0)` inside the drain: with
# a 0.3s window, a flat one second — a cap SHORTER than the event it bounded,
# so no settle longer than a second was expressible at all. Making it explicit
# is what allowed the window above to be measured rather than assumed.
#
# Honest about what this does NOT do: it does not fix #7104. With the window
# at 0.3s the backstop is never reached, so this changes no behaviour today.
# Its value is the return flag it enables — `send_round` now says at the
# moment of submission that it is about to Enter into a TUI that never went
# quiet, instead of that being reconstructed from a screen replay after a
# ten-minute timeout.
_ENTER_SETTLE_MAX_WAIT_SECONDS = 60.0

# Heartbeat cadence for the ``send_round`` poll loop. Without this, a
# wedged agent shows up as 17 minutes of total log silence (#6160 e2e
# regression). The heartbeat logs the deadline countdown, recording
# growth, and the agent's process state so the next reproduction tells
# us *which* step is wedged instead of just "something hung."
_SEND_ROUND_HEARTBEAT_SECONDS = 30.0
_PTY_WRITE_HEARTBEAT_SECONDS = 5.0


@dataclass
class PersistentSession:
    """One agent process attached to a PTY for the lifetime of an exchange.

    The same instance carries every round of the exchange. Callers must
    pair every ``open_persistent_session`` with a ``close_persistent_session``.
    """

    proc: subprocess.Popen[bytes]
    master_fd: int
    log_writer: MirroredTerminalRecordingWriter | None = None
    interaction_state: PersistentInteractionState | None = None
    output_observer: Callable[[bytes], None] | None = None
    closed: bool = False

    @property
    def is_live(self) -> bool:
        """Whether this session can still accept another round prompt."""
        return not self.closed and self.proc.poll() is None


def open_persistent_session(
    *,
    command: list[str],
    working_dir: Path,
    env: dict[str, str],
    recording_path: Path | None = None,
    additional_recording_paths: list[Path] | None = None,
    mirror_path: Path | None = None,
) -> PersistentSession:
    """Spawn the agent attached to a PTY. Process stays alive across rounds.

    ``recording_path`` (when provided) gets the canonical raw recording for
    the role's session; ``additional_recording_paths`` is the run-level
    mirror that the session viewer reads. Pass nothing for tests that do
    not exercise the recording path.
    """
    cols, rows = shutil.get_terminal_size(fallback=(_DEFAULT_PTY_COLS, _DEFAULT_PTY_ROWS))
    master_fd, slave_fd = os.openpty()
    os.set_blocking(master_fd, False)
    _set_pty_geometry(slave_fd, rows=rows, cols=cols)
    _set_pty_noncanonical(slave_fd)

    log_writer: MirroredTerminalRecordingWriter | None = None
    interaction_state = persistent_interaction_state(command)
    if recording_path is not None:
        recording_path.parent.mkdir(parents=True, exist_ok=True)
        log_writer = MirroredTerminalRecordingWriter(
            recording_path,
            additional_recording_paths=additional_recording_paths or [],
            mirror_path=mirror_path,
            initial_rows=rows,
            initial_cols=cols,
        )

    try:
        proc = subprocess.Popen(
            command,
            cwd=str(working_dir),
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            # Don't inherit the blocked SIGTERM mask (agent is SIGTERM-stopped).
            preexec_fn=child_signal_reset_preexec(),
        )
    except Exception:
        os.close(master_fd)
        os.close(slave_fd)
        if log_writer is not None:
            log_writer.close()
        raise
    os.close(slave_fd)
    logger.info(
        "Persistent agent session started: cmd=%s pid=%d",
        command[0] if command else "?",
        proc.pid,
    )
    session = PersistentSession(
        proc=proc,
        master_fd=master_fd,
        log_writer=log_writer,
        interaction_state=interaction_state,
        output_observer=interaction_state.observe if interaction_state else None,
    )
    if interaction_state is not None:
        bind_interaction_sender(session, interaction_state)
    return session


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
    session: PersistentSession,
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
            drain_output=lambda: _drain_pty_output(session),
        )
    except PersistentRoundTimeoutError as exc:
        recording_size = _safe_recording_size(session)
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


def _submit_prompt_with_enter(
    session: PersistentSession,
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
        # Submitting anyway is still the best available move — the alternative
        # is failing a round that might yet work — but this is the moment the
        # composer is most likely to strand the text, so it is recorded HERE
        # rather than inferred from a screen replay ten minutes later.
        logger.warning(
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


def send_round(
    session: PersistentSession,
    *,
    prompt: str,
    response_file: Path,
    timeout_seconds: float,
    write_timeout_seconds: float = _DEFAULT_PTY_WRITE_TIMEOUT_SECONDS,
    prompt_acceptance_idle_seconds: float | None = _DEFAULT_PROMPT_ACCEPTANCE_IDLE_SECONDS,
    poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    response_drain_seconds: float = _DEFAULT_RESPONSE_DRAIN_SECONDS,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    role_label: str | None = None,
    response_reader: Callable[[], dict[str, Any] | None] | None = None,
    on_idle_detector: Callable[[RoundIdleDetector], None] | None = None,
) -> dict[str, Any]:
    """Inject ``prompt`` into the persistent agent and wait for its response.

    The response source is selected by ``response_reader``: ``None`` (default)
    polls ``response_file`` on disk (stale files are cleared first); when
    provided, the loop polls that callable (the orchestrator-owned
    ``TurnMailbox``, fed by ``exchange-respond``) and touches no file. The PTY
    pumping, exit/idle detection, and heartbeats are identical either way.

    A response file that exists but does not yet parse as JSON is treated
    as still-being-written: the loop keeps polling until the JSON parses,
    the agent exits, or the deadline expires. Without this tolerance, a
    non-atomic write from the agent (open, write opening brace, flush,
    write the rest) would race the orchestrator and surface as a fatal
    protocol error on the very first poll.

    ``role_label`` is a short tag (``coder`` / ``reviewer``) included in
    log messages so an interleaved coder + reviewer log is decodable
    without cross-referencing PIDs.

    ``write_timeout_seconds`` bounds only the initial prompt write. The
    effective write deadline is capped by ``timeout_seconds`` so a short
    total round timeout remains authoritative.

    ``prompt_acceptance_idle_seconds`` bounds how long a prompted session may
    stay alive without producing any PTY/recording activity or response after
    prompt delivery. This catches the "prompt rendered, agent never engaged"
    failure mode before the full round timeout.

    ``on_idle_detector`` (when supplied) is handed the round's live
    :class:`RoundIdleDetector` once the poll loop owns it, so an out-of-band
    teardown can retain the idle trajectory of a round still running here.

    ``now`` and ``sleep`` are injectable for deterministic tests.
    """
    if session.closed:
        raise PersistentRoundError(
            "Session already closed; cannot send another round",
            failure_reason=RoundFailureReason.SESSION_CLOSED,
        )
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if write_timeout_seconds <= 0:
        raise ValueError("write_timeout_seconds must be positive")
    if (
        prompt_acceptance_idle_seconds is not None
        and prompt_acceptance_idle_seconds <= 0
    ):
        raise ValueError("prompt_acceptance_idle_seconds must be positive or None")
    label = role_label or f"pid={session.proc.pid}"
    payload = prompt.encode("utf-8")
    started_at = now()
    channel = "file" if response_reader is None else "mailbox"
    logger.info(
        "[send_round] start role=%s pid=%d channel=%s response_file=%s "
        "prompt_bytes=%d timeout=%.1fs write_timeout=%.1fs poll_interval=%.2fs",
        label, session.proc.pid, channel, response_file, len(payload),
        timeout_seconds, write_timeout_seconds, poll_interval_seconds,
    )

    prepare_startup_interactions(
        session.interaction_state,
        drain_output=lambda: drain_pty_output_until_quiet(
            session,
            quiet_seconds=0.3,
            now=now,
            sleep=sleep,
        ),
        now=now,
        sleep=sleep,
    )

    # When the mailbox is the channel, nothing is read from or written to
    # ``response_file`` — so there is no stale file to clear and the read is
    # the mailbox poll.
    file_channel = response_reader is None
    if file_channel:
        response_file.unlink(missing_ok=True)
        read_response: Callable[[], dict[str, Any] | None] = (
            lambda: _try_read_response(response_file)
        )
    else:
        read_response = response_reader
    write_deadline = started_at + min(timeout_seconds, write_timeout_seconds)
    written, recovered = _submit_prompt_with_enter(
        session, payload,
        response_file=response_file, write_deadline=write_deadline,
        now=now, sleep=sleep, label=label,
        timeout_seconds=timeout_seconds,
        write_timeout_seconds=write_timeout_seconds,
        read_response=read_response,
    )
    if recovered is not None:
        return recovered
    write_elapsed = now() - started_at
    logger.info(
        "[send_round] prompt written role=%s bytes=%d in %.3fs",
        label, written, write_elapsed,
    )
    return _wait_for_round_response(
        session,
        response_file=response_file,
        started_at=started_at,
        deadline=started_at + timeout_seconds,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        response_drain_seconds=response_drain_seconds,
        prompt_acceptance_idle_seconds=prompt_acceptance_idle_seconds,
        now=now,
        sleep=sleep,
        label=label,
        read_response=read_response,
        file_channel=file_channel,
        on_idle_detector=on_idle_detector,
    )


def _wait_for_round_response(
    session: PersistentSession,
    *,
    response_file: Path,
    started_at: float,
    deadline: float,
    timeout_seconds: float,
    poll_interval_seconds: float,
    response_drain_seconds: float,
    prompt_acceptance_idle_seconds: float | None,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    label: str,
    read_response: Callable[[], dict[str, Any] | None],
    file_channel: bool,
    on_idle_detector: Callable[[RoundIdleDetector], None] | None,
) -> dict[str, Any]:
    """Poll until the response arrives (via ``read_response``), the agent
    exits, or the deadline expires.

    An agent that exits is given one final response-file read — one-shot
    agents legitimately answer and then terminate. Exit without a valid
    response distinguishes "invalid JSON left behind" from "never answered"
    via the round failure reason, which drives the respawn logic upstream.
    """
    # The detector owns the liveness counters (poll iterations, drained bytes,
    # last-activity clock) and the acceptance-window rule. It also keeps the
    # heartbeat-sampled trajectory that kill-evidence capture retains: zero
    # bytes drained over a long interval means the agent never read its prompt,
    # which is the failure mode that hung the #6160 e2e regression.
    detector = RoundIdleDetector(
        window_seconds=prompt_acceptance_idle_seconds,
        deadline_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        round_started_at=started_at,
        activity_since=now(),
        recording_bytes=_safe_recording_size(session),
    )
    if on_idle_detector is not None:
        # Hand the live detector to the caller's kill-evidence registration so
        # a supervisor tearing this exchange down can retain the trajectory of
        # a round that is still wedged here. ``snapshot`` is non-mutating, so
        # the reader never writes to this thread's object.
        on_idle_detector(detector)
    last_heartbeat = now()
    while now() < deadline:
        current = now()
        detector.observe(
            current,
            drained=_drain_pty_output(session),
            recording_bytes=_safe_recording_size(session),
        )
        poll_iter = detector.poll_iterations
        bytes_drained_total = detector.bytes_drained_total
        recording_size = detector.recording_bytes
        parsed = read_response()
        if parsed is not None:
            drain_pty_output_until_quiet(
                session,
                quiet_seconds=response_drain_seconds,
                now=now,
                sleep=sleep,
            )
            logger.info(
                "[send_round] response received role=%s pid=%d in %.1fs "
                "(poll_iters=%d bytes_drained=%d)",
                label, session.proc.pid, now() - started_at,
                poll_iter, bytes_drained_total,
            )
            return parsed
        ret = session.proc.poll()
        if ret is not None:
            final = read_response()
            if final is not None:
                logger.info(
                    "[send_round] response received at exit role=%s pid=%d "
                    "exit_code=%d in %.1fs",
                    label, session.proc.pid, ret, now() - started_at,
                )
                return final
            # "Invalid JSON left behind" is a file-channel concept only. In
            # mailbox mode the response file is not the channel, so a stale or
            # legacy file an agent leaves behind must NOT downgrade the round to
            # the non-respawnable INVALID_RESPONSE — an exit without a mailbox
            # delivery is a process that exited before responding (respawnable),
            # honouring the fail-safe contract that a forgotten exchange-respond
            # degrades to retry, never a wrong/missing classification.
            if file_channel and response_file.exists():
                logger.warning(
                    "[send_round] agent exited with invalid JSON role=%s pid=%d "
                    "exit_code=%d response_file=%s",
                    label, session.proc.pid, ret, response_file,
                )
                raise PersistentRoundError(
                    f"Agent exited (code={ret}) leaving invalid JSON in {response_file}",
                    failure_reason=RoundFailureReason.INVALID_RESPONSE,
                    idle_trace=detector.snapshot(now()),
                )
            logger.warning(
                "[send_round] agent exited before responding role=%s pid=%d "
                "exit_code=%d after %.1fs (poll_iters=%d bytes_drained=%d)",
                label, session.proc.pid, ret, now() - started_at,
                poll_iter, bytes_drained_total,
            )
            raise PersistentRoundError(
                f"Agent exited unexpectedly (code={ret}) before responding",
                failure_reason=RoundFailureReason.PROCESS_EXITED_BEFORE_RESPONSE,
                idle_trace=detector.snapshot(now()),
            )
        idle_at = now()
        idle_for = detector.idle_for(idle_at)
        if detector.acceptance_window_exhausted(idle_at):
            logger.warning(
                "[send_round] prompt not accepted role=%s pid=%d after %.1fs idle "
                "(elapsed=%.1fs poll_iters=%d bytes_drained=%d "
                "response_file_exists=%s recording_bytes=%s)",
                label,
                session.proc.pid,
                idle_for,
                now() - started_at,
                poll_iter,
                bytes_drained_total,
                response_file.exists(),
                recording_size if recording_size is not None else "n/a",
            )
            raise PersistentRoundTimeoutError(
                "Agent did not produce terminal output or a response after "
                f"prompt delivery for {idle_for:.1f}s",
                failure_reason=RoundFailureReason.PROMPT_NOT_ACCEPTED,
                idle_trace=detector.snapshot(idle_at),
            )
        if now() - last_heartbeat >= _SEND_ROUND_HEARTBEAT_SECONDS:
            logger.info(
                "[send_round] heartbeat role=%s pid=%d alive=%s elapsed=%.0fs "
                "deadline_in=%.0fs poll_iters=%d bytes_drained=%d "
                "idle_for=%.0fs response_file_exists=%s recording_bytes=%s",
                label, session.proc.pid,
                session.proc.poll() is None,
                now() - started_at, deadline - now(),
                poll_iter, bytes_drained_total,
                idle_for,
                response_file.exists(),
                recording_size if recording_size is not None else "n/a",
            )
            detector.record_sample(now())
            last_heartbeat = now()
        sleep(poll_interval_seconds)
    logger.warning(
        "[send_round] timeout role=%s pid=%d after %.1fs "
        "(poll_iters=%d bytes_drained=%d response_file_exists=%s)",
        label, session.proc.pid, timeout_seconds,
        detector.poll_iterations, detector.bytes_drained_total,
        response_file.exists(),
    )
    raise PersistentRoundTimeoutError(
        f"Agent did not produce valid JSON in {response_file} within {timeout_seconds}s",
        idle_trace=detector.snapshot(now()),
    )


def _safe_recording_size(session: PersistentSession) -> int | None:
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


def _try_read_response(response_file: Path) -> dict[str, Any] | None:
    """Return the parsed JSON if the file exists and parses, else None.

    A returned ``None`` covers both "file not yet present" and "file
    present but the writer hasn't finished a complete JSON document yet"
    — both cases call for continued polling rather than escalation.
    """
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
    if not isinstance(parsed, dict):
        return None
    return parsed


def close_persistent_session(
    session: PersistentSession,
    *,
    grace_seconds: float = _DEFAULT_TERMINATE_GRACE_SECONDS,
) -> int | None:
    """Send SIGTERM to the agent's process group, then SIGKILL on grace expiry.

    Returns the exit code if reaped, ``None`` if the process refused to
    die. The master fd and log writer are closed regardless.
    """
    if session.closed:
        return session.proc.returncode
    try:
        if session.proc.poll() is None:
            try:
                os.killpg(os.getpgid(session.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            try:
                session.proc.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(session.proc.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    session.proc.wait(timeout=grace_seconds)
                except subprocess.TimeoutExpired:
                    logger.warning(
                        "Persistent agent (pid=%d) did not exit after SIGKILL",
                        session.proc.pid,
                    )
        # Final drain so any tail output makes it into the recording before
        # we close the writer.
        _drain_pty_output(session)
    finally:
        session.closed = True
        try:
            os.close(session.master_fd)
        except OSError:
            pass
        if session.log_writer is not None:
            session.log_writer.close()
    return session.proc.returncode


def _drain_pty_output(session: PersistentSession) -> int:
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


def _set_pty_geometry(slave_fd: int, *, rows: int, cols: int) -> None:
    try:
        size = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, size)
    except OSError:
        logger.warning("Failed to seed persistent-round PTY geometry", exc_info=True)


def _set_pty_noncanonical(slave_fd: int) -> None:
    """Avoid canonical line-buffer limits for orchestrator-driven PTY input."""
    try:
        attrs = termios.tcgetattr(slave_fd)
        attrs[3] &= ~(termios.ICANON | termios.ECHO)
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
    except OSError:
        logger.warning(
            "Failed to seed persistent-round PTY input mode", exc_info=True
        )
