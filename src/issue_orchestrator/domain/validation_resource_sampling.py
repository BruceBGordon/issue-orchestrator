"""Closed lifecycle facts for optional validation resource observations."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ValidationResourceSamplerStarted:
    """The sampler target acknowledged activation."""


@dataclass(frozen=True, slots=True)
class ValidationResourceSamplerStartRejected:
    """No sampler target can execute after startup rejection."""

    error: BaseException


@dataclass(frozen=True, slots=True)
class ValidationResourceSamplerStartIndeterminate:
    """A retained sampler target may still acknowledge activation."""

    error: BaseException


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
class ValidationResourceSamplerStopped:
    """The sampler can no longer append resource evidence."""


@dataclass(frozen=True, slots=True)
class ValidationResourceSamplerShutdownFailed:
    """The sampler daemon remains blocked, but cannot publish late evidence."""

    error: BaseException


@dataclass(frozen=True, slots=True)
class ValidationResourceSamplerFailed:
    """Resource collection or synchronous evidence publication failed."""

    error: BaseException


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
