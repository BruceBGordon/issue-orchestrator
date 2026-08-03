"""Owner for running a live CLI under a timeout without leaking its process tree.

``subprocess.run(..., timeout=..., start_new_session=True)`` is not safe for the
live-agent probes. On timeout it kills and reaps only the process object — the
session leader — and never signals the new session's process group. An agent CLI
spawns tool subprocesses (Bash, git, a network probe), and those descendants
survive the killed leader. They keep running, and they keep writing.

For a security-boundary probe that is a correctness bug, not untidiness: a
surviving grandchild can create a result file *after* the harness has snapshotted
and reset the previous attempt's evidence, and the retry then inherits it (see
``tests/sandbox_probe_retry``). :func:`run_in_process_group` closes that by
raising ``TimeoutExpired`` only once the entire process group is dead and
drained, so nothing can still be writing when the caller inspects the filesystem.

POSIX only — ``os.killpg`` has no Windows equivalent. The probes that use this
already skip on native Windows.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

# How long to let a signalled process group drain before escalating, and then
# before giving up entirely.
TERMINATE_GRACE_SECONDS = 5.0
KILL_GRACE_SECONDS = 10.0


class ProcessGroupCleanupError(AssertionError):
    """Raised when a timed-out process group could not be fully killed.

    Deliberately loud: if descendants are still alive, anything the caller then
    reads off the filesystem may still be changing, so no result from that run
    can be trusted. Subclasses ``AssertionError`` so pytest reports it as a
    failure rather than an infrastructure error.
    """


def _signal_group(process: subprocess.Popen[str], sig: signal.Signals) -> None:
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        # Already gone, or already reaped — nothing left to signal.
        return


def _drain_after_timeout(
    process: subprocess.Popen[str],
    *,
    cmd: Sequence[str],
) -> tuple[str | None, str | None]:
    """Kill the whole process group and wait for every descendant to exit.

    ``communicate`` returns once every holder of the stdout/stderr write ends
    has closed them. Descendants inherit those pipes, so a successful drain is
    itself evidence that the tree is gone.
    """
    _signal_group(process, signal.SIGTERM)
    try:
        return process.communicate(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    _signal_group(process, signal.SIGKILL)
    try:
        return process.communicate(timeout=KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise ProcessGroupCleanupError(
            f"process group for {list(cmd)!r} survived SIGKILL; descendants may "
            "still be writing, so this run's on-disk evidence cannot be trusted"
        ) from exc


def run_in_process_group(
    cmd: Sequence[str],
    *,
    cwd: Path | str | None = None,
    timeout: float,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` in its own process group, capturing output.

    Raises:
        subprocess.TimeoutExpired: after the whole process group has been
            killed and drained. Carries whatever output was captured.
        ProcessGroupCleanupError: if the group could not be killed at all.
    """
    process = subprocess.Popen(  # noqa: S603
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=None if cwd is None else str(cwd),
        env=None if env is None else dict(env),
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout, stderr = _drain_after_timeout(process, cmd=cmd)
        raise subprocess.TimeoutExpired(
            cmd=list(cmd),
            timeout=timeout,
            output=stdout or exc.stdout,
            stderr=stderr or exc.stderr,
        ) from exc
    return subprocess.CompletedProcess(list(cmd), process.returncode, stdout, stderr)
