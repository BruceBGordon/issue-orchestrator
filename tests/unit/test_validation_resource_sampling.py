"""Lifecycle tests for bounded validation resource evidence."""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from issue_orchestrator.domain.validation_resource_sampling import (
    ValidationHostProbeObserved,
    ValidationHostProbeRequest,
    ValidationHostProbeResult,
    ValidationHostProbeUnavailable,
    ValidationResourceSamplerFailed,
    ValidationResourceSamplerStartIndeterminate,
    ValidationResourceSamplerStartRejected,
    ValidationResourceSamplerShutdownFailed,
    ValidationResourceSamplerStopped,
    ValidationResourceSamplerStarted,
    ValidationResourceSamplingPolicy,
)
from issue_orchestrator.domain.validation_execution import ValidationCommandTimedOut
from issue_orchestrator.domain.validation_timing import ValidationDiskDeltaStatus
from issue_orchestrator.entrypoints.bootstrap_executor import (
    build_validation_command_runner,
)
from issue_orchestrator.execution.retained_thread import (
    ImmediateThreadNativeExitPrimitive,
    MaskedThreadStartPrimitive,
    ThreadingRetainedThreadFactory,
)
from issue_orchestrator.execution.validation_resource_sampling import (
    ContainedValidationHostProbe,
    SystemValidationResourceProbe,
    ValidationResourceSampler,
)
from issue_orchestrator.infra.validation_timings import (
    ValidateTimingRecorder,
    ValidationResourceSample,
)
from tests.process_completion_fixture import PROCESS_COMPLETION_WATCHDOG
from tests.process_tree_fixture import (
    CooperativeTermResistantProcessTreeProgram,
    ProcessTreeMember,
)


def _retained_thread_factory() -> ThreadingRetainedThreadFactory:
    return ThreadingRetainedThreadFactory(
        MaskedThreadStartPrimitive(),
        ImmediateThreadNativeExitPrimitive(),
    )


def _sample() -> ValidationResourceSample:
    return ValidationResourceSample(
        recorded_at=datetime.now(timezone.utc).isoformat(),
        loadavg_1m=None,
        loadavg_5m=None,
        loadavg_15m=None,
        memory_free_percent=None,
        swap=None,
        disk=None,
    )


@pytest.mark.parametrize(
    "fact_type",
    (
        ValidationResourceSamplerStartRejected,
        ValidationResourceSamplerStartIndeterminate,
        ValidationResourceSamplerShutdownFailed,
        ValidationResourceSamplerFailed,
    ),
)
def test_sampler_failure_facts_require_real_exceptions(
    fact_type: type[
        ValidationResourceSamplerStartRejected
        | ValidationResourceSamplerStartIndeterminate
        | ValidationResourceSamplerShutdownFailed
        | ValidationResourceSamplerFailed
    ],
) -> None:
    with pytest.raises(ValueError, match="error must be a BaseException"):
        fact_type("not an exception")  # type: ignore[arg-type]


@dataclass(slots=True)
class _BlockingResourceProbe:
    blocked: threading.Event
    release: threading.Event
    collection_count: int = 0

    def collect(self) -> ValidationResourceSample:
        self.collection_count += 1
        if self.collection_count > 1:
            self.blocked.set()
            PROCESS_COMPLETION_WATCHDOG.wait_for_event(
                self.release,
                operation="blocked validation resource probe release",
            )
        return _sample()


@dataclass(slots=True)
class _FailingPeriodicResourceProbe:
    failure_observed: threading.Event
    collection_count: int = 0

    def collect(self) -> ValidationResourceSample:
        self.collection_count += 1
        if self.collection_count > 1:
            self.failure_observed.set()
            raise RuntimeError("periodic resource probe failed")
        return _sample()


@pytest.mark.skipif(sys.platform == "win32", reason="asserts POSIX group containment")
@pytest.mark.timeout(10)
def test_host_probe_timeout_contains_term_resistant_descendant(
    tmp_path: Path,
) -> None:
    descendant_pid_path = (tmp_path / "host-probe-descendant.pid").resolve()
    source = CooperativeTermResistantProcessTreeProgram(
        descendant_pid_path,
        300,
        ("host-probe-ready",),
    ).python_source()
    result = ContainedValidationHostProbe(build_validation_command_runner()).run(
        ValidationHostProbeRequest(
            (sys.executable, "-c", source),
            tmp_path.resolve(),
            1.0,
        )
    )

    assert type(result) is ValidationHostProbeUnavailable
    assert type(result.execution.cleanup) is ValidationCommandTimedOut
    descendant = ProcessTreeMember(int(descendant_pid_path.read_text(encoding="utf-8")))
    descendant.assert_contained()


@pytest.mark.skipif(sys.platform == "win32", reason="asserts POSIX group containment")
@pytest.mark.timeout(10)
def test_host_probe_returns_output_only_after_contained_success(tmp_path: Path) -> None:
    result = ContainedValidationHostProbe(build_validation_command_runner()).run(
        ValidationHostProbeRequest(
            (sys.executable, "-c", "print('host-probe-ok')"),
            tmp_path.resolve(),
            1.0,
        )
    )

    assert type(result) is ValidationHostProbeObserved
    assert result.output == "host-probe-ok"


