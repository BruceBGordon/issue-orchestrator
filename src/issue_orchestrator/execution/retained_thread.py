"""Deep owner for ambiguous thread activation and bounded finalization."""

from __future__ import annotations

import signal
import threading
from collections.abc import Callable

from ..domain.retained_thread import (
    RetainedThreadActivated,
    RetainedThreadActivation,
    RetainedThreadActivationIndeterminate,
    RetainedThreadActivationInterrupted,
    RetainedThreadActivationRejected,
    RetainedThreadFinalization,
    RetainedThreadFinalized,
    RetainedThreadFinalizedAfterFailure,
    RetainedThreadShutdownPolicy,
    RetainedThreadSpec,
    RetainedThreadState,
    RetainedThreadStillRunning,
    ThreadPrimitiveIndeterminate,
    ThreadPrimitiveRejected,
    ThreadPrimitiveStarted,
)
from ..ports.retained_thread import RetainedThreadLease, ThreadStartPrimitive


_ACTIVATION_SIGNALS = frozenset((signal.SIGHUP, signal.SIGINT, signal.SIGTERM))


class _RetainedTargetHealthy:
    """No infrastructure failure escaped the target wrapper."""


class _RetainedTargetFailed:
    """The target wrapper retained one escaped infrastructure failure."""

    def __init__(self, error: BaseException) -> None:
        self.error = error


_RetainedTargetResult = _RetainedTargetHealthy | _RetainedTargetFailed


class _ThreadFinalizationAttemptStopped:
    """One attempt observed the wrapper's terminal acknowledgement."""

    def __init__(self, failures: tuple[BaseException, ...]) -> None:
        self.failures = failures


class _ThreadFinalizationAttemptPending:
    """One bounded attempt ended before a terminal acknowledgement."""

    def __init__(self, failures: tuple[BaseException, ...]) -> None:
        if not failures:
            raise ValueError("pending thread finalization must retain a failure")
        self.failures = failures


_ThreadFinalizationAttempt = (
    _ThreadFinalizationAttemptStopped | _ThreadFinalizationAttemptPending
)


def _combined_thread_failure(
    operation: str,
    failures: tuple[BaseException, ...],
) -> BaseException:
    if not failures:
        raise ValueError("thread failure collection must not be empty")
    if len(failures) == 1:
        return failures[0]
    return BaseExceptionGroup(operation, failures)


