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
from .composer_readiness import LiveComposerScreen
from .persistent_round_io import drain_pty_output_until_quiet
from .persistent_round_write import (
    PromptDeliveryBudget,
    drain_pty_output,
    safe_recording_size,
    submit_prompt_with_enter,
)
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

# Heartbeat cadence for the ``send_round`` poll loop. Without this, a
# wedged agent shows up as 17 minutes of total log silence (#6160 e2e
# regression). The heartbeat logs the deadline countdown, recording
# growth, and the agent's process state so the next reproduction tells
# us *which* step is wedged instead of just "something hung."
_SEND_ROUND_HEARTBEAT_SECONDS = 30.0

# Readiness gate before typing a turn into a live TUI (#7104).
#
# The wait is on the EVENT — the rendered screen showing an idle agent and an
# empty composer — and these bound it. Sized against a measured codex 0.153.4
# bootstrap turn, whose busy footer cleared at 38.4s: the agent runs its
# argv-delivered setup prompt before the first round is injected, and that is
# the window this exists to cover.


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
    #: Live rendered screen used to decide whether the agent will accept
    #: typing. None for sessions built by tests that never open a PTY.
    composer_screen: LiveComposerScreen | None = None
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
    # The readiness screen rides the output the runner already reads, so it
    # costs one grid update per chunk and never opens a second reader on the
    # PTY. Chained ahead of the interaction observer rather than replacing it.
    #
    # Only for the TUI shapes, keyed off the same command detection that
    # decides whether a session has startup interactions at all: those rules
    # exist for interactive claude and interactive codex, which are exactly
    # the agents that HAVE a composer to strand a prompt in. A plain
    # stdin-reading agent has none, and gating it would add the readiness
    # grace period to every round for no benefit — which is what made 14
    # round-runner tests time out when this applied unconditionally.
    composer_screen = (
        LiveComposerScreen(rows=rows, cols=cols)
        if interaction_state is not None
        else None
    )

    def _observe(chunk: bytes) -> None:
        if composer_screen is not None:
            composer_screen.feed(chunk)
        if interaction_state is not None:
            interaction_state.observe(chunk)

    session = PersistentSession(
        proc=proc,
        master_fd=master_fd,
        log_writer=log_writer,
        interaction_state=interaction_state,
        output_observer=_observe,
        composer_screen=composer_screen,
    )
    if interaction_state is not None:
        bind_interaction_sender(session, interaction_state)
    return session


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
    # The round deadline is the only clock readiness may consume; the write
    # allowance starts when writing does (F1). Previously this precomputed a
    # write deadline that the readiness wait could exhaust before any byte
    # was written.
    delivery_budget = PromptDeliveryBudget(
        round_deadline=started_at + timeout_seconds,
        write_allowance_seconds=write_timeout_seconds,
    )
    written, recovered = submit_prompt_with_enter(
        session, payload,
        response_file=response_file, budget=delivery_budget,
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
        recording_bytes=safe_recording_size(session),
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
            drained=drain_pty_output(session),
            recording_bytes=safe_recording_size(session),
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
        drain_pty_output(session)
    finally:
        session.closed = True
        try:
            os.close(session.master_fd)
        except OSError:
            pass
        if session.log_writer is not None:
            session.log_writer.close()
    return session.proc.returncode


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
