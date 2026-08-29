# pyright: strict
"""The exec shim's CPU-usage side channel — one owner for both halves.

The lane's CPU demand has to be measured somewhere. The scheduler's own
``RemoteUserCpu``/``RemoteSysCpu`` attributes are native on Linux but
report a flat 0.0 on the macOS pool, which has no cgroups to account
against — so a mechanism that only worked there would leave the whole
learning loop inert on the development host. The measurement is
therefore taken one level lower, inside the exec shim, where it is the
same on every platform the shim runs on.

Mechanism: the POSIX shell ``times`` special built-in. After the shell
has waited for the lane, ``times`` reports the accumulated CPU of its
terminated children — precisely ``getrusage(RUSAGE_CHILDREN)``,
recursively including grandchildren the lane itself reaped. Its output
format is fixed by POSIX::

    "%dm%fs %dm%fs\\n%dm%fs %dm%fs\\n"

with the shell's own CPU on the first line and the children's on the
second. Verified identical in shape (only the fraction's precision
varies) across the three shells a lane can land on: bash 3.2 as macOS
``/bin/sh``, dash as the Linux ``/bin/sh``, and zsh.

Why this and not a small Python wrapper around ``os.wait4``: the shim
must stay dependency-free. A Python wrapper bakes one interpreter path
into a job description that a scheduler may run later, elsewhere, or
after that interpreter's virtualenv has moved — and it inserts one
more process between the scheduler's family tracking and the lane, on
a platform where that tracking is already documented as fragile
(ADR-0001). ``times`` is a special built-in: always present, no
process, no path.

Writing and parsing live in the same module on purpose — the format is
one contract with two halves, and splitting them is how a producer and
a consumer drift apart.
"""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

RUSAGE_FILE_NAME = "lane.rusage"

# "%dm%fs %dm%fs" — minutes are a whole number, seconds carry a
# shell-dependent number of decimals (2 on bash 3.2, 6 on dash).
_TIMES_LINE = re.compile(
    r"^\s*(\d+)m([0-9]+(?:\.[0-9]+)?)s\s+(\d+)m([0-9]+(?:\.[0-9]+)?)s\s*$"
)
_CHILDREN_LINE_INDEX = 1
_SECONDS_PER_MINUTE = 60.0


def compile_rusage_capture(rusage_path: Path) -> str:
    """The shim lines that report the lane subtree's CPU, in shell.

    Emitted after the lane has been waited for and before the shim
    exits with the lane's own status. A failure to write the report is
    swallowed (``|| :``): the measurement is a side channel, and a
    full disk must never turn a green lane red.
    """
    if not rusage_path.is_absolute():
        raise ValueError("compile_rusage_capture rusage_path must be absolute")
    return f"times > {shlex.quote(str(rusage_path))} 2>/dev/null || :\n"


def read_cpu_seconds(rusage_path: Path) -> float | None:
    """Total CPU seconds the shim reported, or None when it reported none.

    ``None`` means the report is absent — the normal state for a lane
    the scheduler removed before the shim could run, and for any lane
    whose shim never got that far. A report that EXISTS but does not
    parse is a different thing entirely: the shim's contract is broken
    and that must be visible, so it raises.
    """
    try:
        text = rusage_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError(
            f"cannot read lane CPU report {rusage_path}: {error}"
        ) from error
    lines = text.splitlines()
    if len(lines) <= _CHILDREN_LINE_INDEX:
        raise ValueError(
            f"lane CPU report {rusage_path} is not `times` output: {text!r}"
        )
    matched = _TIMES_LINE.match(lines[_CHILDREN_LINE_INDEX])
    if matched is None:
        raise ValueError(
            f"lane CPU report {rusage_path} has an unparseable children line: "
            f"{lines[_CHILDREN_LINE_INDEX]!r}"
        )
    user_minutes, user_seconds, system_minutes, system_seconds = matched.groups()
    return (
        int(user_minutes) * _SECONDS_PER_MINUTE
        + float(user_seconds)
        + int(system_minutes) * _SECONDS_PER_MINUTE
        + float(system_seconds)
    )


def busy_cores(cpu_seconds: float, runtime_seconds: float) -> float | None:
    """CPU-seconds spread over the lane's runtime, or None if undividable.

    A runtime of zero is not a lane that used infinite cores: it is a
    clock too coarse to divide by. The scheduler's event log carries
    whole-second timestamps, so any lane finishing inside one second
    reports a runtime of 0.0 — common for trivial lanes and always
    meaningless as a denominator. Abstaining is the honest answer.

    Coarse timestamps also INFLATE the figure for short lanes (a 1.4s
    lane logged as 1s reports 40% high). That direction is safe by
    construction: the declared value caps the request, so an inflated
    measurement can never raise it — see
    :mod:`issue_orchestrator.domain.lane_cpu_request`.
    """
    if runtime_seconds <= 0.0:
        return None
    return cpu_seconds / runtime_seconds


def measure_busy_cores(
    rusage_path: Path, runtime_seconds: float, lane_name: str
) -> float | None:
    """The whole side channel as one answer: cores, or nothing.

    This is where "the report is unusable" is decided, so the rule
    lives in one place instead of being re-invented by each caller.
    An unreadable report is announced on stderr and then dropped: a
    lane that ran correctly must never fail over its own
    instrumentation, but a silently swallowed measurement failure
    would leave the loop permanently inert with nobody the wiser. The
    raw report goes into the message because the run directory holding
    it is deleted moments later on a clean completion.
    """
    try:
        cpu_seconds = read_cpu_seconds(rusage_path)
    except ValueError as error:
        print(
            f"lane {lane_name}: CPU report unusable, this run teaches no "
            f"CPU demand: {error}",
            file=sys.stderr,
        )
        return None
    if cpu_seconds is None:
        return None
    return busy_cores(cpu_seconds, runtime_seconds)
