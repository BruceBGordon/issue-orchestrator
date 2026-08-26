# pyright: strict
"""Deep lifecycle owner for unregistered PTYs and their completion watcher."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import NoReturn, Protocol, runtime_checkable

from ..domain.process_group import (
    OwnedProcessGroupLeader,
)
from ..domain.retained_thread import (
    RetainedThreadActivation,
    RetainedThreadFinalized,
    RetainedThreadFinalizedAfterFailure,
    RetainedThreadShutdownPolicy,
    RetainedThreadSpec,
    RetainedThreadState,
    RetainedThreadStillRunning,
)
from ..domain.terminal_session_lifecycle import (
    TerminalSessionWatcherCompleted,
    TerminalSessionWatcherFailed,
    TerminalSessionWatcherOutcome,
    TerminalSessionWatcherPolicy,
    TerminalSessionWatcherTimedOut,
)
from ..ports.process_group_supervisor import ProcessGroupSupervisor
from ..ports.retained_thread import RetainedThreadFactory, RetainedThreadLease
from ..ports.terminal_session_owner import TerminalSessionLaunchLease
from .agent_runner import AgentResult, AgentSession


logger = logging.getLogger(__name__)


class TerminalSessionWatcherShutdownError(RuntimeError):
    """A live-session watcher did not prove complete PTY finalization."""


def _require_process_group_supervisor(value: object) -> None:
    if not isinstance(value, ProcessGroupSupervisor):
        raise ValueError(
            "PendingTerminalSession.process_group_supervisor must implement "
            "ProcessGroupSupervisor"
        )


def _require_retained_thread_factory(value: object, owner: str) -> None:
    if not isinstance(value, RetainedThreadFactory):
        raise ValueError(f"{owner} must implement RetainedThreadFactory")


class PendingTerminalSession:
    """Own a spawned PTY leader until durable registry publication succeeds."""

    def __init__(
        self,
        session: AgentSession,
        process_group_supervisor: ProcessGroupSupervisor,
        launch_lease: TerminalSessionLaunchLease,
    ) -> None:
        if type(session) is not AgentSession:
            raise ValueError("PendingTerminalSession.session must be AgentSession")
        _require_process_group_supervisor(process_group_supervisor)
        self._session = session
        self._process_group_supervisor = process_group_supervisor
        self._launch_lease = launch_lease

    @property
    def session(self) -> AgentSession:
        """Return the PTY handle while this owner remains pending."""
        return self._session

    @property
    def process_id(self) -> int:
        """Return the exact process-group leader this owner can reap."""
        return self._session.pid

    def require_owner_ready(self) -> None:
        """Prove self-containment before durable session publication."""
        self._launch_lease.require_ready()

    def abort(self, preceding_error: BaseException) -> NoReturn:
        """Attempt containment, reaping, and descriptor closure before failing."""
        cleanup_errors: list[BaseException] = []
        group_contained = False
        try:
            termination = self._process_group_supervisor.abort(
                OwnedProcessGroupLeader(self.process_id)
            )
            group_contained = True
            courtesy_failure = termination.courtesy_failure()
            if courtesy_failure is not None:
                cleanup_errors.append(courtesy_failure.error)
        except BaseException as cleanup_error:
            cleanup_errors.append(cleanup_error)
        if group_contained:
            try:
                self._launch_lease.retire_after_containment()
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
            try:
                self._session.finalize_after_owned_process_group_reap()
            except BaseException as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            raise BaseExceptionGroup(
                "terminal session startup failed and cleanup was incomplete",
                [preceding_error, *cleanup_errors],
            )
        raise preceding_error


@dataclass(frozen=True, slots=True)
class _WatcherRunning:
    """The watcher thread has not published a terminal fact."""


@dataclass(frozen=True, slots=True)
class _WatcherCompleted:
    """The watcher completed with one exact agent result."""

    result: AgentResult

    def __post_init__(self) -> None:
        if type(self.result) is not AgentResult:
            raise ValueError("_WatcherCompleted.result must be AgentResult")


@dataclass(frozen=True, slots=True)
class _WatcherFailed:
    """The watcher stopped after its owned wait/finalization raised."""

    error: BaseException


_WatcherState = _WatcherRunning | _WatcherCompleted | _WatcherFailed


class TerminalSessionWatcher:
    """Own the only thread allowed to wait on and finalize one live PTY."""

    def __init__(
        self,
        session_name: str,
        session: AgentSession,
        thread_factory: RetainedThreadFactory,
    ) -> None:
        if type(session_name) is not str or not session_name:
            raise ValueError("TerminalSessionWatcher.session_name must not be empty")
        if type(session) is not AgentSession:
            raise ValueError("TerminalSessionWatcher.session must be AgentSession")
        _require_retained_thread_factory(
            thread_factory,
            "TerminalSessionWatcher.thread_factory",
        )
        self._session_name = session_name
        self._session = session
        self._state_lock = threading.Lock()
        self._state: _WatcherState = _WatcherRunning()
        self._thread: RetainedThreadLease = thread_factory.prepare(
            RetainedThreadSpec(
                name=f"terminal-session-watcher:{session_name}",
                daemon=True,
            ),
            self._watch,
        )

    @property
    def activation(self) -> RetainedThreadState:
        """Return whether the thread may own PTY wait/finalization."""
        return self._thread.state

    def activate(self) -> RetainedThreadActivation:
        """Start this already-retained owner, preserving post-start failures."""
        return self._thread.activate()

    def await_completion(
        self,
        policy: TerminalSessionWatcherPolicy,
    ) -> TerminalSessionWatcherOutcome:
        """Return a closed typed fact; never discard a still-live watcher."""
        if type(policy) is not TerminalSessionWatcherPolicy:
            raise ValueError(
                "TerminalSessionWatcher.await_completion.policy must be "
                "TerminalSessionWatcherPolicy"
            )
        if self.activation not in (
            RetainedThreadState.ACTIVATING,
            RetainedThreadState.ACTIVATED,
        ):
            raise RuntimeError("cannot await a terminal watcher before activation")
        finalization = self._thread.finalize(
            RetainedThreadShutdownPolicy(
                initial_timeout_seconds=policy.shutdown_timeout_seconds,
                recovery_timeout_seconds=policy.shutdown_timeout_seconds,
            )
        )
        if type(finalization) is RetainedThreadStillRunning:
            return TerminalSessionWatcherTimedOut(
                self._session_name,
                self._session.pid,
                policy.shutdown_timeout_seconds * 2,
                finalization.error,
            )
        if type(finalization) is RetainedThreadFinalizedAfterFailure:
            return TerminalSessionWatcherFailed(
                self._session_name,
                self._session.pid,
                finalization.error,
            )
        if type(finalization) is not RetainedThreadFinalized:
            raise AssertionError("retained thread finalization is a closed union")
        with self._state_lock:
            state = self._state
        if type(state) is _WatcherCompleted:
            return TerminalSessionWatcherCompleted(
                self._session_name,
                self._session.pid,
            )
        if type(state) is _WatcherFailed:
            return TerminalSessionWatcherFailed(
                self._session_name,
                self._session.pid,
                state.error,
            )
        raise AssertionError("a stopped terminal watcher must publish a terminal fact")

    def _watch(self) -> None:
        logger.info(
            "[subprocess] watcher started: session_name=%s pid=%s",
            self._session_name,
            self._session.pid,
        )
        try:
            result = self._session.wait()
        except BaseException as error:
            with self._state_lock:
                self._state = _WatcherFailed(error)
            logger.exception(
                "[subprocess] watcher failed: session_name=%s pid=%s",
                self._session_name,
                self._session.pid,
            )
            return
        with self._state_lock:
            self._state = _WatcherCompleted(result)
        logger.info(
            "[subprocess] watcher completed: session_name=%s pid=%s "
            "exit_code=%s timed_out=%s duration=%.1fs",
            self._session_name,
            self._session.pid,
            result.exit_code,
            result.timed_out,
            result.duration_seconds,
        )


@runtime_checkable
class TerminalSessionWatcherFactory(Protocol):
    """Typed construction boundary for the PTY completion owner."""

    def create(
        self,
        session_name: str,
        session: AgentSession,
    ) -> TerminalSessionWatcher: ...


class ThreadTerminalSessionWatcherFactory:
    """Production adapter that starts one watcher thread per live PTY."""

    def __init__(self, thread_factory: RetainedThreadFactory) -> None:
        _require_retained_thread_factory(
            thread_factory,
            "ThreadTerminalSessionWatcherFactory.thread_factory",
        )
        self._thread_factory = thread_factory

    def create(
        self,
        session_name: str,
        session: AgentSession,
    ) -> TerminalSessionWatcher:
        return TerminalSessionWatcher(session_name, session, self._thread_factory)
