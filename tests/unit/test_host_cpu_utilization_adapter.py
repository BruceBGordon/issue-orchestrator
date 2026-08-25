"""Contract tests for native host CPU-utilization observation."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from issue_orchestrator.adapters import host_cpu_utilization
from issue_orchestrator.adapters.host_cpu_utilization import SystemHostCpuUtilizationObserver


_LINUX_COUNTER_MODULUS = 2**64


def test_darwin_observation_accepts_independently_wrapped_cumulative_sums(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = iter(
        (
            host_cpu_utilization._CpuCounterSnapshot(  # noqa: SLF001
                (80,), (95,), 100
            ),
            host_cpu_utilization._CpuCounterSnapshot(  # noqa: SLF001
                (95,), (10,), 100
            ),
        )
    )

    class _WrappedDarwinCounterSource:
        def snapshot(self) -> host_cpu_utilization._CpuCounterSnapshot:
            return next(snapshots)

    monkeypatch.setattr(
        host_cpu_utilization,
        "_DarwinCpuCounterSource",
        _WrappedDarwinCounterSource,
    )
    observer = SystemHostCpuUtilizationObserver(
        platform="darwin",
        monotonic=_MonotonicSequence((10.0, 12.0)),
    )
    observer.reset()

    observation = observer.observe()

    assert observation.busy_percent == pytest.approx(100.0 * 15 / 15)


class _MonotonicSequence:
    """Deterministic positive observation intervals for the public adapter."""

    def __init__(self, values: tuple[float, ...]) -> None:
        self._values: Iterator[float] = iter(values)

    def __call__(self) -> float:
        return next(self._values)


def _linux_observer(proc_stat: Path) -> SystemHostCpuUtilizationObserver:
    return SystemHostCpuUtilizationObserver(
        platform="linux",
        monotonic=_MonotonicSequence((10.0, 12.0)),
        linux_proc_stat=proc_stat,
    )


def test_linux_observation_excludes_duplicate_guest_counters(tmp_path: Path) -> None:
    proc_stat = tmp_path / "proc-stat"
    proc_stat.write_text("cpu 100 10 20 300 40 5 6 7 50 4\n", encoding="utf-8")
    observer = _linux_observer(proc_stat)
    observer.reset()
    proc_stat.write_text("cpu 110 10 30 320 45 7 8 10 60 5\n", encoding="utf-8")

    observation = observer.observe()

    assert observation.observation_seconds == 2.0
    assert observation.busy_percent == pytest.approx(100.0 * 27 / 52)


def test_linux_observation_handles_unsigned_counter_wrap(tmp_path: Path) -> None:
    proc_stat = tmp_path / "proc-stat"
    proc_stat.write_text(
        f"cpu {_LINUX_COUNTER_MODULUS - 20} 0 0 10 0 0 0 0 0 0\n",
        encoding="utf-8",
    )
    observer = _linux_observer(proc_stat)
    observer.reset()
    proc_stat.write_text("cpu 5 0 0 20 0 0 0 0 0 0\n", encoding="utf-8")

    observation = observer.observe()

    assert observation.busy_percent == pytest.approx(100.0 * 25 / 35)


@pytest.mark.parametrize(
    "contents",
    (
        "",
        "intr 1 2 3 4 5 6 7 8\n",
        "cpu 1 2 3 4 5 6 7\n",
        "cpu 1 2 3 4 5 6 7 invalid\n",
        "cpu 1 2 3 4 5 6 7 -1\n",
        f"cpu 1 2 3 4 5 6 7 {_LINUX_COUNTER_MODULUS}\n",
    ),
)
def test_linux_observer_fails_fast_on_malformed_proc_stat(
    tmp_path: Path,
    contents: str,
) -> None:
    proc_stat = tmp_path / "proc-stat"
    proc_stat.write_text(contents, encoding="utf-8")
    observer = _linux_observer(proc_stat)

    with pytest.raises(RuntimeError, match="CPU counters"):
        observer.reset()


def test_observation_requires_explicit_reset(tmp_path: Path) -> None:
    proc_stat = tmp_path / "proc-stat"
    proc_stat.write_text("cpu 1 2 3 4 5 6 7 8 9 10\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="reset before observe"):
        _linux_observer(proc_stat).observe()
