"""PTY-based agent runner — pexpect with raw terminal recording.

This runner creates a pexpect PTY, records raw output for replay,
applies environment filtering, and enforces timeouts.

Architecture:
    AgentRunner.start(spec)  →  AgentSession (handle)
    AgentRunner.run(spec)    →  AgentResult  (start + wait + retry, inherited from base)

All callers — coding sessions, review exchange rounds, simulated tests —
use the same code path. No subprocess.run, no raw pexpect.spawn elsewhere.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import signal
import time
from pathlib import Path
from typing import Protocol, cast

import pexpect

from issue_orchestrator.execution.agent_runner_base import (
    BaseAgentRunner,
    _pty_preexec,
)
from issue_orchestrator.execution.agent_runner_env import build_filtered_env
from issue_orchestrator.execution.session_interactions import SessionInteractionHandler
from issue_orchestrator.execution.agent_runner_types import (
    AgentResult,
    AgentSpec,
    RetryPolicy,
    _format_command_for_log,
)
from issue_orchestrator.infra.terminal_recording import MirroredTerminalRecordingWriter

logger = logging.getLogger(__name__)
_DEFAULT_PTY_COLS = 120
_DEFAULT_PTY_ROWS = 40

# Re-export types so existing ``from execution.agent_runner import ...`` still works.
__all__ = [
    "AgentResult",
    "AgentRunner",
    "AgentSession",
    "AgentSpec",
    "RetryPolicy",
]

_GRACEFUL_KILL_TIMEOUT = 5


class _PtyFile(Protocol):
    def close(self) -> None: ...


class _PtyProcessInternals(Protocol):
    fileobj: _PtyFile
    fd: int
    closed: bool


class _PexpectSpawnInternals(Protocol):
    ptyproc: _PtyProcessInternals


class AgentSession:
    """Handle to a running agent process.

    Returned by :meth:`AgentRunner.start`.  Callers either poll
    :meth:`is_alive` across ticks or block with :meth:`wait`.
    """

    def __init__(
        self,
        child: pexpect.spawn,
        log_writer: MirroredTerminalRecordingWriter | None,
        spec: AgentSpec,
        start_time: float,
        interaction_handler: SessionInteractionHandler | None = None,
    ) -> None:
        if type(child.pid) is not int or child.pid <= 1:
            raise ValueError(
                "AgentSession child must have a process id above 1"
            )
        self._child = child
        self._process_id = child.pid
        self._log_writer = log_writer
        self._spec = spec
        self._start_time = start_time
        self._closed = False
        self._interaction_handler = interaction_handler
        if self._interaction_handler is not None:
            self._interaction_handler.bind_sender(self.send)

    @property
    def pid(self) -> int:
        return self._process_id

    def send(self, text: str) -> bool:
        """Send text to the agent's PTY stdin.

        Used by SubprocessPlugin.send_to_session() to relay interactive input.
        Returns False if the session is already closed or the send fails.
        """
        if self._closed:
            return False
        try:
            self._child.sendline(text)
            return True
        except Exception:  # noqa: BLE001
            return False

    def is_alive(self) -> bool:
        """Check whether the agent process is still running."""
        if self._closed:
            return False
        try:
            return self._child.isalive()
        except (ChildProcessError, OSError):
            return False

    def wait(self, timeout: float | None = None) -> AgentResult:
        """Block until the agent finishes or *timeout* seconds elapse.

        On timeout the process group is killed (SIGTERM → grace → SIGKILL).
        Always closes the PTY and flushes the log writer before returning.
        """
        timed_out = False
        try:
            self._child.expect(pexpect.EOF, timeout=timeout)
        except pexpect.TIMEOUT:
            timed_out = True
            logger.warning(
                "Agent timed out after %ss, terminating",
                timeout,
            )
            self.kill()
        except pexpect.ExceptionPexpect:
            # Covers unexpected pexpect errors (e.g. child already closed)
            pass

        return self._close(timed_out=timed_out)

    def kill(self) -> None:
        """Terminate the agent's process group (SIGTERM → grace → SIGKILL)."""
        if self._closed:
            return
        pid = self._child.pid
        if pid is None:
            return
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return

        # Wait briefly for graceful termination
        deadline = time.monotonic() + _GRACEFUL_KILL_TIMEOUT
        while time.monotonic() < deadline:
            if not self.is_alive():
                return
            time.sleep(0.1)

        # Force kill
        logger.warning("Agent did not terminate gracefully, using SIGKILL")
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

    def finalize_after_owned_process_group_reap(self) -> None:
        """Close PTY and log descriptors after another owner reaped the child.

        This is deliberately separate from :meth:`wait`: calling pexpect's
        normal close path after ``waitpid`` has already run raises while trying
        to reap the same child.  The startup lifecycle owner uses this method
        only after its process-group supervisor has provided typed reaping
        evidence.
        """
        if self._closed:
            raise RuntimeError("AgentSession is already finalized")
        self._closed = True
        close_errors: list[BaseException] = []
        try:
            self._child.flush()
        except BaseException as error:
            close_errors.append(error)
        pty_process = cast(_PexpectSpawnInternals, self._child).ptyproc
        try:
            pty_process.fileobj.close()
        except BaseException as error:
            close_errors.append(error)
        finally:
            pty_process.fd = -1
            pty_process.closed = True
            self._child.child_fd = -1
            self._child.closed = True
        if self._log_writer is not None:
            try:
                self._log_writer.close()
            except BaseException as error:
                close_errors.append(error)
        if close_errors:
            raise BaseExceptionGroup(
                "could not finalize externally reaped agent session",
                close_errors,
            )

    def _close(self, *, timed_out: bool) -> AgentResult:
        """Close the PTY, flush the log, return the result."""
        if self._closed:
            return AgentResult(
                exit_code=None,
                timed_out=timed_out,
                duration_seconds=time.monotonic() - self._start_time,
                stderr="session already closed",
                command=self._spec.command,
            )

        self._closed = True
        duration = time.monotonic() - self._start_time

        finalization_errors: list[Exception] = []
        try:
            self._child.close(force=True)
        except (OSError, pexpect.ExceptionPexpect) as error:
            finalization_errors.append(error)

        if self._log_writer is not None:
            try:
                self._log_writer.close()
            except OSError as error:
                finalization_errors.append(error)
        if finalization_errors:
            raise ExceptionGroup(
                "agent session PTY or recording finalization failed",
                finalization_errors,
            )

        exit_code = self._child.exitstatus
        stderr = ""
        # If bash couldn't find the command, exit code is 127
        if exit_code == 127:
            stderr = f"Command not found: {self._spec.command[0]}"
        elif exit_code == 126:
            stderr = f"Permission denied: {self._spec.command[0]}"

        logger.info(
            "Agent finished: exit_code=%s, timed_out=%s, duration=%.1fs",
            exit_code,
            timed_out,
            duration,
        )

        return AgentResult(
            exit_code=exit_code,
            timed_out=timed_out,
            duration_seconds=duration,
            stderr=stderr,
            command=self._spec.command,
        )


