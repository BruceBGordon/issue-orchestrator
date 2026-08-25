# pyright: strict
"""Darwin and Linux host CPU counter adapter for executor admission."""

from __future__ import annotations

import ctypes
import math
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.executor_host import ExecutorHostCpuUtilization
from ..ports.host_cpu_utilization import HostCpuUtilizationObserver


_DARWIN_PROCESSOR_CPU_LOAD_INFO = 2
_DARWIN_CPU_STATE_IDLE = 2
_DARWIN_CPU_STATE_COUNT = 4
_DARWIN_COUNTER_MODULUS = 2**32
_LINUX_COUNTER_MODULUS = 2**64
_LINUX_NON_DUPLICATED_COUNTER_COUNT = 8
_KERN_SUCCESS = 0


def _require_path(value: object) -> None:
    if not isinstance(value, Path):
        raise ValueError("Linux CPU counter path must be a Path")


@dataclass(frozen=True, slots=True)
class _CpuCounterSnapshot:
    """Cumulative busy and total counters with an explicit wrap modulus."""

    busy: tuple[int, ...]
    total: tuple[int, ...]
    modulus: int

    def __post_init__(self) -> None:
        if type(self.busy) is not tuple or type(self.total) is not tuple:
            raise ValueError("CPU counter collections must be tuples")
        if not self.busy or len(self.busy) != len(self.total):
            raise ValueError("CPU counter collections must be non-empty and aligned")
        if any(type(value) is not int or value < 0 for value in (*self.busy, *self.total)):
            raise ValueError("CPU counters must be non-negative integers")
        if type(self.modulus) is not int or self.modulus < 2:
            raise ValueError("CPU counter modulus must be an integer greater than one")
        if any(value >= self.modulus for value in (*self.busy, *self.total)):
            raise ValueError("CPU counters must be below their wrap modulus")
        if any(busy > total for busy, total in zip(self.busy, self.total, strict=True)):
            raise ValueError("busy CPU counters must not exceed total counters")


class _CpuCounterSource(Protocol):
    def snapshot(self) -> _CpuCounterSnapshot: ...


class _DarwinCpuCounterSource:
    """Read per-processor Mach CPU ticks and release the kernel buffer."""

    def __init__(self) -> None:
        self._library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        self._library.mach_host_self.argtypes = []
        self._library.mach_host_self.restype = ctypes.c_uint
        self._library.host_processor_info.argtypes = [
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_int)),
            ctypes.POINTER(ctypes.c_uint),
        ]
        self._library.host_processor_info.restype = ctypes.c_int
        self._library.vm_deallocate.argtypes = [
            ctypes.c_uint,
            ctypes.c_uint64,
            ctypes.c_uint64,
        ]
        self._library.vm_deallocate.restype = ctypes.c_int
        self._task = ctypes.c_uint.in_dll(
            self._library,
            "mach_task_self_",
        ).value

    def snapshot(self) -> _CpuCounterSnapshot:
        processor_count = ctypes.c_uint()
        information_count = ctypes.c_uint()
        information = ctypes.POINTER(ctypes.c_int)()
        status = self._library.host_processor_info(
            self._library.mach_host_self(),
            _DARWIN_PROCESSOR_CPU_LOAD_INFO,
            ctypes.byref(processor_count),
            ctypes.byref(information),
            ctypes.byref(information_count),
        )
        if status != _KERN_SUCCESS:
            raise RuntimeError(f"host_processor_info failed with status {status}")
        address = ctypes.cast(information, ctypes.c_void_p).value
        if address is None:
            raise RuntimeError("host_processor_info returned a null buffer")
        expected = processor_count.value * _DARWIN_CPU_STATE_COUNT
        if information_count.value != expected:
            self._deallocate(address, information_count.value)
            raise RuntimeError(
                "host_processor_info returned an unexpected CPU counter count: "
                f"processors={processor_count.value} counters={information_count.value}"
            )
        try:
            values = tuple(
                int(information[index]) % _DARWIN_COUNTER_MODULUS
                for index in range(information_count.value)
            )
        finally:
            self._deallocate(address, information_count.value)
        busy: list[int] = []
        total: list[int] = []
        for offset in range(0, len(values), _DARWIN_CPU_STATE_COUNT):
            processor = values[offset : offset + _DARWIN_CPU_STATE_COUNT]
            total.append(sum(processor) % _DARWIN_COUNTER_MODULUS)
            busy.append(
                sum(
                    value
                    for state, value in enumerate(processor)
                    if state != _DARWIN_CPU_STATE_IDLE
                )
                % _DARWIN_COUNTER_MODULUS
            )
        return _CpuCounterSnapshot(
            tuple(busy),
            tuple(total),
            _DARWIN_COUNTER_MODULUS,
        )

    def _deallocate(
        self,
        address: int,
        information_count: int,
    ) -> None:
        status = self._library.vm_deallocate(
            self._task,
            address,
            information_count * ctypes.sizeof(ctypes.c_int),
        )
        if status != _KERN_SUCCESS:
            raise RuntimeError(f"vm_deallocate failed with status {status}")


