"""Host machine-state sampling and the record envelope it stamps.

This module is the single owner of three decisions that would otherwise
scatter across every record writer (#7127):

**Where the idle figure comes from.** Load average is not a substitute:
on macOS it counts parked threads, so a host reading 12.5 can be 85%
idle — the exact misreading that cost a 40-minute "wait for quiet". Both
supported platforms expose the same thing, cumulative CPU tick counters,
so both are read the same way: two reads a fixed window apart, and the
idle share of the delta. Linux reads ``/proc/stat``; darwin reads the
kernel's ``host_statistics(HOST_CPU_LOAD_INFO)`` — the counters ``top``
itself prints, without ``top``.

Parsing ``top -l 1`` (the obvious darwin route, and the one #7127
sketched) was measured and rejected: it costs ~1-1.5s of *CPU* per
probe on an 18-core host and gets more expensive as the host gets
busier — an observability probe that materially disturbs the thing it
measures is a broken instrument, and stamping one per record would have
added tens of seconds of self-inflicted load to every gate. The tick
counters cost microseconds plus the sample window. This layer must also
stay free of ``subprocess`` (the control layer imports it transitively,
and the import contract forbids that), which the same choice satisfies.

**How often the host is actually probed.** A sampler holds its reading
for a minimum interval, so a process writing many records probes on a
bounded cadence rather than once per row, and every envelope carries
``sampled_at`` so reuse is visible and never silently stale.

**What a failed probe does.** Nothing, to the work being observed. See
``sample_machine_state``.

Deliberately absent: a running-job count from the batch scheduler. It
would cost a scheduler-tool subprocess per record (under Rosetta 2 on
darwin, by far the most expensive thing in an otherwise syscall-only
sampler), and it would put scheduler vocabulary in this layer, which the
anti-corruption guardrail forbids for good reason. It is also
redundant: the lane-dispatch journal records every lane's end instant,
runtime and queue wait, so concurrent-lane overlap at any moment is
derivable from the journal itself at zero runtime cost.
"""

from __future__ import annotations

import ctypes
import logging
import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, cast

from ..ports.machine_state import MachineState, MachineStateSampler
from .containment import TEARDOWN_SIGNALS, describe_exception, safe_type_name

logger = logging.getLogger(__name__)

MACHINE_STATE_RECORD_KEY = "machine_state"

# The window is taken inline on both platforms: a delta carried across
# calls would report a different quantity from a short-lived CLI than
# from a long-running gate, and a fixed anchor means every record's
# number means the same thing.
#
# It is a FLOOR, not a fixed wait. Darwin's host-wide aggregate refreshes
# on the kernel's own cadence, which coarsens exactly when the host is
# busy — measured 2026-08-29 on a saturated 18-core host, a 0.1s window
# saw the counters unchanged 68% of the time, and even a 1s window
# occasionally saw nothing. A "no measurement" answer precisely when the
# machine is pegged is the worst possible failure for this envelope, so
# the probe re-reads until the counters actually move, bounded.
CPU_SAMPLE_WINDOW_SECONDS = 0.1
CPU_SAMPLE_TIMEOUT_SECONDS = 2.0
_DEFAULT_MINIMUM_INTERVAL_SECONDS = 5.0

_PROC_STAT = Path("/proc/stat")
_LINUX_TICK_SOURCE = f"/proc/stat over {CPU_SAMPLE_WINDOW_SECONDS:g}s"
_DARWIN_TICK_SOURCE = (
    "host_statistics(HOST_CPU_LOAD_INFO) over "
    f"{CPU_SAMPLE_WINDOW_SECONDS:g}s"
)

# mach/processor_info.h: CPU_STATE_USER, SYSTEM, IDLE, NICE.
_HOST_CPU_LOAD_INFO = 3
_MACH_IDLE_STATE_INDEX = 2
_SYSTEM_LIBRARY = "/usr/lib/libSystem.dylib"


@dataclass(frozen=True, slots=True)
class CpuTicks:
    """Cumulative CPU time since boot, in whatever unit the OS counts."""

    total: int
    idle: int


class _HostCpuLoadInfo(ctypes.Structure):
    _fields_ = [("cpu_ticks", ctypes.c_uint * 4)]


def sample_machine_state(sampler: MachineStateSampler) -> MachineState:
    """Read host contention, containing any sampling failure here."""
    return sample_machine_state_from(lambda: sampler)


