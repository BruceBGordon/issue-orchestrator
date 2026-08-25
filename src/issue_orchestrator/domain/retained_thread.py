"""Strong lifecycle facts for a retained background thread."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class RetainedThreadState(StrEnum):
    """Exact activation state of a thread whose owner already exists."""

    CREATED = "created"
    ACTIVATING = "activating"
    ACTIVATED = "activated"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class RetainedThreadSpec:
    """Construction contract for one retained background thread."""

    name: str
    daemon: bool

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise ValueError("RetainedThreadSpec.name must not be empty")
        if type(self.daemon) is not bool:
            raise ValueError("RetainedThreadSpec.daemon must be bool")


@dataclass(frozen=True, slots=True)
class RetainedThreadShutdownPolicy:
    """Two bounded attempts used to prove that an activated thread stopped."""

    initial_timeout_seconds: float
    recovery_timeout_seconds: float

    def __post_init__(self) -> None:
        for field_name, value in (
            ("initial_timeout_seconds", self.initial_timeout_seconds),
            ("recovery_timeout_seconds", self.recovery_timeout_seconds),
        ):
            if type(value) is not float or not math.isfinite(value) or value <= 0.0:
                raise ValueError(
                    f"RetainedThreadShutdownPolicy.{field_name} must be finite "
                    "and positive"
                )


@dataclass(frozen=True, slots=True)
class RetainedThreadActivated:
    """The target may execute and the retained owner must finalize it."""


@dataclass(frozen=True, slots=True)
class RetainedThreadActivationRejected:
    """Activation failed before the target could execute."""

    error: BaseException


@dataclass(frozen=True, slots=True)
class RetainedThreadActivationInterrupted:
    """Activation raised after the target acquired execution ownership."""

    error: BaseException


@dataclass(frozen=True, slots=True)
class RetainedThreadActivationIndeterminate:
    """The primitive may have started a target that has not acknowledged yet."""

    error: BaseException


RetainedThreadActivation = (
    RetainedThreadActivated
    | RetainedThreadActivationRejected
    | RetainedThreadActivationInterrupted
    | RetainedThreadActivationIndeterminate
)


@dataclass(frozen=True, slots=True)
class ThreadPrimitiveStarted:
    """The primitive authoritatively started the native thread."""


@dataclass(frozen=True, slots=True)
class ThreadPrimitiveRejected:
    """The primitive authoritatively rejected activation before native start."""

    error: BaseException


@dataclass(frozen=True, slots=True)
class ThreadPrimitiveIndeterminate:
    """The primitive was interrupted after native activation may have begun."""

    error: BaseException


ThreadPrimitiveActivation = (
    ThreadPrimitiveStarted | ThreadPrimitiveRejected | ThreadPrimitiveIndeterminate
)


@dataclass(frozen=True, slots=True)
class RetainedThreadFinalized:
    """The thread stopped without a join failure."""


@dataclass(frozen=True, slots=True)
class RetainedThreadFinalizedAfterFailure:
    """Recovery proved the thread stopped while preserving earlier failure."""

    error: BaseException


@dataclass(frozen=True, slots=True)
class RetainedThreadStillRunning:
    """Both bounded finalization attempts ended without proving completion."""

    error: BaseException


RetainedThreadFinalization = (
    RetainedThreadFinalized
    | RetainedThreadFinalizedAfterFailure
    | RetainedThreadStillRunning
)