class ThreadingRetainedThreadLease:
    """Retain a real ``threading.Thread`` across every lifecycle boundary."""

    def __init__(
        self,
        spec: RetainedThreadSpec,
        target: Callable[[], None],
        start_primitive: ThreadStartPrimitive,
    ) -> None:
        if type(spec) is not RetainedThreadSpec:
            raise ValueError(
                "ThreadingRetainedThreadLease.spec must be RetainedThreadSpec"
            )
        if not callable(target):
            raise ValueError("ThreadingRetainedThreadLease.target must be callable")
        if not isinstance(start_primitive, ThreadStartPrimitive):
            raise ValueError(
                "ThreadingRetainedThreadLease.start_primitive must implement "
                "ThreadStartPrimitive"
            )
        self._state_lock = threading.Lock()
        self._state = RetainedThreadState.CREATED
        self._activation_acknowledged = threading.Event()
        self._terminal = threading.Event()
        self._target = target
        self._start_primitive = start_primitive
        self._target_result: _RetainedTargetResult = _RetainedTargetHealthy()
        self._thread = threading.Thread(
            target=self._run_target,
            name=spec.name,
            daemon=spec.daemon,
        )

    @property
    def state(self) -> RetainedThreadState:
        with self._state_lock:
            return self._state

    def activate(self) -> RetainedThreadActivation:
        """Return typed ownership even if activation itself is interrupted."""
        if self.state is not RetainedThreadState.CREATED:
            raise RuntimeError("retained thread activation was attempted twice")
        activation = self._start_primitive.start(self._thread)
        if type(activation) is ThreadPrimitiveRejected:
            with self._state_lock:
                self._state = RetainedThreadState.REJECTED
            return RetainedThreadActivationRejected(activation.error)
        if type(activation) is ThreadPrimitiveIndeterminate:
            with self._state_lock:
                self._state = RetainedThreadState.ACTIVATING
            if self._activation_acknowledged.is_set():
                with self._state_lock:
                    self._state = RetainedThreadState.ACTIVATED
                return RetainedThreadActivationInterrupted(activation.error)
            return RetainedThreadActivationIndeterminate(activation.error)
        if type(activation) is not ThreadPrimitiveStarted:
            raise AssertionError("thread primitive activation is a closed union")
        with self._state_lock:
            self._state = RetainedThreadState.ACTIVATED
        return RetainedThreadActivated()

    def finalize(
        self,
        policy: RetainedThreadShutdownPolicy,
    ) -> RetainedThreadFinalization:
        """Try twice, preserving the first fault while proving completion."""
        if type(policy) is not RetainedThreadShutdownPolicy:
            raise ValueError(
                "ThreadingRetainedThreadLease.finalize.policy must be "
                "RetainedThreadShutdownPolicy"
            )
        if self.state not in (
            RetainedThreadState.ACTIVATING,
            RetainedThreadState.ACTIVATED,
        ):
            raise RuntimeError("cannot finalize a retained thread that never activated")
        failures: list[BaseException] = []
        for attempt_name, timeout_seconds in (
            ("initial", policy.initial_timeout_seconds),
            ("recovery", policy.recovery_timeout_seconds),
        ):
            attempt = self._finalization_attempt(attempt_name, timeout_seconds)
            failures.extend(attempt.failures)
            if type(attempt) is _ThreadFinalizationAttemptStopped:
                if not failures:
                    return RetainedThreadFinalized()
                return RetainedThreadFinalizedAfterFailure(
                    _combined_thread_failure(
                        "retained thread finalization recovered",
                        tuple(failures),
                    )
                )
            if type(attempt) is not _ThreadFinalizationAttemptPending:
                raise AssertionError("thread finalization attempt is a closed union")
        return RetainedThreadStillRunning(
            _combined_thread_failure(
                "retained thread finalization did not prove completion",
                tuple(failures),
            )
        )

    def _finalization_attempt(
        self,
        attempt_name: str,
        timeout_seconds: float,
    ) -> _ThreadFinalizationAttempt:
        failures: list[BaseException] = []
        if self.state is RetainedThreadState.ACTIVATING:
            if not self._activation_acknowledged.wait(timeout_seconds):
                return _ThreadFinalizationAttemptPending(
                    (
                        TimeoutError(
                            "retained thread target did not acknowledge "
                            f"{attempt_name} activation within "
                            f"{timeout_seconds:.3f}s"
                        ),
                    )
                )
            with self._state_lock:
                self._state = RetainedThreadState.ACTIVATED
        try:
            terminal = self._terminal.wait(timeout_seconds)
        except BaseException as error:
            error.add_note(f"retained thread {attempt_name} terminal wait failed")
            failures.append(error)
            terminal = self._terminal.is_set()
        if not terminal:
            failures.append(
                TimeoutError(
                    f"retained thread remained live after {attempt_name} "
                    f"{timeout_seconds:.3f}s terminal wait: {self._thread.name!r}"
                )
            )
            return _ThreadFinalizationAttemptPending(tuple(failures))
        try:
            self._thread.join(timeout=0.0)
        except BaseException as error:
            error.add_note(f"retained thread {attempt_name} reap failed")
            failures.append(error)
        with self._state_lock:
            target_result = self._target_result
        if type(target_result) is _RetainedTargetFailed:
            failures.append(target_result.error)
        elif type(target_result) is not _RetainedTargetHealthy:
            raise AssertionError("retained target result is a closed union")
        return _ThreadFinalizationAttemptStopped(tuple(failures))

    def _run_target(self) -> None:
        self._activation_acknowledged.set()
        try:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, _ACTIVATION_SIGNALS)
            self._target()
        except BaseException as error:
            with self._state_lock:
                self._target_result = _RetainedTargetFailed(error)
        finally:
            self._terminal.set()


class MaskedThreadStartPrimitive:
    """Block external signals until ``Thread.start`` owns a native thread."""

    def start(
        self,
        thread: threading.Thread,
    ) -> (
        ThreadPrimitiveStarted | ThreadPrimitiveRejected | ThreadPrimitiveIndeterminate
    ):
        if not isinstance(thread, threading.Thread):
            raise ValueError(
                "MaskedThreadStartPrimitive.thread must be threading.Thread"
            )
        try:
            previous_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK,
                _ACTIVATION_SIGNALS,
            )
        except BaseException as error:
            return ThreadPrimitiveRejected(error)
        activation: ThreadPrimitiveStarted | ThreadPrimitiveIndeterminate
        try:
            thread.start()
        except BaseException as error:
            activation = ThreadPrimitiveIndeterminate(error)
        else:
            activation = ThreadPrimitiveStarted()
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        except BaseException as restore_error:
            if type(activation) is ThreadPrimitiveStarted:
                return ThreadPrimitiveIndeterminate(restore_error)
            if type(activation) is not ThreadPrimitiveIndeterminate:
                raise AssertionError("thread primitive activation is a closed union")
            return ThreadPrimitiveIndeterminate(
                BaseExceptionGroup(
                    "thread start interruption and signal-mask restoration failed",
                    (activation.error, restore_error),
                )
            )
        return activation


class ThreadingRetainedThreadFactory:
    """Production construction adapter for retained threads."""

    def __init__(self, start_primitive: ThreadStartPrimitive) -> None:
        if not isinstance(start_primitive, ThreadStartPrimitive):
            raise ValueError(
                "ThreadingRetainedThreadFactory.start_primitive must implement "
                "ThreadStartPrimitive"
            )
        self._start_primitive = start_primitive

    def prepare(
        self,
        spec: RetainedThreadSpec,
        target: Callable[[], None],
    ) -> RetainedThreadLease:
        return ThreadingRetainedThreadLease(spec, target, self._start_primitive)