def sample_machine_state_from(
    acquire: Callable[[], MachineStateSampler],
) -> MachineState:
    """Acquire the probe and read it inside ONE containment.

    THE owner decision for observability-probe failure semantics: this
    repository is fail-fast, and this is the one deliberate exception.
    An envelope exists to explain a lane's timing; if it could raise it
    would instead turn a green lane red and manufacture exactly the
    false failures this forensics work exists to remove. The failure is
    contained, not swallowed — it lands in the record as ``probe_error``,
    in the same JSONL the analyst is already reading, so a degrading
    sampler is visible rather than invisible. Obtaining the probe is as
    much "the probe" as reading it, so both happen inside the boundary.

    **BaseException policy** (round 1 finding A). ``except Exception``
    was not a boundary: a sampler raising ``SystemExit``, or a signal
    handler raising one during the sampling window, sailed straight
    through and replaced an ALREADY-DECIDED lane outcome before it could
    be journaled. So this catches ``BaseException`` and re-raises only
    ``TEARDOWN_SIGNALS`` (``infra/containment.py``, shared with the
    lane executor's cancellation path so the two cannot drift) — the
    signals whose whole meaning is "stop, the caller is going away".

    ``SystemExit`` is therefore contained and recorded, including one
    delivered by a signal handler mid-sample. That is a real trade, made
    deliberately: the alternative is a probe silently replacing the
    gate's exit code with its own, which is the exact harm this boundary
    exists to prevent. The window is bounded by the sampler's own
    timeout, an interrupt still wins, and a supervisor that means it
    will follow SIGTERM with SIGKILL.
    """
    try:
        state = acquire().sample()
    except TEARDOWN_SIGNALS:
        raise
    except BaseException as error:
        reason = describe_exception(error)
        logger.warning("machine-state sampling failed: %s", reason)
        return unmeasured_machine_state(reason, source="sampler raised")
    if type(state) is not MachineState:
        # A sampler answering with the wrong type is a bug, but not one
        # worth failing a gate over: record it like any failed probe.
        name = safe_type_name(state)
        logger.warning("machine-state sampler returned %s", name)
        return unmeasured_machine_state(
            f"sampler returned {name}, not MachineState",
            source="sampler contract violated",
        )
    return state


def unmeasured_machine_state(reason: str, *, source: str) -> MachineState:
    """A reading that failed: the reason, and no invented numbers."""
    return MachineState(
        sampled_at=datetime.now(timezone.utc),
        loadavg_1m=None,
        loadavg_5m=None,
        loadavg_15m=None,
        cpu_idle_percent=None,
        cpu_idle_source=source,
        physical_cores=None,
        probe_error=reason,
    )


def machine_state_fields(state: MachineState) -> dict[str, object]:
    """Render one reading as the nested envelope every record carries.

    Nested under one key so the envelope is one obvious unit that cannot
    collide with a record's own timing fields, and always with the same
    keys — a null is a recorded fact, an absent key is an ambiguity.
    """
    if type(state) is not MachineState:
        raise ValueError("machine_state_fields requires a MachineState")
    envelope: dict[str, object] = {
        "sampled_at": state.sampled_at.isoformat(),
        "loadavg_1m": state.loadavg_1m,
        "loadavg_5m": state.loadavg_5m,
        "loadavg_15m": state.loadavg_15m,
        "cpu_idle_percent": state.cpu_idle_percent,
        "cpu_idle_source": state.cpu_idle_source,
        "physical_cores": state.physical_cores,
        "probe_error": state.probe_error,
    }
    return {MACHINE_STATE_RECORD_KEY: envelope}


class MachineStateEnvelopeError(ValueError):
    """A stored envelope cannot be read back as the reading it recorded."""


def machine_state_from_fields(record: dict[str, object]) -> MachineState:
    """Read back the envelope :func:`machine_state_fields` wrote.

    Lives beside the writer because the envelope's shape is ONE
    decision; a reader that lived with its first consumer would drift
    from the writer the moment either changed. Strict on the way in for
    the same reason the writer always emits every key: a null is a
    recorded fact and an absent key is an ambiguity, so a missing key is
    an error rather than a quietly-invented ``None``.
    """
    if type(record) is not dict:
        raise MachineStateEnvelopeError("a record must be a mapping")
    envelope = record.get(MACHINE_STATE_RECORD_KEY)
    if not isinstance(envelope, dict):
        raise MachineStateEnvelopeError(
            f"record carries no {MACHINE_STATE_RECORD_KEY!r} envelope"
        )
    fields = cast("dict[str, object]", envelope)
    expected = {
        "sampled_at",
        "loadavg_1m",
        "loadavg_5m",
        "loadavg_15m",
        "cpu_idle_percent",
        "cpu_idle_source",
        "physical_cores",
        "probe_error",
    }
    missing = expected - set(fields)
    if missing:
        raise MachineStateEnvelopeError(
            f"envelope is missing {sorted(missing)}"
        )
    sampled_at = fields["sampled_at"]
    if type(sampled_at) is not str:
        raise MachineStateEnvelopeError("envelope 'sampled_at' is not a string")
    try:
        moment = datetime.fromisoformat(sampled_at)
    except ValueError as error:
        raise MachineStateEnvelopeError(
            f"envelope 'sampled_at' is not a timestamp: {sampled_at!r}"
        ) from error
    try:
        # MachineState owns every field invariant; reuse them rather
        # than restating them here.
        return MachineState(
            sampled_at=moment,
            loadavg_1m=_optional_float(fields, "loadavg_1m"),
            loadavg_5m=_optional_float(fields, "loadavg_5m"),
            loadavg_15m=_optional_float(fields, "loadavg_15m"),
            cpu_idle_percent=_optional_float(fields, "cpu_idle_percent"),
            cpu_idle_source=cast(str, fields["cpu_idle_source"]),
            physical_cores=cast("int | None", fields["physical_cores"]),
            probe_error=cast("str | None", fields["probe_error"]),
        )
    except ValueError as error:
        raise MachineStateEnvelopeError(f"envelope is not a reading: {error}") from error


