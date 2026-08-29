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

Every failure to run, decode, parse, or range-check a probe leaves this module
as :class:`HostProbeError` and nothing else. The caller is a CLI that must
always exit 0, and a raw ``ValueError`` escaping from a malformed ``ps`` row
would take the gate's diagnostics out on a nonzero exit.
"""

from __future__ import annotations

import math
import os
import pwd
import re
import subprocess
from dataclasses import dataclass

# The probe is bounded so a wedged ``top`` cannot stall the gate it precedes.
PROBE_TIMEOUT_SECONDS = 15.0

# The idle figure must be a WHOLE token: the match starts at a space and runs
# to ``%``, so nothing may precede the digits inside the token. Partial matches
# are the danger, not absent ones -- ``49,90% idle`` under a comma locale,
# ``+90% idle``, ``abc90% idle`` all end in a plausible number that is not the
# host's idle time, and a wrong-but-high reading is silence exactly when the
# machine is on fire. Probes run under LC_ALL=C so the comma shape should never
# arrive; this grammar is the backstop for when something else does.
#
# ``top`` always separates the field from the previous one with whitespace, so
# requiring it costs nothing real and makes the token unambiguous.
_CPU_IDLE_RE = re.compile(r"CPU usage:[^\n]*?(?<=\s)(\d+(?:\.\d+)?)%\s+idle")
_PS_FIELDS = ("pid", "ppid", "user", "pcpu", "etime", "command")

# A process older than a century is a parse error, not a process. Deliberately
# far above any real uptime: this rejects nonsense, it does not police age.
_MAX_ELAPSED_SECONDS = 100 * 365 * 24 * 3600


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
        HostProbeError: if the CPU usage line is absent, malformed, or outside
            0-100. Defaulting to "looks idle" would turn a broken probe into a
            permanently silent check, which is the failure the caller exists
            to prevent.
    """
    match = _CPU_IDLE_RE.search(top_output)
    if match is None:
        raise HostProbeError("no well-formed 'CPU usage: ... % idle' line in top output")
    idle = float(match.group(1))
    if not 0.0 <= idle <= 100.0:
        raise HostProbeError(f"top reported {idle}% idle, which is not a percentage")
    return idle


def parse_elapsed_seconds(elapsed: str) -> int:
    """Convert a ``ps`` ETIME field (``[[dd-]hh:]mm:ss``) to seconds.

    Raises:
        HostProbeError: on any shape ``ps`` cannot have produced.
    """
    days = 0
    remainder = elapsed
    # Presence, not truthiness: ``0-2:3`` has a day prefix worth nothing, and
    # testing ``if days`` would wave it through as a bare mm:ss.
    has_day_prefix = "-" in elapsed
    if has_day_prefix:
        day_text, _, remainder = elapsed.partition("-")
        days = _to_count(day_text, field=f"ETIME days in {elapsed!r}")
    parts = remainder.split(":")
    if not 2 <= len(parts) <= 3:
        raise HostProbeError(f"unparseable ps ETIME field: {elapsed!r}")
    # ``ps`` only prints a day field alongside a full hh:mm:ss.
    if has_day_prefix and len(parts) != 3:
        raise HostProbeError(f"unparseable ps ETIME field: {elapsed!r}")
    hours = _to_count(parts[0], field=f"ETIME hours in {elapsed!r}") if len(parts) == 3 else 0
    minutes = _to_count(parts[-2], field=f"ETIME minutes in {elapsed!r}")
    seconds = _to_count(parts[-1], field=f"ETIME seconds in {elapsed!r}")
    # Sexagesimal by construction: ps rolls 60 minutes into the next field.
    if minutes > 59 or seconds > 59:
        raise HostProbeError(f"out-of-range ps ETIME field: {elapsed!r}")
    total = ((days * 24 + hours) * 60 + minutes) * 60 + seconds
    if total > _MAX_ELAPSED_SECONDS:
        raise HostProbeError(f"implausible ps ETIME field: {elapsed!r}")
    return total


def _to_count(text: str, *, field: str) -> int:
    """A non-negative integer field, or ``HostProbeError``."""
    try:
        value = int(text)
    except ValueError as exc:
        raise HostProbeError(f"unparseable ps {field}: {text!r}") from exc
    if value < 0:
        raise HostProbeError(f"negative ps {field}: {text!r}")
    return value


def _to_cpu_percent(text: str) -> float:
    """A non-negative finite %CPU, or ``HostProbeError``.

    No upper bound: a threaded process legitimately exceeds 100% of one core.
    ``float`` accepts ``nan`` and ``inf``, so finiteness is checked explicitly.
    """
    try:
        value = float(text)
    except ValueError as exc:
        raise HostProbeError(f"unparseable ps %CPU: {text!r}") from exc
    if not math.isfinite(value) or value < 0.0:
        raise HostProbeError(f"out-of-range ps %CPU: {text!r}")
    return value


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
                pid=_to_count(pid, field="PID"),
                ppid=_to_count(ppid, field="PPID"),
                user=user,
                cpu_percent=_to_cpu_percent(pcpu),
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


def current_owner() -> str:
    """The login name of the user this process runs as."""
    uid = os.getuid()
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError as exc:
        raise HostProbeError(f"no passwd entry for uid {uid}") from exc


def _probe_env() -> dict[str, str]:
    """The inherited environment with the numeric locale pinned.

    Under a comma-decimal locale ``top`` prints ``49,90% idle`` and ``ps``
    prints ``87,3`` for %CPU. C is the only locale whose number formatting the
    parsers here are written against, so it is pinned rather than hoped for.
    """
    return {**os.environ, "LC_ALL": "C", "LANG": "C"}


def _run_probe(args: list[str]) -> str:
    """Run one probe and decode its output, or raise ``HostProbeError``.

    Decoding is explicit rather than ``text=True``: a process whose argv holds
    non-UTF-8 bytes would otherwise raise ``UnicodeDecodeError`` straight out
    of this module, past the caller's typed boundary.
    """
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            check=False,
            timeout=PROBE_TIMEOUT_SECONDS,
            env=_probe_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HostProbeError(f"{args[0]} could not be run: {exc}") from exc
    if result.returncode != 0:
        raise HostProbeError(f"{args[0]} exited {result.returncode}")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HostProbeError(f"{args[0]} produced undecodable output: {exc}") from exc
