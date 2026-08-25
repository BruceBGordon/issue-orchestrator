"""Bounded host-resource evidence collection for validation runs."""

from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar, cast

from ..domain.validation_resource_sampling import (
    ValidationResourceSamplerFailed,
    ValidationResourceSamplerShutdown,
    ValidationResourceSamplerShutdownFailed,
    ValidationResourceSamplerStopped,
    ValidationResourceSamplerStart,
    ValidationResourceSamplerStarted,
    ValidationResourceSamplerStartIndeterminate,
    ValidationResourceSamplerStartRejected,
    ValidationResourceSamplingPolicy,
)
from ..domain.retained_thread import (
    RetainedThreadActivated,
    RetainedThreadActivationIndeterminate,
    RetainedThreadActivationInterrupted,
    RetainedThreadActivationRejected,
    RetainedThreadFinalized,
    RetainedThreadFinalizedAfterFailure,
    RetainedThreadShutdownPolicy,
    RetainedThreadSpec,
    RetainedThreadState,
    RetainedThreadStillRunning,
)
from ..infra.validation_timings import (
    ValidateTimingRecorder,
    ValidationDiskObservation,
    ValidationResourceSample,
    ValidationSwapUsage,
)
from ..ports.validation_resource_probe import ValidationResourceProbe
from ..ports.retained_thread import RetainedThreadFactory, RetainedThreadLease


_MEMORY_FREE_RE = re.compile(r"System-wide memory free percentage:\s*(?P<percent>\d+)%")
_SWAP_RE = re.compile(
    r"total = (?P<total>[0-9.]+)M\s+used = (?P<used>[0-9.]+)M\s+"
    r"free = (?P<free>[0-9.]+)M"
)
_ExactValue = TypeVar("_ExactValue")


def _require_positive_float(value: float, field_name: str) -> None:
    if type(value) is not float or value <= 0:
        raise ValueError(f"{field_name} must be a positive float")


def _require_exact_type(
    value: object,
    expected_type: type[_ExactValue],
    field_name: str,
) -> _ExactValue:
    if type(value) is not expected_type:
        raise ValueError(f"{field_name} must be {expected_type.__name__}")
    return cast(_ExactValue, value)


def _require_protocol(
    value: object, protocol_type: type[object], field_name: str
) -> None:
    if not isinstance(value, protocol_type):
        raise ValueError(f"{field_name} does not implement {protocol_type.__name__}")