class AgentRunner(BaseAgentRunner):
    """PTY-based agent runner using pexpect + CleaningLogWriter.

    Two usage modes (same underlying mechanism):

    **Async** — for long-running sessions::

        session = runner.start(spec)
        while session.is_alive():
            # ... do other work ...
        result = session.wait()

    **Sync** — for single-shot execution with optional retry::

        result = runner.run(spec)  # blocks until done
    """

    def start(
        self,
        spec: AgentSpec,
        interaction_handler: SessionInteractionHandler | None = None,
    ) -> AgentSession:
        """Start an agent in a PTY. Returns a session handle.

        The agent runs in a pexpect PTY with:
        - Raw PTY recording via MirroredTerminalRecordingWriter → spec.log_path
        - Filtered environment (credentials scrubbed, overrides applied)
        - Process group isolation (for clean termination)
        - SIGTTIN/SIGTTOU immunity via preexec_fn

        The caller is responsible for calling :meth:`AgentSession.wait`
        or :meth:`AgentSession.kill` when done.
        """
        spec.output_dir.mkdir(parents=True, exist_ok=True)
        if spec.log_path is not None:
            spec.log_path.parent.mkdir(parents=True, exist_ok=True)

        env = build_filtered_env(
            scrub_vars=spec.env_scrub if spec.env_scrub else None,
            passthrough_vars=spec.env_passthrough if spec.env_passthrough else None,
            overrides=spec.env_overrides,
        )

        logger.info(
            "Starting agent: %s in %s (timeout: %ds)",
            spec.command[0],
            spec.working_dir,
            spec.timeout_seconds,
        )
        logger.info("Agent argv: %s", _format_command_for_log(spec.command))

        cols, rows = shutil.get_terminal_size(fallback=(_DEFAULT_PTY_COLS, _DEFAULT_PTY_ROWS))
        log_writer = None
        if spec.log_path is not None:
            log_writer = MirroredTerminalRecordingWriter(
                spec.log_path,
                mirror_path=spec.mirror_log_path,
                on_output=interaction_handler.on_output if interaction_handler is not None else None,
                initial_rows=rows,
                initial_cols=cols,
            )

        shell_cmd = shlex.join(spec.command)
        child = pexpect.spawn(
            "/bin/bash",
            ["-c", shell_cmd],
            cwd=str(spec.working_dir),
            env=env,
            logfile=log_writer,
            timeout=None,
            preexec_fn=_pty_preexec,
            dimensions=(rows, cols),
        )

        return AgentSession(
            child,
            log_writer,
            spec,
            time.monotonic(),
            interaction_handler=interaction_handler,
        )

    def run_interactive(self, spec: AgentSpec, response_file: Path) -> AgentResult:
        """Run an interactive agent round without PTY/fork.

        Unlike :meth:`start` which uses pexpect (fork-based), this delegates
        to a Popen-based runner that is safe from multi-threaded processes
        (uvicorn + SSE threads).  Used by the review exchange loop.
        """
        from .interactive_round import run_interactive_round

        return run_interactive_round(spec, response_file)

    def _execute_once(self, spec: AgentSpec, *, attempt: int) -> AgentResult:
        """Execute a single attempt via pexpect PTY."""
        session = self.start(spec)
        return session.wait(timeout=spec.timeout_seconds)