def _optional_float(fields: dict[str, object], name: str) -> float | None:
    """A recorded number, or a recorded absence — never a guess.

    JSON renders 2.0 as 2, so an integer here is the same measurement as
    a float one; anything else is not a measurement at all.
    """
    value = fields[name]
    if value is None:
        return None
    if type(value) is int:
        return float(value)
    if type(value) is float:
        return value
    raise MachineStateEnvelopeError(f"envelope {name!r} is not a number")


def stamp_machine_state(sampler: MachineStateSampler) -> dict[str, object]:
    """Sample and render in one step, for writers that own no state."""
    return machine_state_fields(sample_machine_state(sampler))


def idle_percent_between(before: CpuTicks, after: CpuTicks) -> float | None:
    """The idle share of the CPU time that elapsed between two reads."""
    total_delta = after.total - before.total
    idle_delta = after.idle - before.idle
    if total_delta <= 0 or idle_delta < 0:
        # No elapsed ticks is no measurement. Reporting 0% here would
        # read as a pegged machine, which is the opposite conclusion.
        return None
    return round(min(100.0, 100.0 * idle_delta / total_delta), 2)


def parse_proc_stat_ticks(text: str) -> CpuTicks | None:
    """The aggregate ``cpu`` line's counters; iowait counts as idle."""
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] != "cpu":
            continue
        try:
            values = [int(field) for field in fields[1:9]]
        except ValueError:
            return None
        if len(values) < 5:
            return None
        return CpuTicks(total=sum(values), idle=values[3] + values[4])
    return None


def parse_proc_stat_idle_percent(before: str, after: str) -> float | None:
    """Idle percentage over the delta between two ``/proc/stat`` reads."""
    first = parse_proc_stat_ticks(before)
    second = parse_proc_stat_ticks(after)
    if first is None or second is None:
        return None
    return idle_percent_between(first, second)


def _read_proc_stat_ticks() -> CpuTicks | None:
    return parse_proc_stat_ticks(_PROC_STAT.read_text(encoding="utf-8"))


_MACH_LOCK = threading.Lock()
_MACH_LIBRARY: ctypes.CDLL | None = None


def _mach_library() -> ctypes.CDLL:
    global _MACH_LIBRARY
    with _MACH_LOCK:
        if _MACH_LIBRARY is None:
            library = ctypes.CDLL(_SYSTEM_LIBRARY, use_errno=True)
            library.mach_host_self.restype = ctypes.c_uint
            library.host_statistics.argtypes = [
                ctypes.c_uint,
                ctypes.c_int,
                ctypes.POINTER(_HostCpuLoadInfo),
                ctypes.POINTER(ctypes.c_uint),
            ]
            _MACH_LIBRARY = library
        return _MACH_LIBRARY


def read_mach_cpu_ticks() -> CpuTicks | None:
    """Darwin's host-wide CPU tick counters — ``/proc/stat``'s twin.

    Raises ``OSError`` when the kernel call is unavailable or refuses,
    which the sampler reports as an unanswered probe.
    """
    library = _mach_library()
    info = _HostCpuLoadInfo()
    count = ctypes.c_uint(
        ctypes.sizeof(_HostCpuLoadInfo) // ctypes.sizeof(ctypes.c_uint)
    )
    status = library.host_statistics(
        library.mach_host_self(),
        _HOST_CPU_LOAD_INFO,
        ctypes.byref(info),
        ctypes.byref(count),
    )
    if status != 0:
        raise OSError(f"host_statistics returned {status}")
    ticks = [int(value) for value in info.cpu_ticks]
    return CpuTicks(total=sum(ticks), idle=ticks[_MACH_IDLE_STATE_INDEX])