@dataclass(slots=True)
class _DiskCounterHostProbe:
    disk_outputs: tuple[str, ...]
    disk_index: int = 0

    def run(self, request: ValidationHostProbeRequest) -> ValidationHostProbeResult:
        if request.arguments[:1] != ("iostat",):
            return ValidationHostProbeObserved("")
        output = self.disk_outputs[self.disk_index]
        self.disk_index += 1
        return ValidationHostProbeObserved(output)


def test_iostat_counters_reset_independently_without_negative_usage(
    tmp_path: Path,
) -> None:
    host_probe = _DiskCounterHostProbe(
        (
            "device xfrs MB\ndisk0 totals\ndisk0 100 50",
            "device xfrs MB\ndisk0 totals\ndisk0 90 55",
            "device xfrs MB\ndisk0 totals\ndisk0 95 54",
        )
    )
    probe = SystemValidationResourceProbe(
        tmp_path.resolve(),
        ValidationResourceSamplingPolicy(1.0, 1.0, 4.0),
        host_probe,
    )

    baseline = probe.collect().disk
    transfers_reset = probe.collect().disk
    megabytes_reset = probe.collect().disk

    assert baseline is not None
    assert baseline.transfers_delta is None
    assert baseline.megabytes_delta is None
    assert (
        baseline.transfers_delta_status
        is ValidationDiskDeltaStatus.BASELINE_UNAVAILABLE
    )
    assert transfers_reset is not None
    assert transfers_reset.transfers_delta is None
    assert (
        transfers_reset.transfers_delta_status
        is ValidationDiskDeltaStatus.COUNTER_RESET
    )
    assert transfers_reset.megabytes_delta == 5.0
    assert transfers_reset.megabytes_delta_status is ValidationDiskDeltaStatus.AVAILABLE
    assert megabytes_reset is not None
    assert megabytes_reset.transfers_delta == 5.0
    assert megabytes_reset.megabytes_delta is None
    assert (
        megabytes_reset.megabytes_delta_status
        is ValidationDiskDeltaStatus.COUNTER_RESET
    )


def test_blocked_sampler_reports_typed_shutdown_and_cannot_append_late(
    tmp_path: Path,
) -> None:
    worktree = (tmp_path / "repo").resolve()
    worktree.mkdir()
    (worktree / ".git").mkdir()
    recorder = ValidateTimingRecorder(worktree, "true")
    blocked = threading.Event()
    release = threading.Event()
    probe = _BlockingResourceProbe(blocked, release)
    policy = ValidationResourceSamplingPolicy(
        sample_interval_seconds=0.01,
        probe_timeout_seconds=0.01,
        shutdown_timeout_seconds=0.1,
    )
    sampler = ValidationResourceSampler(
        recorder,
        probe,
        policy,
        _retained_thread_factory(),
    )
    assert type(sampler.start()) is ValidationResourceSamplerStarted
    try:
        PROCESS_COMPLETION_WATCHDOG.wait_for_event(
            blocked,
            operation="resource sampler blocked probe",
        )

        first_shutdown = sampler.stop()
        timing_path = (
            worktree / ".git" / "issue-orchestrator" / "validate-timings.jsonl"
        )
        lines_after_failed_shutdown = timing_path.read_text(
            encoding="utf-8"
        ).splitlines()
    finally:
        release.set()
    final_shutdown = sampler.stop()
    lines_after_probe_release = timing_path.read_text(encoding="utf-8").splitlines()

    assert type(first_shutdown) is ValidationResourceSamplerShutdownFailed
    assert type(first_shutdown.error) is ExceptionGroup
    assert len(first_shutdown.error.exceptions) == 2
    assert all(type(error) is TimeoutError for error in first_shutdown.error.exceptions)
    assert "initial" in str(first_shutdown.error.exceptions[0])
    assert "recovery" in str(first_shutdown.error.exceptions[1])
    assert type(final_shutdown) is ValidationResourceSamplerStopped
    assert lines_after_probe_release == lines_after_failed_shutdown


def test_periodic_probe_failure_is_returned_as_typed_terminal_evidence(
    tmp_path: Path,
) -> None:
    worktree = (tmp_path / "repo").resolve()
    worktree.mkdir()
    (worktree / ".git").mkdir()
    failure_observed = threading.Event()
    sampler = ValidationResourceSampler(
        ValidateTimingRecorder(worktree, "true"),
        _FailingPeriodicResourceProbe(failure_observed),
        ValidationResourceSamplingPolicy(
            sample_interval_seconds=0.01,
            probe_timeout_seconds=0.01,
            shutdown_timeout_seconds=0.1,
        ),
        _retained_thread_factory(),
    )
    assert type(sampler.start()) is ValidationResourceSamplerStarted
    PROCESS_COMPLETION_WATCHDOG.wait_for_event(
        failure_observed,
        operation="periodic resource probe failure",
    )

    shutdown = sampler.stop()

    assert type(shutdown) is ValidationResourceSamplerFailed
    assert type(shutdown.error) is RuntimeError
    assert str(shutdown.error) == "periodic resource probe failed"
