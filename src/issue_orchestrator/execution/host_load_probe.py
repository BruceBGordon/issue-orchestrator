"""Sample what is running on this host, right now.

Facts only: this module reads the machine and parses what it read. What counts
as "too busy" and what counts as debris are policy and live with the caller
that acts on them (``entrypoints/cli_tools/host_load_preflight``).

Signal choice: macOS ``top -l 1`` CPU-idle, not ``getloadavg``. This repo has
been burned by macOS load average before -- it counts parked threads, so it
reads catastrophic on an idle machine and proves nothing either way. The ~1s
that ``top -l 1`` costs is its sampling window, not overhead to shave; it is
what makes the number a measurement.

``ps`` %CPU on BSD is a decaying average of recent CPU, not a lifetime mean, so
the process rows say who is burning the machine now rather than who once did.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

# The probe is bounded so a wedged ``top`` cannot stall the gate it precedes.
PROBE_TIMEOUT_SECONDS = 15.0

_CPU_IDLE_RE = re.compile(r"CPU usage:.*?([0-9.]+)%\s+idle")
_PS_FIELDS = ("pid", "ppid", "user", "pcpu", "etime", "command")


class HostProbeError(RuntimeError):
    """The host could not be sampled, so there is nothing to report on."""


@dataclass(frozen=True)
class ProcessRow:
    """One row of the host process table."""

    pid: int
    ppid: int
    user: str
    cpu_percent: float
    elapsed: str
    elapsed_seconds: int
    command: str


@dataclass(frozen=True)
class HostSnapshot:
    """What the host looked like at one instant."""

    idle_percent: float
    processes: tuple[ProcessRow, ...]


def parse_idle_percent(top_output: str) -> float:
    """Extract the idle percentage from ``top -l 1`` output.

    Raises:
        HostProbeError: if the CPU usage line is absent or unparseable.
            Defaulting to "looks idle" would turn a broken probe into a
            permanently silent check, which is the failure the caller exists
            to prevent.
    """
    match = _CPU_IDLE_RE.search(top_output)
    if match is None:
        raise HostProbeError("no 'CPU usage: ... % idle' line in top output")
    return float(match.group(1))


def parse_elapsed_seconds(elapsed: str) -> int:
    """Convert a ``ps`` ETIME field (``[[dd-]hh:]mm:ss``) to seconds."""
    days = 0
    remainder = elapsed
    if "-" in elapsed:
        day_text, _, remainder = elapsed.partition("-")
        days = int(day_text)
    parts = remainder.split(":")
    if not 2 <= len(parts) <= 3:
        raise HostProbeError(f"unparseable ps ETIME field: {elapsed!r}")
    hours = int(parts[0]) if len(parts) == 3 else 0
    minutes = int(parts[-2])
    seconds = int(parts[-1])
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def parse_process_rows(ps_output: str) -> tuple[ProcessRow, ...]:
    """Parse the ``ps`` table this module asks for.

    Raises:
        HostProbeError: if the output has no data rows or a row does not have
            the requested columns.
    """
    lines = ps_output.splitlines()
    if len(lines) < 2:
        raise HostProbeError("ps produced no process rows")
    rows: list[ProcessRow] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        # COMMAND is the last field and contains spaces, so it takes the rest.
        fields = line.split(maxsplit=len(_PS_FIELDS) - 1)
        if len(fields) != len(_PS_FIELDS):
            raise HostProbeError(f"unparseable ps row: {line!r}")
        pid, ppid, user, pcpu, etime, command = fields
        rows.append(
            ProcessRow(
                pid=int(pid),
                ppid=int(ppid),
                user=user,
                cpu_percent=float(pcpu),
                elapsed=etime,
                elapsed_seconds=parse_elapsed_seconds(etime),
                command=command,
            )
        )
    if not rows:
        raise HostProbeError("ps produced no process rows")
    return tuple(rows)


def build_snapshot(top_output: str, ps_output: str) -> HostSnapshot:
    """Assemble a snapshot from raw probe text."""
    return HostSnapshot(
        idle_percent=parse_idle_percent(top_output),
        processes=parse_process_rows(ps_output),
    )


def probe_host() -> HostSnapshot:
    """Sample the live host.

    Raises:
        HostProbeError: if either probe fails to run or to parse.
    """
    return build_snapshot(
        _run_probe(["top", "-l", "1", "-n", "0"]),
        _run_probe(["ps", "-Ao", ",".join(_PS_FIELDS)]),
    )


def _run_probe(args: list[str]) -> str:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostProbeError(f"{args[0]} could not be run: {exc}") from exc
    if result.returncode != 0:
        raise HostProbeError(f"{args[0]} exited {result.returncode}")
    return result.stdout
