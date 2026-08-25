"""Public lifecycle proofs for the retained-thread deep module."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from issue_orchestrator.domain.retained_thread import (
    RetainedThreadActivationIndeterminate,
    RetainedThreadActivationRejected,
    RetainedThreadFinalized,
    RetainedThreadShutdownPolicy,
    RetainedThreadSpec,
    RetainedThreadState,
    ThreadPrimitiveActivation,
    ThreadPrimitiveIndeterminate,
    ThreadPrimitiveRejected,
)
from issue_orchestrator.execution.retained_thread import (
    ThreadingRetainedThreadFactory,
)
from tests.process_completion_fixture import PROCESS_COMPLETION_WATCHDOG


@dataclass(slots=True)
class _DelayedIndeterminateStart:
    """Delay native start until after the caller receives indeterminate evidence."""

    interruption: KeyboardInterrupt
    launcher_entered: threading.Event = field(default_factory=threading.Event)
    release_native_start: threading.Event = field(default_factory=threading.Event)
    launcher: threading.Thread = field(init=False)

    def start(self, thread: threading.Thread) -> ThreadPrimitiveActivation:
        self.launcher = threading.Thread(
            target=self._launch,
            args=(thread,),
            name="delayed-retained-thread-launcher",
            daemon=True,
        )
        self.launcher.start()
        PROCESS_COMPLETION_WATCHDOG.wait_for_event(
            self.launcher_entered,
            operation="delayed retained-thread launcher entered",
        )
        return ThreadPrimitiveIndeterminate(self.interruption)

    def _launch(self, thread: threading.Thread) -> None:
        self.launcher_entered.set()
        PROCESS_COMPLETION_WATCHDOG.wait_for_event(
            self.release_native_start,
            operation="release delayed retained-thread native start",
        )
        thread.start()


@dataclass(frozen=True, slots=True)
class _RejectedStart:
    failure: RuntimeError

    def start(self, thread: threading.Thread) -> ThreadPrimitiveActivation:
        del thread
        return ThreadPrimitiveRejected(self.failure)


_SHUTDOWN_POLICY = RetainedThreadShutdownPolicy(
    initial_timeout_seconds=1.0,
    recovery_timeout_seconds=1.0,
)


def test_indeterminate_start_retains_until_delayed_target_acknowledges() -> None:
    interruption = KeyboardInterrupt("injected after native start request")
    primitive = _DelayedIndeterminateStart(interruption)
    target_ran = threading.Event()
    lease = ThreadingRetainedThreadFactory(primitive).prepare(
        RetainedThreadSpec(name="delayed-retained-target", daemon=True),
        target_ran.set,
    )

    activation = lease.activate()

    assert type(activation) is RetainedThreadActivationIndeterminate
    assert activation.error is interruption
    assert lease.state is RetainedThreadState.ACTIVATING

    primitive.release_native_start.set()
    finalization = lease.finalize(_SHUTDOWN_POLICY)
    PROCESS_COMPLETION_WATCHDOG.join_thread(
        primitive.launcher,
        operation="delayed retained-thread launcher completion",
    )

    assert type(finalization) is RetainedThreadFinalized
    assert target_ran.is_set()
    assert lease.state is RetainedThreadState.ACTIVATED


def test_authoritative_primitive_rejection_never_runs_target() -> None:
    failure = RuntimeError("native thread allocation rejected")
    target_ran = threading.Event()
    lease = ThreadingRetainedThreadFactory(_RejectedStart(failure)).prepare(
        RetainedThreadSpec(name="rejected-retained-target", daemon=True),
        target_ran.set,
    )

    activation = lease.activate()

    assert type(activation) is RetainedThreadActivationRejected
    assert activation.error is failure
    assert lease.state is RetainedThreadState.REJECTED
    assert not target_ran.is_set()
