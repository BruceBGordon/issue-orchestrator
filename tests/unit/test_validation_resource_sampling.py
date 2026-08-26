"""Lifecycle tests for bounded validation resource evidence."""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from issue_orchestrator.domain.validation_resource_sampling import (
    ValidationResourceSamplerFailed,
    ValidationResourceSamplerShutdownFailed,
    ValidationResourceSamplerStopped,
    ValidationResourceSamplerStarted,
    ValidationResourceSamplingPolicy,
)
from issue_orchestrator.execution.retained_thread import (
    ImmediateThreadNativeExitPrimitive,
    MaskedThreadStartPrimitive,
    ThreadingRetainedThreadFactory,
)
from issue_orchestrator.execution.validation_resource_sampling import (
    ValidationResourceSampler,
    run_bounded_host_probe,
)
from issue_orchestrator.infra.validation_timings import (
    ValidateTimingRecorder,
    ValidationResourceSample,
)
from tests.process_completion_fixture import PROCESS_COMPLETION_WATCHDOG


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


def test_host_probe_timeout_bounds_an_unresponsive_command(tmp_path: Path) -> None:
    started_at = datetime.now(timezone.utc)

    output = run_bounded_host_probe(
        (sys.executable, "-c", "import time; time.sleep(30)"),
        working_directory=tmp_path.resolve(),
        timeout_seconds=0.05,
    )

    assert output is None
    assert (datetime.now(timezone.utc) - started_at).total_seconds() < 2.0


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
