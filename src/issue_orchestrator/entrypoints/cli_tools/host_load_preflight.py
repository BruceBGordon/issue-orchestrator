"""Gate-entry host sanity check: name stray load before the gate pays for it.

Incident 2026-08-29 (#7142): an ad-hoc verification shell spawned twenty
``python -c 'while True: pass'`` burners, orphaned them to launchd, and left
them spinning at ~87% CPU for nine hours. Every test-web gate flake that day
(seven, across four unrelated branches) landed inside that window, gate wall
time went from ~90s to 350s+, and the cause was misattributed twice before
anyone ran ``ps``. Killing the burners restored 60%+ idle and the next gate was
the fastest of the night. A sweep then found nine older orphaned fixtures --
deliberately TERM-resistant ``signal.pause`` waiters, some three days old.

So: sample the host at gate entry and *say what is already running*. This is
diagnosis, never enforcement. Nothing here kills a process and nothing here
fails the gate -- a preflight that can block would be a new way to lose gates,
and the cost of the incident was not the load, it was nine hours of not knowing
about the load.

This module owns the policy; ``execution/host_load_probe`` owns the sampling.
"""

from __future__ import annotations

import re
import sys
from typing import TextIO

from ...execution.host_load_probe import (
    HostProbeError,
    HostSnapshot,
    ProcessRow,
    current_owner,
    probe_host,
)

# ---------------------------------------------------------------------------
# Policy. The threshold and the warn behavior live here and nowhere else.
# ---------------------------------------------------------------------------

# Warn below this much idle CPU. At gate entry no lane has spawned yet, so a
# healthy host measures 60-95% idle; the incident measured 0%. 50% sits below
# every healthy observation and far above the degraded one, and an eager
# warning costs one screen line because the check never blocks.
IDLE_WARN_PERCENT = 50.0

# How many CPU consumers to name. Enough to show a burner fleet's shape without
# turning the gate log into a process listing.
TOP_CONSUMER_COUNT = 8

# Orphaned fixture processes younger than this are plausibly a live test run's
# own children, not debris left behind by a finished one.
STRAY_MIN_AGE_SECONDS = 600

# Long argv (a pasted one-liner, a browser helper) must not wrap the table.
COMMAND_DISPLAY_WIDTH = 100

LINE_PREFIX = "[host-preflight]"

# Fixture signatures. Deliberately narrow: an interpreter running an inline
# one-liner, or a bare sleep. Shell `-c` loops are excluded even though the
# sweep found one, because agent tooling runs PPID-1 `zsh -c ... sleep ...`
# poll loops as normal operation and flagging those would train people to
# ignore this section.
_PYTHON_EXECUTABLE_RE = re.compile(r"(?i)(^|/)python(\d+(\.\d+)*)?$")


def top_consumers(snapshot: HostSnapshot) -> tuple[ProcessRow, ...]:
    """The busiest processes, most expensive first."""
    ranked = sorted(snapshot.processes, key=lambda row: row.cpu_percent, reverse=True)
    return tuple(row for row in ranked[:TOP_CONSUMER_COUNT] if row.cpu_percent > 0.0)


def is_fixture_signature(command: str) -> bool:
    """Whether ``command`` looks like a test fixture rather than a daemon."""
    fields = command.split()
    if not fields:
        return False
    executable = fields[0].rsplit("/", maxsplit=1)[-1]
    if executable == "sleep":
        return True
    return (
        len(fields) > 2
        and fields[1] == "-c"
        and _PYTHON_EXECUTABLE_RE.search(fields[0]) is not None
    )


def stray_debris(snapshot: HostSnapshot, *, owner: str) -> tuple[ProcessRow, ...]:
    """Orphaned fixture processes: reparented to init, ours, and stale.

    All three conditions are required. PPID 1 alone matches hundreds of normal
    launchd agents; the fixture signature alone matches a live test run's own
    children; the age alone matches every long-running daemon on the box.
    """
    return tuple(
        row
        for row in snapshot.processes
        if row.ppid == 1
        and row.user == owner
        and row.elapsed_seconds >= STRAY_MIN_AGE_SECONDS
        and is_fixture_signature(row.command)
    )


