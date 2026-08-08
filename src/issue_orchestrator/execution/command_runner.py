"""Local command runner adapter."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from ..infra.shutdown_signals import child_signal_reset_preexec
from ..ports.command_runner import CommandResult, OutputNewlines

logger = logging.getLogger(__name__)


class LocalCommandRunner:
    """Executes commands locally using subprocess."""

    def run(
        self,
        command: str | list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        shell: bool = False,
        newlines: OutputNewlines = OutputNewlines.TRANSLATED,
    ) -> CommandResult:
        logger.debug("Running command: %s", command)
        try:
            result = subprocess.run(
                command,
                cwd=str(cwd) if cwd else None,
                capture_output=True,
                # Text mode is where universal-newline translation happens, so
                # a byte-exact capture has to decode the output itself.
                text=not newlines.capture_bytes,
                timeout=timeout_seconds,
                env=env,
                shell=shell,
                # Children must not inherit the orchestrator's blocked
                # SIGTERM/SIGINT mask (None where signals were never blocked, so
                # this is a no-op on macOS/Windows). See infra.shutdown_signals.
                preexec_fn=child_signal_reset_preexec(),
            )
            return CommandResult(
                returncode=result.returncode,
                stdout=newlines.decode(result.stdout),
                stderr=newlines.decode(result.stderr),
                timed_out=False,
            )
        except subprocess.TimeoutExpired as exc:
            # A timed-out capture can end mid-character, and its output is only
            # ever diagnostic, so replace here instead of losing the result.
            return CommandResult(
                returncode=-1,
                stdout=newlines.decode(exc.stdout, errors="replace"),
                stderr=newlines.decode(exc.stderr, errors="replace"),
                timed_out=True,
            )
        except Exception as exc:
            logger.exception("Command execution failed")
            return CommandResult(
                returncode=-1,
                stdout="",
                stderr=str(exc),
                timed_out=False,
            )
