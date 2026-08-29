"""Load and fixture processes that cannot outlive the run that made them.

Incident 2026-08-29 (#7142): a verification shell spawned twenty
``python -c 'while True: pass'`` burners to reproduce a timing effect and never
reaped them. They orphaned to launchd and spun at ~87% CPU for nine hours.
Seven test-web gate flakes across four unrelated branches landed inside that
window, gate wall time went from ~90s to 350s+, and the cause was misattributed
twice. A sweep then found nine older orphaned fixtures -- ``signal.pause``
waiters from kill-escalation tests, deliberately TERM-resistant, some three
days old -- plus a stray sleep loop. The cost of leaked load lands on whoever
runs the *next* gate, who has no way to see it.

The rule, for committed tests and for ad-hoc verification alike:

1. Own the group. Every spawned process is a session leader
   (``start_new_session=True``), so one signal reaches it and everything it
   spawns.
2. Reap in ``finally``. Not after the asserts, not in ``except TimeoutExpired``
   -- a failing assert is exactly when the fixture is still alive.
3. Escalate. SIGTERM, a short grace, then SIGKILL, unconditionally. Fixtures
   worth writing are often deliberately TERM-immune; the escalation is the
   point, not a fallback.
4. Self-limit. Load here dies on its own deadline even if this process is
   SIGKILLed mid-run. A ``while True: pass`` burner has no such floor, which is
   why nine hours was possible.

For load this module spawns, use :func:`cpu_load`. For a tree the system under
test spawned -- where the pgid is not ours to know -- use
:func:`reap_marked_processes`.

POSIX only, deliberately: see ``tests/process_group_run``, which owns the group
signalling and the no-reap-before-kill ordering this module reuses.
"""

from __future__ import annotations

import argparse
import math
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from tests.process_group_run import (
    ProcessGroupUnsupportedError,
    await_exit_without_reaping,
    signal_group,
    supports_process_groups,
)

# Courtesy window before the unconditional SIGKILL. Short on purpose: load
# fixtures have nothing to flush, and every second here is a second the next
# gate might be paying for.
TERMINATE_GRACE_SECONDS = 2.0
# How long a killed process may take to disappear. Exceeding this is a real
# failure — something survived SIGKILL, or is not the process we think it is.
KILL_GRACE_SECONDS = 10.0

_POLL_SECONDS = 0.05
_PS_TIMEOUT_SECONDS = 15.0

# Burns a core, then exits. The deadline is what makes an escaped burner
# survivable: it caps the blast radius at max_lifetime_seconds even if nothing
# ever signals it.
_BURN_SCRIPT = (
    "import sys, time\n"
    "deadline = time.monotonic() + float(sys.argv[1])\n"
    "while time.monotonic() < deadline:\n"
    "    pass\n"
)


class LeakedProcessError(AssertionError):
    """Raised when a spawned process could not be proven dead.

    Subclasses ``AssertionError`` so pytest reports the leak as a test failure.
    A run that leaks load has poisoned the machine for every later run on it,
    which is a result worth failing over.
    """


@contextmanager
def cpu_load(*, workers: int, max_lifetime_seconds: float) -> Iterator[tuple[int, ...]]:
    """Run ``workers`` CPU burners for the body, then prove they are gone.

    Each burner is its own session leader, so its pid is its pgid and the
    yielded pids can be signalled as groups. Every burner also exits on its own
    after ``max_lifetime_seconds`` — pick the shortest window the test can
    tolerate, because that number is the worst case if this process dies
    without running its cleanup.

    Yields:
        The burner pids, which are also their process group ids.

    Raises:
        ProcessGroupUnsupportedError: before anything is spawned, if this
            platform cannot contain what would be spawned.
        LeakedProcessError: if a burner outlived its SIGKILL.
    """
    if not supports_process_groups():
        raise ProcessGroupUnsupportedError(
            "refusing to spawn CPU load: this platform has no os.killpg, so "
            "the burners could not be guaranteed dead afterwards"
        )
    if workers < 1:
        raise ValueError(f"workers must be at least 1, got {workers}")
    # Finiteness first, and not as a formality: ``inf`` and ``nan`` both slip
    # past ``<= 0`` (``nan`` compares false against everything), and an
    # inf-lifetime burner is exactly the immortal `while True: pass` this
    # helper exists to make unspawnable. It survives a SIGKILLed harness.
    if not math.isfinite(max_lifetime_seconds) or max_lifetime_seconds <= 0:
        raise ValueError(
            "max_lifetime_seconds must be a positive finite number, got "
            f"{max_lifetime_seconds!r}"
        )

    processes: list[subprocess.Popen[bytes]] = []
    try:
        for _ in range(workers):
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", _BURN_SCRIPT, str(max_lifetime_seconds)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            )
        yield tuple(process.pid for process in processes)
    finally:
        reap_process_groups(processes)