def _measure_idle_percent(
    reader: Callable[[], CpuTicks | None]
) -> float | None:
    """Widen the window until the counters move, or give up bounded.

    Re-anchors on a decrease (a counter reset or wrap): holding a stale
    anchor would keep producing the same unusable delta until timeout.
    """
    before = reader()
    if before is None:
        return None
    deadline = time.monotonic() + CPU_SAMPLE_TIMEOUT_SECONDS
    while True:
        time.sleep(CPU_SAMPLE_WINDOW_SECONDS)
        after = reader()
        if after is None:
            return None
        percent = idle_percent_between(before, after)
        if percent is not None:
            return percent
        if after.total < before.total or after.idle < before.idle:
            before = after
        if time.monotonic() >= deadline:
            return None


class HostMachineStateSampler:
    """Probe this host, at most once per ``minimum_interval_seconds``.

    Implements ``MachineStateSampler``: it never raises, reporting a
    failed probe inside the reading instead.
    """

    def __init__(
        self,
        *,
        minimum_interval_seconds: float = _DEFAULT_MINIMUM_INTERVAL_SECONDS,
        platform: str = sys.platform,
    ) -> None:
        if (
            type(minimum_interval_seconds) is not float
            or not math.isfinite(minimum_interval_seconds)
            or minimum_interval_seconds < 0
        ):
            raise ValueError(
                "HostMachineStateSampler.minimum_interval_seconds must be "
                "finite and non-negative"
            )
        self._minimum_interval_seconds = minimum_interval_seconds
        self._platform = platform
        self._lock = threading.Lock()
        self._cached: MachineState | None = None
        self._cached_at: float | None = None

    def sample(self) -> MachineState:
        with self._lock:
            now = time.monotonic()
            cached = self._cached
            cached_at = self._cached_at
            if (
                cached is not None
                and cached_at is not None
                and now - cached_at < self._minimum_interval_seconds
            ):
                return cached
            state = self._read()
            self._cached = state
            self._cached_at = now
            return state

    def _read(self) -> MachineState:
        idle_percent, idle_source = self._cpu_idle()
        try:
            load_1m, load_5m, load_15m = os.getloadavg()
            # sysconf, NOT os.cpu_count(): pytest-xdist sets
            # PYTHON_CPU_COUNT in worker processes and os.cpu_count()
            # honors it, so the envelope would report the xdist width as
            # the machine's size (the same trap documented in
            # tests/unit/test_condor_personal_script.py).
            cores = os.sysconf("SC_NPROCESSORS_ONLN")
        except (OSError, ValueError) as error:
            return unmeasured_machine_state(
                f"{type(error).__name__}: {error}", source=idle_source
            )
        return MachineState(
            sampled_at=datetime.now(timezone.utc),
            loadavg_1m=round(load_1m, 3),
            loadavg_5m=round(load_5m, 3),
            loadavg_15m=round(load_15m, 3),
            cpu_idle_percent=idle_percent,
            cpu_idle_source=idle_source,
            physical_cores=int(cores),
            probe_error=None,
        )

    def _cpu_idle(self) -> tuple[float | None, str]:
        reader, source = self._tick_reader()
        if reader is None:
            return None, source
        try:
            percent = _measure_idle_percent(reader)
        except (OSError, AttributeError, ValueError) as error:
            return None, f"{source} unreadable: {type(error).__name__}"
        if percent is None:
            return None, (
                f"{source} counters did not advance within "
                f"{CPU_SAMPLE_TIMEOUT_SECONDS:g}s"
            )
        return percent, source

    def _tick_reader(
        self,
    ) -> tuple[Callable[[], CpuTicks | None] | None, str]:
        if self._platform == "darwin":
            return read_mach_cpu_ticks, _DARWIN_TICK_SOURCE
        if self._platform.startswith("linux"):
            return _read_proc_stat_ticks, _LINUX_TICK_SOURCE
        return None, f"no CPU idle probe for platform {self._platform!r}"


_DEFAULT_SAMPLER_LOCK = threading.Lock()
_DEFAULT_SAMPLER: HostMachineStateSampler | None = None


def default_machine_state_sampler() -> MachineStateSampler:
    """The one host sampler per process, shared by every record writer.

    Sharing is the mechanism, not a convenience: the minimum sampling
    interval only bounds probe cost if all the records a process writes
    go through the same instance.
    """
    global _DEFAULT_SAMPLER
    with _DEFAULT_SAMPLER_LOCK:
        if _DEFAULT_SAMPLER is None:
            _DEFAULT_SAMPLER = HostMachineStateSampler()
        return _DEFAULT_SAMPLER
