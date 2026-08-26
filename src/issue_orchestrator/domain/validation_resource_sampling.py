"""Closed lifecycle facts for optional validation resource observations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from .validation_execution import ValidationCommandExecution


@dataclass(frozen=True, slots=True)
class ValidationResourceSamplerStarted:
    """The sampler target acknowledged activation."""


@dataclass(frozen=True, slots=True)
class ValidationResourceSamplerStartRejected:
    """No sampler target can execute after startup rejection."""

    error: BaseException

    def __post_init__(self) -> None:
        _require_error(self.error, "ValidationResourceSamplerStartRejected.error")


@dataclass(frozen=True, slots=True)
class ValidationResourceSamplerStartIndeterminate:
    """A retained sampler target may still acknowledge activation."""

    error: BaseException

    def __post_init__(self) -> None:
        _require_error(
            self.error,
            "ValidationResourceSamplerStartIndeterminate.error",
        )


ValidationResourceSamplerStart = (
    ValidationResourceSamplerStarted
    | ValidationResourceSamplerStartRejected
    | ValidationResourceSamplerStartIndeterminate
)


@dataclass(frozen=True, slots=True)
class ValidationResourceSamplingPolicy:
    """Bounds every host probe and the sampler's complete shutdown."""

    sample_interval_seconds: float
    probe_timeout_seconds: float
    shutdown_timeout_seconds: float

    def __post_init__(self) -> None:
        for field_name, value in (
            ("sample_interval_seconds", self.sample_interval_seconds),
            ("probe_timeout_seconds", self.probe_timeout_seconds),
            ("shutdown_timeout_seconds", self.shutdown_timeout_seconds),
        ):
            if type(value) is not float or not math.isfinite(value) or value <= 0:
                raise ValueError(
                    f"ValidationResourceSamplingPolicy.{field_name} must be "
                    "finite and positive"
                )
        if self.shutdown_timeout_seconds <= 3 * self.probe_timeout_seconds:
            raise ValueError(
                "ValidationResourceSamplingPolicy.shutdown_timeout_seconds must "
                "exceed the three sequential host-probe bounds"
            )


@dataclass(frozen=True, slots=True)
class ValidationHostProbeRequest:
    """One argv-safe host observation bounded by an active deadline."""

    arguments: tuple[str, ...]
    working_directory: Path
    timeout_seconds: float

    def __post_init__(self) -> None:
        if (
            type(self.arguments) is not tuple
            or not self.arguments
            or type(self.arguments[0]) is not str
            or not self.arguments[0]
            or any(
                type(argument) is not str or "\0" in argument
                for argument in self.arguments
            )
        ):
            raise ValueError(
                "ValidationHostProbeRequest.arguments must be a non-empty argv tuple"
            )
        if (
            not isinstance(self.working_directory, Path)
            or not self.working_directory.is_absolute()
        ):
            raise ValueError(
                "ValidationHostProbeRequest.working_directory must be absolute"
            )
        if (
            type(self.timeout_seconds) is not float
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError(
                "ValidationHostProbeRequest.timeout_seconds must be finite and positive"
            )


@dataclass(frozen=True, slots=True)
class ValidationHostProbeObserved:
    """A contained host probe exited successfully with complete text evidence."""

    output: str

    def __post_init__(self) -> None:
        if type(self.output) is not str:
            raise ValueError("ValidationHostProbeObserved.output must be text")


@dataclass(frozen=True, slots=True)
class ValidationHostProbeUnavailable:
    """A contained host probe retained its exact non-success lifecycle."""

    execution: ValidationCommandExecution

    def __post_init__(self) -> None:
        if type(self.execution) is not ValidationCommandExecution:
            raise ValueError("ValidationHostProbeUnavailable.execution must be exact")


ValidationHostProbeResult = ValidationHostProbeObserved | ValidationHostProbeUnavailable


@dataclass(frozen=True, slots=True)
class ValidationResourceSamplerStopped:
    """The sampler can no longer append resource evidence."""


@dataclass(frozen=True, slots=True)
class ValidationResourceSamplerShutdownFailed:
    """The sampler daemon remains blocked, but cannot publish late evidence."""

    error: BaseException

    def __post_init__(self) -> None:
        _require_error(self.error, "ValidationResourceSamplerShutdownFailed.error")


@dataclass(frozen=True, slots=True)
class ValidationResourceSamplerFailed:
    """Resource collection or synchronous evidence publication failed."""

    error: BaseException

    def __post_init__(self) -> None:
        _require_error(self.error, "ValidationResourceSamplerFailed.error")


ValidationResourceSamplerShutdown = (
    ValidationResourceSamplerStopped
    | ValidationResourceSamplerFailed
    | ValidationResourceSamplerShutdownFailed
)


def validation_resource_sampler_shutdown_failure(
    shutdown: ValidationResourceSamplerShutdown,
) -> BaseException | None:
    """Return the exact shutdown failure, or prove the sampler stopped."""
    if type(shutdown) is ValidationResourceSamplerShutdownFailed:
        return shutdown.error
    if type(shutdown) is ValidationResourceSamplerFailed:
        return shutdown.error
    if type(shutdown) is ValidationResourceSamplerStopped:
        return None
    raise AssertionError("validation resource sampler shutdown is a closed union")


def _require_error(value: object, field_name: str) -> None:
    if not isinstance(value, BaseException):
        raise ValueError(f"{field_name} must be a BaseException")