def _display_command(command: str) -> str:
    """Basename the executable, keep the argv.

    A truncation that keeps the interpreter path and drops the arguments hides
    the only part that identifies the process: an orphan reads as
    ``python -c 'while True: pass'``, never as its Framework path.
    """
    collapsed = " ".join(command.split())
    executable, separator, arguments = collapsed.partition(" ")
    shortened = executable.rsplit("/", maxsplit=1)[-1] + separator + arguments
    if len(shortened) <= COMMAND_DISPLAY_WIDTH:
        return shortened
    return shortened[: COMMAND_DISPLAY_WIDTH - 1] + "…"


def _table(rows: tuple[ProcessRow, ...]) -> list[str]:
    header = f"{'PID':>7}  {'%CPU':>6}  {'AGE':>11}  COMMAND"
    return [header] + [
        f"{row.pid:>7}  {row.cpu_percent:>6.1f}  {row.elapsed:>11}  "
        f"{_display_command(row.command)}"
        for row in rows
    ]


def report_lines(snapshot: HostSnapshot, *, owner: str) -> tuple[str, ...]:
    """Unprefixed report lines; empty when the host is clean.

    The busy check and the debris check are independent on purpose: the nine
    orphaned ``signal.pause`` waiters the sweep found burn no CPU at all, so an
    idle-gated debris report would never have mentioned them.
    """
    lines: list[str] = []
    if snapshot.idle_percent < IDLE_WARN_PERCENT:
        lines.append(
            f"BUSY HOST: only {snapshot.idle_percent:.1f}% CPU idle at gate entry "
            f"(warn below {IDLE_WARN_PERCENT:.0f}%). This gate will contend for CPU: "
            "expect slow lanes and timing-sensitive flakes."
        )
        lines.extend(_table(top_consumers(snapshot)))
    # Debris is listed in full while consumers are ranked and clipped: the
    # ranking makes a consumer tail uninteresting, but every orphan is equally
    # a pid someone has to go and kill.
    strays = stray_debris(snapshot, owner=owner)
    if strays:
        lines.append(
            f"POSSIBLE STRAY TEST DEBRIS: {len(strays)} orphaned (PPID 1) fixture "
            f"process(es) owned by {owner}, older than "
            f"{STRAY_MIN_AGE_SECONDS // 60}m. Leaked fixtures outlive the run that "
            "made them and poison every later gate."
        )
        lines.extend(_table(strays))
    if lines:
        lines.append(
            "Nothing was killed and the gate is not blocked. Verify before acting: "
            "ps -p <pid> -o pid,ppid,etime,command"
        )
    return tuple(lines)


def emit(stream: TextIO, lines: tuple[str, ...]) -> None:
    """Write report lines to ``stream`` with the greppable prefix."""
    for line in lines:
        stream.write(f"{LINE_PREFIX} {line}\n")
    stream.flush()


def main() -> None:
    """Report host load, then get out of the way.

    Always exits 0. The only degradation this tolerates is a host that cannot
    be sampled, and it says so on one line rather than falling silent. Every
    way that can fail -- spawn, exit status, decode, parse, range, passwd
    lookup -- arrives here as ``HostProbeError``, so the single typed handler
    is the whole contract and no blanket ``except`` is needed to keep it.
    """
    if sys.platform != "darwin":
        emit(
            sys.stderr,
            (
                f"skipped: CPU-idle sampling is macOS-only (top -l 1); "
                f"no trusted host signal on {sys.platform}.",
            ),
        )
        return
    try:
        snapshot = probe_host()
        owner = current_owner()
    except HostProbeError as exc:
        emit(sys.stderr, (f"host probe unavailable: {exc}",))
        return
    emit(sys.stderr, report_lines(snapshot, owner=owner))


if __name__ == "__main__":
    main()