def run_bounded_host_probe(
    arguments: tuple[str, ...],
    *,
    working_directory: Path,
    timeout_seconds: float,
) -> str | None:
    """Return successful probe text; absence is explicit best-effort evidence."""
    if type(arguments) is not tuple or not arguments:
        raise ValueError("host probe arguments must be a non-empty tuple")
    if not working_directory.is_absolute():
        raise ValueError("host probe working_directory must be absolute")
    _require_positive_float(timeout_seconds, "host probe timeout_seconds")
    try:
        result = subprocess.run(
            arguments,
            cwd=working_directory,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def parse_memory_free_percent(output: str | None) -> int | None:
    """Parse ``memory_pressure -Q`` output."""
    if not output:
        return None
    match = _MEMORY_FREE_RE.search(output)
    if not match:
        return None
    return int(match.group("percent"))


def parse_swap_usage(output: str | None) -> ValidationSwapUsage | None:
    """Parse ``sysctl vm.swapusage`` output into MiB values."""
    if not output:
        return None
    match = _SWAP_RE.search(output)
    if not match:
        return None
    return ValidationSwapUsage(
        total_mb=float(match.group("total")),
        used_mb=float(match.group("used")),
        free_mb=float(match.group("free")),
    )


def parse_iostat_totals(output: str | None) -> ValidationDiskObservation | None:
    """Parse ``iostat -Id disk0`` cumulative transfer/MB totals."""
    if not output:
        return None
    lines = [line for line in output.splitlines() if line.strip()]
    if len(lines) < 3:
        return None
    parts = lines[-1].split()
    if len(parts) < 3:
        return None
    try:
        transfers = float(parts[-2])
        megabytes = float(parts[-1])
    except ValueError:
        return None
    return ValidationDiskObservation(
        transfers_total=transfers,
        megabytes_total=megabytes,
        transfers_delta=None,
        megabytes_delta=None,
    )


@dataclass(frozen=True, slots=True)
class _ValidationResourceSamplerThreadHealthy:
    """The collection thread has not raised an exception."""


@dataclass(frozen=True, slots=True)
class _ValidationResourceSamplerThreadFailed:
    """The collection thread stopped at one exact exception."""

    error: BaseException


_ValidationResourceSamplerThread = (
    _ValidationResourceSamplerThreadHealthy | _ValidationResourceSamplerThreadFailed
)


@dataclass(frozen=True, slots=True)
class _ValidationResourceSamplerFinalizationEvidence:
    """Typed facts retained from the thread owner's finalization attempt."""

    remains_live: bool
    failures: tuple[BaseException, ...]


def _sampler_finalization_evidence(
    finalization: (
        RetainedThreadFinalized
        | RetainedThreadFinalizedAfterFailure
        | RetainedThreadStillRunning
    ),
) -> _ValidationResourceSamplerFinalizationEvidence:
    if type(finalization) is RetainedThreadFinalized:
        return _ValidationResourceSamplerFinalizationEvidence(False, ())
    if type(finalization) is RetainedThreadFinalizedAfterFailure:
        return _ValidationResourceSamplerFinalizationEvidence(
            False,
            (finalization.error,),
        )
    if type(finalization) is RetainedThreadStillRunning:
        return _ValidationResourceSamplerFinalizationEvidence(
            True,
            (finalization.error,),
        )
    raise AssertionError("retained thread finalization is a closed union")


def _combined_sampler_failure(
    failures: tuple[BaseException, ...],
) -> BaseException:
    if not failures:
        raise ValueError("sampler failure collection must not be empty")
    if len(failures) == 1:
        return failures[0]
    return BaseExceptionGroup(
        "validation resource sampler failed more than once",
        failures,
    )


@dataclass(slots=True)
class SystemValidationResourceProbe:
    """Collect one bounded, portable-or-partial host resource sample."""

    worktree: Path
    policy: ValidationResourceSamplingPolicy
    _last_disk_totals: ValidationDiskObservation | None = field(
        default=None,
        init=False,
    )

    def __post_init__(self) -> None:
        if not self.worktree.is_absolute():
            raise ValueError("SystemValidationResourceProbe.worktree must be absolute")
        _require_exact_type(
            self.policy,
            ValidationResourceSamplingPolicy,
            "SystemValidationResourceProbe.policy",
        )

    def collect(self) -> ValidationResourceSample:
        loadavg_1m: float | None = None
        loadavg_5m: float | None = None
        loadavg_15m: float | None = None
        try:
            load1, load5, load15 = os.getloadavg()
            loadavg_1m = round(load1, 3)
            loadavg_5m = round(load5, 3)
            loadavg_15m = round(load15, 3)
        except OSError:
            pass

        timeout = self.policy.probe_timeout_seconds
        free_percent = parse_memory_free_percent(
            run_bounded_host_probe(
                ("memory_pressure", "-Q"),
                working_directory=self.worktree,
                timeout_seconds=timeout,
            )
        )
        swap_usage = parse_swap_usage(
            run_bounded_host_probe(
                ("sysctl", "vm.swapusage"),
                working_directory=self.worktree,
                timeout_seconds=timeout,
            )
        )
        disk_totals = parse_iostat_totals(
            run_bounded_host_probe(
                ("iostat", "-Id", "disk0"),
                working_directory=self.worktree,
                timeout_seconds=timeout,
            )
        )
        if disk_totals is not None:
            previous = self._last_disk_totals
            if previous is not None:
                disk_totals = ValidationDiskObservation(
                    transfers_total=disk_totals.transfers_total,
                    megabytes_total=disk_totals.megabytes_total,
                    transfers_delta=round(
                        disk_totals.transfers_total - previous.transfers_total,
                        3,
                    ),
                    megabytes_delta=round(
                        disk_totals.megabytes_total - previous.megabytes_total,
                        3,
                    ),
                )
            self._last_disk_totals = disk_totals

        return ValidationResourceSample(
            recorded_at=datetime.now(timezone.utc).isoformat(),
            loadavg_1m=loadavg_1m,
            loadavg_5m=loadavg_5m,
            loadavg_15m=loadavg_15m,
            memory_free_percent=free_percent,
            swap=swap_usage,
            disk=disk_totals,
        )


@dataclass(slots=True)
class ValidationResourceSampler:
    """Own one resource-probe thread and its evidence publication lifetime."""

    recorder: ValidateTimingRecorder
    probe: ValidationResourceProbe
    policy: ValidationResourceSamplingPolicy
    thread_factory: RetainedThreadFactory
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False)
    _thread: RetainedThreadLease = field(init=False)
    _state_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _pending_samples: list[ValidationResourceSample] = field(
        default_factory=list,
        init=False,
    )
    _thread_result: _ValidationResourceSamplerThread = field(
        default_factory=_ValidationResourceSamplerThreadHealthy,
        init=False,
    )

    def __post_init__(self) -> None:
        if type(self.recorder) is not ValidateTimingRecorder:
            raise ValueError(
                "ValidationResourceSampler.recorder must be ValidateTimingRecorder"
            )
        _require_protocol(
            self.probe,
            ValidationResourceProbe,
            "ValidationResourceSampler.probe",
        )
        _require_exact_type(
            self.policy,
            ValidationResourceSamplingPolicy,
            "ValidationResourceSampler.policy",
        )
        _require_protocol(
            self.thread_factory,
            RetainedThreadFactory,
            "ValidationResourceSampler.thread_factory",
        )
        self._thread = self.thread_factory.prepare(
            RetainedThreadSpec(name="validate-resource-sampler", daemon=True),
            self._run,
        )

    def start(self) -> ValidationResourceSamplerStart:
        if self._thread.state is not RetainedThreadState.CREATED:
            raise RuntimeError("validation resource sampler was started twice")
        try:
            self.recorder.append_resource_sample(self.probe.collect())
        except BaseException as error:
            return ValidationResourceSamplerStartRejected(error)
        activation = self._thread.activate()
        if type(activation) is RetainedThreadActivated:
            return ValidationResourceSamplerStarted()
        if type(activation) is RetainedThreadActivationRejected:
            return ValidationResourceSamplerStartRejected(activation.error)
        if type(activation) is RetainedThreadActivationInterrupted:
            return ValidationResourceSamplerStartIndeterminate(activation.error)
        if type(activation) is RetainedThreadActivationIndeterminate:
            return ValidationResourceSamplerStartIndeterminate(activation.error)
        raise AssertionError("retained thread activation is a closed union")

    def stop(self) -> ValidationResourceSamplerShutdown:
        self._stop_event.set()
        if self._thread.state not in (
            RetainedThreadState.ACTIVATING,
            RetainedThreadState.ACTIVATED,
        ):
            raise RuntimeError("validation resource sampler was stopped before start")
        finalization = self._thread.finalize(
            RetainedThreadShutdownPolicy(
                initial_timeout_seconds=self.policy.shutdown_timeout_seconds,
                recovery_timeout_seconds=self.policy.shutdown_timeout_seconds,
            )
        )
        finalization_evidence = _sampler_finalization_evidence(finalization)
        failures = list(finalization_evidence.failures)
        with self._state_lock:
            samples = tuple(self._pending_samples)
            self._pending_samples.clear()
            if type(self._thread_result) is _ValidationResourceSamplerThreadFailed:
                failures.append(self._thread_result.error)
            elif (
                type(self._thread_result) is not _ValidationResourceSamplerThreadHealthy
            ):
                raise AssertionError("resource sampler thread result is a closed union")
        for sample in samples:
            try:
                self.recorder.append_resource_sample(sample)
            except BaseException as error:
                error.add_note("failed to publish collected validation resource sample")
                failures.append(error)
        if not failures:
            return ValidationResourceSamplerStopped()
        failure = _combined_sampler_failure(tuple(failures))
        if finalization_evidence.remains_live:
            return ValidationResourceSamplerShutdownFailed(failure)
        return ValidationResourceSamplerFailed(failure)

    def _run(self) -> None:
        try:
            while not self._stop_event.wait(self.policy.sample_interval_seconds):
                sample = self.probe.collect()
                # The caller of stop owns all publication. A blocked probe may
                # finish later, but can never append after terminal evidence.
                with self._state_lock:
                    if self._stop_event.is_set():
                        return
                    self._pending_samples.append(sample)
        except BaseException as error:
            with self._state_lock:
                if (
                    type(self._thread_result)
                    is not _ValidationResourceSamplerThreadHealthy
                ):
                    raise AssertionError(
                        "validation resource sampler recorded two thread failures"
                    ) from error
                self._thread_result = _ValidationResourceSamplerThreadFailed(error)
