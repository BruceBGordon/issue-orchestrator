"""Owner for running a live CLI under a timeout without leaking its process tree.

``subprocess.run(..., timeout=..., start_new_session=True)`` is not safe for the
live-agent probes. On timeout it kills and reaps only the process object — the
session leader — and never signals the new session's process group. An agent CLI
spawns tool subprocesses (Bash, git, a network probe), and those descendants
survive the killed leader. They keep running, and they keep writing.

For a security-boundary probe that is a correctness bug, not untidiness: a
surviving grandchild can create a result file *after* the harness has snapshotted
and reset the previous attempt's evidence, and the retry then inherits it (see
``tests/sandbox_probe_retry``).

Termination here is established by the kernel, never inferred from the pipes.
Pipe EOF only proves the session leader was reaped and every *inherited* write
end was closed; a descendant may ignore ``SIGTERM`` and redirect its stdout and
stderr elsewhere, at which point the pipes reach EOF while it is still alive and
still able to write. So ``SIGKILL`` is sent to the whole group
**unconditionally** — not "if the group still looks alive" — and it is sent
while the leader is still unreaped. Two properties follow:

* ``SIGKILL`` cannot be caught, blocked, or ignored, so once ``killpg``
  returns, no member of that group executes another instruction of user code.
* An unreaped leader keeps its pid, and therefore the pgid, reserved. Signalling
  before the reap means the pgid unambiguously names *our* group.

That second point is why this module does not poll the group for liveness after
reaping. Once the leader is reaped the pgid becomes a recyclable resource, so a
post-reap probe is both unreliable and dangerous: it can report a stranger's
recycled group as our live descendant, and then ``SIGKILL`` it.

``ESRCH`` and ``EPERM`` from a group signal are both accepted as "nothing live
left to signal here". ``EPERM`` in particular is the *normal* end state on
macOS, where a group whose only remaining member is the unreaped zombie leader
reports ``EPERM`` rather than ``ESRCH`` — treating it as an error would fail the
ordinary cooperative path on the platform these probes run on.

Known limit: a descendant that leaves the process group on purpose (``setsid``)
is unreachable by ``killpg`` and is not covered here.

POSIX only — ``os.killpg`` has no Windows equivalent. Rather than degrade
quietly there, :func:`run_in_process_group` refuses before it spawns anything:
see :func:`supports_process_groups` and :class:`ProcessGroupUnsupportedError`.
That refusal is asserted behavior, not skipped coverage — the unit suite pins it
on every platform.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

# Courtesy window: how long a signalled group may take to wind down on SIGTERM
# before SIGKILL is sent anyway. This is politeness, not correctness — cleanup
# does not depend on anything happening during it.
TERMINATE_GRACE_SECONDS = 5.0
# How long the killed group may take to release the pipes and exit. Correctness
# does depend on this one: exceeding it means something survived SIGKILL.
KILL_GRACE_SECONDS = 10.0

# Gap between polls while waiting for the leader to exit. There is no ack
# channel from the kernel for "this process exited", so a bounded poll is the
# available observation; ``tests/AGENTS.md`` permits bounded waits on real
# external systems.
_POLL_SECONDS = 0.01


class ProcessGroupCleanupError(AssertionError):
    """Raised when a timed-out process group could not be proven dead.

    If anything may still be running, whatever the caller then reads off the
    filesystem may still be changing, so no result from that run can be trusted.
    Subclasses ``AssertionError`` so pytest reports it as a failure rather than
    an infrastructure error.
    """


class ProcessGroupUnsupportedError(NotImplementedError):
    """Raised when this platform cannot contain a timed-out process tree.

    Refusing is the point. The containment guarantee here rests entirely on
    POSIX process groups, and a probe that ran without it would look like it
    passed while leaving descendants free to write evidence after the harness
    moved on. A loud refusal is the honest outcome; a quiet degraded run is not.
    """


def supports_process_groups() -> bool:
    """Whether this platform has the primitives the containment guarantee needs.

    ``os.killpg`` signals a whole group and ``os.waitid`` observes the leader's
    exit without reaping it — the two operations the timeout path is built on.
    Neither exists on native Windows.
    """
    return hasattr(os, "killpg") and hasattr(os, "waitid")


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    """Signal every live member of ``pgid``.

    ``ESRCH`` means the group is gone; ``EPERM`` means no live member could be
    signalled, which on macOS is what a group holding only the zombie leader
    reports. Neither leaves anything for us to do.
    """
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        return


def _await_leader_exit(process: subprocess.Popen[str], *, grace_seconds: float) -> None:
    """Wait up to ``grace_seconds`` for the leader to exit, without reaping it.

    ``WNOWAIT`` leaves the child in a waitable state, so the leader stays a
    zombie and its pid — and with it the pgid — stays reserved for the SIGKILL
    that follows. Returning early is only an optimisation: the caller signals
    unconditionally either way.
    """
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        try:
            info = os.waitid(
                os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG
            )
        except ChildProcessError:
            return
        if info is not None and info.si_pid != 0:
            return
        time.sleep(_POLL_SECONDS)


def _terminate_group(
    process: subprocess.Popen[str],
    *,
    cmd: Sequence[str],
    terminate_grace_seconds: float,
    kill_grace_seconds: float,
) -> tuple[str | None, str | None]:
    """Kill the whole process group, then drain it."""
    # ``start_new_session=True`` makes the child both session and process-group
    # leader, so its pid is the pgid.
    pgid = process.pid

    _signal_group(pgid, signal.SIGTERM)
    _await_leader_exit(process, grace_seconds=terminate_grace_seconds)

    # Unconditional, and before any reap. This is the whole guarantee: it does
    # not matter whether the pipes went quiet, whether the leader cooperated, or
    # whether a descendant is ignoring signals.
    _signal_group(pgid, signal.SIGKILL)

    try:
        return process.communicate(timeout=kill_grace_seconds)
    except subprocess.TimeoutExpired as exc:
        raise ProcessGroupCleanupError(
            f"process group for {list(cmd)!r} still held the output pipes "
            f"{kill_grace_seconds}s after SIGKILL; something may still be "
            "writing, so this run's on-disk evidence cannot be trusted"
        ) from exc


def run_in_process_group(
    cmd: Sequence[str],
    *,
    cwd: Path | str | None = None,
    timeout: float,
    env: Mapping[str, str] | None = None,
    terminate_grace_seconds: float = TERMINATE_GRACE_SECONDS,
    kill_grace_seconds: float = KILL_GRACE_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` in its own process group, capturing output.

    Args:
        terminate_grace_seconds: courtesy window between ``SIGTERM`` and the
            unconditional ``SIGKILL``.
        kill_grace_seconds: how long the killed group may take to release the
            pipes before cleanup is declared failed.

    Raises:
        ProcessGroupUnsupportedError: if this platform has no process groups.
            Checked before anything is spawned, so a caller that ignores it
            never gets a running child it cannot contain.
        subprocess.TimeoutExpired: after the whole process group has been
            killed and drained. Carries whatever output was captured.
        ProcessGroupCleanupError: if the group could not be proven dead.
    """
    if not supports_process_groups():
        raise ProcessGroupUnsupportedError(
            f"cannot run {list(cmd)!r} under containment: this platform has no "
            "os.killpg/os.waitid, so a timed-out process tree could not be "
            "killed and its descendants could keep writing after the timeout"
        )
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
        stdout, stderr = _terminate_group(
            process,
            cmd=cmd,
            terminate_grace_seconds=terminate_grace_seconds,
            kill_grace_seconds=kill_grace_seconds,
        )
        raise subprocess.TimeoutExpired(
            cmd=list(cmd),
            timeout=timeout,
            output=stdout or exc.stdout,
            stderr=stderr or exc.stderr,
        ) from exc
    return subprocess.CompletedProcess(list(cmd), process.returncode, stdout, stderr)