def reap_process_groups(
    processes: Sequence[subprocess.Popen[bytes]],
    *,
    terminate_grace_seconds: float = TERMINATE_GRACE_SECONDS,
    kill_grace_seconds: float = KILL_GRACE_SECONDS,
) -> None:
    """Kill and reap the process groups led by ``processes``.

    Both signals are sent to every group before anything is reaped: an unreaped
    leader keeps its pid, and therefore the pgid, reserved, so the SIGKILL
    cannot land on a stranger's recycled group.

    Raises:
        LeakedProcessError: if a leader outlived its SIGKILL.
    """
    for process in processes:
        signal_group(process.pid, signal.SIGTERM)
    for process in processes:
        await_exit_without_reaping(process.pid, grace_seconds=terminate_grace_seconds)
    for process in processes:
        signal_group(process.pid, signal.SIGKILL)
    for process in processes:
        try:
            process.wait(timeout=kill_grace_seconds)
        except subprocess.TimeoutExpired as exc:
            raise LeakedProcessError(
                f"pid {process.pid} survived SIGKILL to its process group for "
                f"{kill_grace_seconds}s; it is now load on this machine that "
                "no later run can attribute"
            ) from exc


def reap_marked_processes(
    marker: str,
    *,
    terminate_grace_seconds: float = TERMINATE_GRACE_SECONDS,
    kill_grace_seconds: float = KILL_GRACE_SECONDS,
) -> tuple[int, ...]:
    """Kill every surviving process of ours whose argv contains ``marker``.

    For trees spawned by the system under test, where the caller never learns
    the pgid. Identity comes from the argv, not from a remembered pid: the
    process table is re-read before each signal round, so a pid that died and
    was recycled between rounds no longer matches and is never signalled.

    ``marker`` must be unique to the spawn — a pytest ``tmp_path`` passed into
    the spawned command is the intended shape.

    Returns:
        The pids that were signalled, empty when nothing survived.

    Raises:
        ValueError: if ``marker`` also matches this process, which would make
            the sweep suicidal.
        LeakedProcessError: if a marked process outlived its SIGKILL.
    """
    if marker in " ".join(sys.argv):
        raise ValueError(
            f"marker {marker!r} appears in this process's own argv; it cannot "
            "identify the spawned tree"
        )
    signalled: set[int] = set()
    for sig, grace in (
        (signal.SIGTERM, terminate_grace_seconds),
        (signal.SIGKILL, kill_grace_seconds),
    ):
        pids = _marked_pids(marker)
        if not pids:
            break
        signalled.update(pids)
        for pid in pids:
            _signal_pid(pid, sig)
        _await_marker_clear(marker, grace_seconds=grace)
    survivors = _marked_pids(marker)
    if survivors:
        raise LeakedProcessError(
            f"processes {sorted(survivors)} matching {marker!r} survived "
            f"SIGKILL; they are now unattributable load on this machine"
        )
    return tuple(sorted(signalled))


def _marked_pids(marker: str) -> tuple[int, ...]:
    """Our live pids whose argv contains ``marker``.

    Scoped to this user's processes: anything we cannot signal is not ours to
    report on. A zombie's argv is gone from ``ps``, so a reaped-but-unwaited
    child cannot keep this returning non-empty forever.

    ``-U <uid> -o pid,command`` is the spelling both BSD ps (macOS) and procps
    (Linux CI) accept; the BSD-style ``-x`` does not survive the crossing.

    ``-ww`` and the scrubbed ``COLUMNS`` are not belt-and-braces, they are the
    fix for a real CI failure (#7142): procps truncates the COMMAND column to
    ``$COLUMNS`` even when writing to a pipe, and a pytest-xdist worker on
    Linux starts with ``COLUMNS=80`` already set — the controller does not, and
    macOS workers do not, which is why this passed everywhere except CI. The
    marker sits ~450 characters into the argv, so every row came back cut off
    at the interpreter path and the sweep reaped nothing while reporting
    success. A width the environment can choose is a width this cannot use.
    """
    env = {**os.environ}
    env.pop("COLUMNS", None)
    env.pop("LINES", None)
    result = subprocess.run(
        ["ps", "-ww", "-U", str(os.getuid()), "-o", "pid,command"],
        capture_output=True,
        text=True,
        check=True,
        timeout=_PS_TIMEOUT_SECONDS,
        env=env,
    )
    own_pid = os.getpid()
    pids: list[int] = []
    for line in result.stdout.splitlines()[1:]:
        pid_text, _, command = line.strip().partition(" ")
        if marker not in command:
            continue
        pid = int(pid_text)
        if pid != own_pid:
            pids.append(pid)
    return tuple(pids)


def _signal_pid(pid: int, sig: signal.Signals) -> None:
    """Signal one process; gone or unreachable both mean nothing left to do."""
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        return


def _await_marker_clear(marker: str, *, grace_seconds: float) -> None:
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _marked_pids(marker):
            return
        time.sleep(_POLL_SECONDS)


def main(argv: Sequence[str] | None = None) -> int:
    """Hold CPU load for a bounded window, for ad-hoc verification.

    ``python -m tests.load_fixture --workers 20 --seconds 60``

    This exists so that reproducing "the gate is slow under load" by hand takes
    the same guaranteed-reaping path a committed test does. The 2026-08-29
    burners came from a shell, not from a test.
    """
    parser = argparse.ArgumentParser(description=str(main.__doc__).splitlines()[0])
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument(
        "--seconds",
        type=float,
        required=True,
        help="how long to hold the load; also each burner's own hard deadline",
    )
    args = parser.parse_args(argv)

    with cpu_load(workers=args.workers, max_lifetime_seconds=args.seconds) as pids:
        print(f"holding {len(pids)} burners for {args.seconds}s: {list(pids)}")
        time.sleep(args.seconds)
    print("burners reaped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
