"""Translate a contained command status into the current entrypoint status."""

from __future__ import annotations

import os
import signal


def forward_command_exit_status(return_code: int) -> int:
    """Return a normal exit code or terminate this wrapper by the same signal."""
    if type(return_code) is not int:
        raise ValueError("command return code must be an integer")
    if return_code >= 0:
        return return_code

    signal_number = -return_code
    try:
        command_signal = signal.Signals(signal_number)
    except ValueError as error:
        raise ValueError(
            f"command return code {return_code} names no process signal"
        ) from error
    if command_signal is signal.SIGSTOP:
        raise ValueError("SIGSTOP cannot be a terminal command status")

    if command_signal is not signal.SIGKILL:
        signal.signal(command_signal, signal.SIG_DFL)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {command_signal})
    os.kill(os.getpid(), command_signal)
    raise RuntimeError(
        f"process remained alive after forwarding terminal signal {command_signal.name}"
    )