class _LinuxCpuCounterSource:
    """Read the aggregate Linux CPU line without a subprocess dependency."""

    def __init__(self, proc_stat: Path = Path("/proc/stat")) -> None:
        _require_path(proc_stat)
        self._proc_stat = proc_stat

    def snapshot(self) -> _CpuCounterSnapshot:
        try:
            first_line = self._proc_stat.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, IndexError) as exc:
            raise RuntimeError(f"cannot read Linux CPU counters: {self._proc_stat}") from exc
        fields = first_line.split()
        if (
            not fields
            or fields[0] != "cpu"
            or len(fields) < _LINUX_NON_DUPLICATED_COUNTER_COUNT + 1
        ):
            raise RuntimeError(f"invalid aggregate CPU counters: {self._proc_stat}")
        try:
            counters = tuple(int(value) for value in fields[1:])
        except ValueError as exc:
            raise RuntimeError(f"invalid aggregate CPU counters: {self._proc_stat}") from exc
        if any(value < 0 or value >= _LINUX_COUNTER_MODULUS for value in counters):
            raise RuntimeError(f"out-of-range aggregate CPU counters: {self._proc_stat}")
        # Linux reports guest and guest_nice as subsets of user and nice.
        # Summing every field therefore double-counts guest execution. The first
        # eight counters are the mutually exclusive user-through-steal values.
        non_duplicated = counters[:_LINUX_NON_DUPLICATED_COUNTER_COUNT]
        idle = non_duplicated[3] + non_duplicated[4]
        total = sum(non_duplicated) % _LINUX_COUNTER_MODULUS
        busy = (total - idle) % _LINUX_COUNTER_MODULUS
        return _CpuCounterSnapshot(
            busy=(busy,),
            total=(total,),
            modulus=_LINUX_COUNTER_MODULUS,
        )


class SystemHostCpuUtilizationObserver(HostCpuUtilizationObserver):
    """Measure interval CPU utilization using the host's native counters."""

    def __init__(
        self,
        *,
        platform: str = sys.platform,
        monotonic: Callable[[], float] = time.monotonic,
        linux_proc_stat: Path = Path("/proc/stat"),
    ) -> None:
        if type(platform) is not str or not platform:
            raise ValueError("host CPU platform must not be empty")
        if not callable(monotonic):
            raise ValueError("host CPU monotonic clock must be callable")
        _require_path(linux_proc_stat)
        if platform == "darwin":
            source: _CpuCounterSource = _DarwinCpuCounterSource()
        elif platform.startswith("linux"):
            source = _LinuxCpuCounterSource(linux_proc_stat)
        else:
            raise RuntimeError(
                "pooled executor CPU-pressure observation supports Darwin and Linux; "
                f"unsupported platform: {platform}"
            )
        self._source = source
        self._monotonic = monotonic
        self._previous: _CpuCounterSnapshot | None = None
        self._previous_at: float | None = None

    def reset(self) -> None:
        self._previous = self._source.snapshot()
        self._previous_at = self._monotonic()

    def observe(self) -> ExecutorHostCpuUtilization:
        previous = self._previous
        previous_at = self._previous_at
        if previous is None or previous_at is None:
            raise RuntimeError("host CPU observation must be reset before observe")
        current = self._source.snapshot()
        observed_at = self._monotonic()
        if current.modulus != previous.modulus:
            raise RuntimeError("host CPU counter modulus changed during observation")
        if len(current.total) != len(previous.total):
            raise RuntimeError("host CPU count changed during observation")
        observation_seconds = observed_at - previous_at
        if not math.isfinite(observation_seconds) or observation_seconds <= 0:
            raise RuntimeError("host CPU observation interval must be positive")
        busy_delta = self._counter_delta(previous.busy, current.busy, current.modulus)
        total_delta = self._counter_delta(previous.total, current.total, current.modulus)
        if total_delta < 1 or busy_delta > total_delta:
            raise RuntimeError(
                "invalid host CPU counter interval: "
                f"busy_delta={busy_delta} total_delta={total_delta}"
            )
        self._previous = current
        self._previous_at = observed_at
        return ExecutorHostCpuUtilization(
            busy_percent=100.0 * busy_delta / total_delta,
            observation_seconds=observation_seconds,
        )

    @staticmethod
    def _counter_delta(
        previous: tuple[int, ...],
        current: tuple[int, ...],
        modulus: int,
    ) -> int:
        return sum(
            (after - before) % modulus
            for before, after in zip(previous, current, strict=True)
        )
