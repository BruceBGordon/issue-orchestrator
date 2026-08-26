"""Port for preparing a retained thread before activation."""

from __future__ import annotations

from collections.abc import Callable
import threading
from typing import Protocol, runtime_checkable

from ..domain.retained_thread import (
    RetainedThreadActivation,
    RetainedThreadFinalization,
    RetainedThreadShutdownPolicy,
    RetainedThreadSpec,
    RetainedThreadState,
    ThreadPrimitiveActivation,
)


@runtime_checkable
class RetainedThreadLease(Protocol):
    """Lifecycle owner retained before the underlying thread may start."""

    @property
    def state(self) -> RetainedThreadState: ...

    def activate(self) -> RetainedThreadActivation: ...

    def finalize(
        self,
        policy: RetainedThreadShutdownPolicy,
    ) -> RetainedThreadFinalization: ...


@runtime_checkable
class RetainedThreadFactory(Protocol):
    """Prepare one lifecycle owner without starting its target."""

    def prepare(
        self,
        spec: RetainedThreadSpec,
        target: Callable[[], None],
    ) -> RetainedThreadLease: ...


@runtime_checkable
class ThreadStartPrimitive(Protocol):
    """Authoritative adapter boundary around one native thread start."""

    def start(self, thread: threading.Thread) -> ThreadPrimitiveActivation: ...


@runtime_checkable
class ThreadNativeExitPrimitive(Protocol):
    """Observable seam immediately before the target's native thread returns."""

    def before_native_return(self) -> None: ...
