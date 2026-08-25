"""State machine for development session lifecycle management.

This module implements the state machine for tracking a development session through
its lifecycle, from launch through completion or failure.

The state machine is designed to be pure - it returns TransitionResult instead of
publishing events directly. The caller (control layer) is responsible for emitting
TraceEvents via EventSink.
"""

import logging
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from transitions import MachineError, EventData, Machine

from .transition_result import TransitionResult
from .errors import InvalidStateTransition
from ..session_watchdog import (
    SYSTEM_SESSION_WATCHDOG_CLOCK,
    SessionWatchdogClock,
)

logger = logging.getLogger(__name__)


class SessionState(Enum):
    """States a development session can be in during its lifecycle."""

    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    SLOW = "slow"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BLOCKED = "blocked"
    NEEDS_HUMAN = "needs_human"


# NOTE:
# This class is the mutation target for `transitions.Machine`.
# It must remain a simple data object with no logic or recursion.
class _Model:
    """Internal transitions model.

    transitions injects trigger methods onto this object. The outer SessionStateMachine
    wrapper exposes a typed/stable API and prevents dynamic methods from leaking.
    """

    def __init__(self, initial_state: str) -> None:
        self.state = initial_state


class SessionStateMachine:
    """State machine for managing development session lifecycle.

    This state machine tracks a development session through the following states:
    - PENDING: Session is scheduled but not yet launched
    - STARTING: Session is being initialized
    - RUNNING: Session is actively running
    - SLOW: Session has been running longer than expected
    - COMPLETING: Session is in the process of wrapping up
    - COMPLETED: Session finished successfully
    - FAILED: Session encountered a fatal error
    - TIMED_OUT: Session exceeded maximum runtime
    - BLOCKED: Session is blocked waiting for resolution
    - NEEDS_HUMAN: Session needs human intervention

    The state machine is pure - transitions store their result in last_transition
    which callers use to emit appropriate TraceEvents via EventSink.

    Attributes:
        session_id: Unique identifier for this session
        issue_number: The GitHub issue number this session is working on
        state: Current state of the session
        started_at: Timestamp when session started running (None if not started)
        timeout_minutes: Maximum runtime before timing out (None for no timeout)
        last_transition: Result of the most recent transition (for event emission)
    """

    def __init__(
        self,
        session_id: str,
        issue_number: int,
        initial_state: SessionState = SessionState.PENDING,
        timeout_minutes: Optional[int] = None,
        watchdog_clock: SessionWatchdogClock = SYSTEM_SESSION_WATCHDOG_CLOCK,
    ):
        """Initialize the session state machine.

        Args:
            session_id: Unique identifier for this session
            issue_number: The GitHub issue number
            initial_state: Starting state (defaults to PENDING)
            timeout_minutes: Maximum runtime in minutes (None for no timeout)
        """
        self.session_id = session_id
        self.issue_number = issue_number
        self._model = _Model(initial_state.value)
        self.state = self._model.state
        self.started_at: Optional[datetime] = None
        self.started_at_monotonic: Optional[float] = None
        self.timeout_minutes = timeout_minutes
        self.last_transition: Optional[TransitionResult] = None
        if type(watchdog_clock) is not SessionWatchdogClock:
            raise ValueError(
                "SessionStateMachine.watchdog_clock must be a "
                "SessionWatchdogClock"
            )
        self._watchdog_clock = watchdog_clock

        # Define all possible states
        states = [state.value for state in SessionState]

        # Define state transitions
        transitions = [
            # Launch a pending session
            {
                'trigger': 'launch',
                'source': SessionState.PENDING.value,
                'dest': SessionState.STARTING.value,
                'after': self._on_launched
            },
            # Session successfully started
            {
                'trigger': 'started',
                'source': SessionState.STARTING.value,
                'dest': SessionState.RUNNING.value,
                'after': self._on_started
            },
            # Mark a running session as slow
            {
                'trigger': 'mark_slow',
                'source': SessionState.RUNNING.value,
                'dest': SessionState.SLOW.value,
                'after': self._on_slow
            },
            # Complete a running or slow session
            {
                'trigger': 'complete',
                'source': [SessionState.RUNNING.value, SessionState.SLOW.value],
                'dest': SessionState.COMPLETED.value,
                'after': self._on_completed
            },
            # Session failed during startup, running, or slow states
            {
                'trigger': 'fail',
                'source': [SessionState.STARTING.value, SessionState.RUNNING.value, SessionState.SLOW.value],
                'dest': SessionState.FAILED.value,
                'after': self._on_failed
            },
            # Session timed out
            {
                'trigger': 'timeout',
                'source': [SessionState.RUNNING.value, SessionState.SLOW.value],
                'dest': SessionState.TIMED_OUT.value,
                'after': self._on_timed_out
            },
            # Session blocked
            {
                'trigger': 'block',
                'source': SessionState.RUNNING.value,
                'dest': SessionState.BLOCKED.value,
                'after': self._on_blocked
            },
            # Session needs human intervention
            {
                'trigger': 'needs_human',
                'source': SessionState.RUNNING.value,
                'dest': SessionState.NEEDS_HUMAN.value,
                'after': self._on_needs_human
            },
            # Resume from blocked or needs_human states
            {
                'trigger': 'resume',
                'source': [SessionState.BLOCKED.value, SessionState.NEEDS_HUMAN.value],
                'dest': SessionState.RUNNING.value,
                'after': self._on_resumed
            }
        ]

        # Create the state machine with internal model
        self.machine = Machine(
            model=self._model,
            states=states,
            transitions=transitions,
            initial=initial_state.value,
            send_event=True,
            auto_transitions=False
        )

        logger.info(f"SessionStateMachine initialized for session {session_id} in state {initial_state.value}")

    def _on_launched(self, event: EventData) -> None:
        """Callback for launch transition."""
        data = event.kwargs.get('data', {})
        transition_data = {**data, 'session_id': self.session_id}

        self.last_transition = TransitionResult(
            success=True,
            from_state=SessionState.PENDING.value,
            to_state=SessionState.STARTING.value,
            event_name="session.launched",
            entity_id=self.issue_number,
            data=transition_data,
        )
        logger.info(f"Session {self.session_id} launched for issue {self.issue_number}")

    def _on_started(self, event: EventData) -> None:
        """Callback for started transition."""
        self.started_at = self._watchdog_clock.wall_now()
        self.started_at_monotonic = self._watchdog_clock.require_monotonic_now()
        data = event.kwargs.get('data', {})
        transition_data = {**data, 'session_id': self.session_id, 'started_at': self.started_at.isoformat()}

        self.last_transition = TransitionResult(
            success=True,
            from_state=SessionState.STARTING.value,
            to_state=SessionState.RUNNING.value,
            event_name="session.started",
            entity_id=self.issue_number,
            data=transition_data,
        )
        logger.info(f"Session {self.session_id} started for issue {self.issue_number}")

    def _on_slow(self, event: EventData) -> None:
        """Callback for mark_slow transition."""
        data = event.kwargs.get('data', {})
        runtime = self.get_runtime_minutes()
        transition_data = {**data, 'session_id': self.session_id, 'runtime_minutes': runtime}

        self.last_transition = TransitionResult(
            success=True,
            from_state=SessionState.RUNNING.value,
            to_state=SessionState.SLOW.value,
            event_name="session.slow",
            entity_id=self.issue_number,
            data=transition_data,
        )
        logger.warning(f"Session {self.session_id} marked as slow (runtime: {runtime} minutes)")

    def _on_completed(self, event: EventData) -> None:
        """Callback for complete transition."""
        data = event.kwargs.get('data', {})
        runtime = self.get_runtime_minutes()
        from_state = event.transition.source if hasattr(event, 'transition') and event.transition else SessionState.RUNNING.value
        transition_data = {**data, 'session_id': self.session_id, 'runtime_minutes': runtime}

        self.last_transition = TransitionResult(
            success=True,
            from_state=from_state,
            to_state=SessionState.COMPLETED.value,
            event_name="session.completed",
            entity_id=self.issue_number,
            data=transition_data,
        )
        logger.info(f"Session {self.session_id} completed (runtime: {runtime} minutes)")

    def _on_failed(self, event: EventData) -> None:
        """Callback for fail transition."""
        data = event.kwargs.get('data', {})
        runtime = self.get_runtime_minutes()
        from_state = event.transition.source if hasattr(event, 'transition') and event.transition else SessionState.RUNNING.value
        transition_data = {**data, 'session_id': self.session_id, 'runtime_minutes': runtime}

        self.last_transition = TransitionResult(
            success=True,
            from_state=from_state,
            to_state=SessionState.FAILED.value,
            event_name="session.failed",
            entity_id=self.issue_number,
            data=transition_data,
        )
        logger.error(f"Session {self.session_id} failed (runtime: {runtime} minutes)")

    def _on_timed_out(self, event: EventData) -> None:
        """Callback for timeout transition."""
        data = event.kwargs.get('data', {})
        runtime = self.get_runtime_minutes()
        from_state = event.transition.source if hasattr(event, 'transition') and event.transition else SessionState.RUNNING.value
        transition_data = {**data, 'session_id': self.session_id, 'runtime_minutes': runtime}

        self.last_transition = TransitionResult(
            success=True,
            from_state=from_state,
            to_state=SessionState.TIMED_OUT.value,
            event_name="session.timeout",
            entity_id=self.issue_number,
            data=transition_data,
        )
        logger.error(f"Session {self.session_id} timed out (runtime: {runtime} minutes)")

    def _on_blocked(self, event: EventData) -> None:
        """Callback for block transition."""
        data = event.kwargs.get('data', {})
        transition_data = {**data, 'session_id': self.session_id}

        self.last_transition = TransitionResult(
            success=True,
            from_state=SessionState.RUNNING.value,
            to_state=SessionState.BLOCKED.value,
            event_name="session.blocked",
            entity_id=self.issue_number,
            data=transition_data,
        )
        logger.warning(f"Session {self.session_id} blocked")

    def _on_needs_human(self, event: EventData) -> None:
        """Callback for needs_human transition."""
        data = event.kwargs.get('data', {})
        transition_data = {**data, 'session_id': self.session_id}

        self.last_transition = TransitionResult(
            success=True,
            from_state=SessionState.RUNNING.value,
            to_state=SessionState.NEEDS_HUMAN.value,
            event_name="session.needs_human",
            entity_id=self.issue_number,
            data=transition_data,
        )
        logger.warning(f"Session {self.session_id} needs human intervention")

    def _on_resumed(self, event: EventData) -> None:
        """Callback for resume transition."""
        from_state = event.transition.source if hasattr(event, 'transition') and event.transition else SessionState.BLOCKED.value

        self.last_transition = TransitionResult(
            success=True,
            from_state=from_state,
            to_state=SessionState.RUNNING.value,
            event_name="session.resumed",
            entity_id=self.issue_number,
            data={'session_id': self.session_id},
        )

        logger.info(f"Session {self.session_id} resumed")

    def get_runtime_minutes(self) -> Optional[float]:
        """Calculate runtime in minutes since session started.

        Returns:
            Runtime in minutes, or None if session hasn't started
        """
        if self.started_at_monotonic is None:
            return None
        elapsed_seconds = (
            self._watchdog_clock.require_monotonic_now()
            - self.started_at_monotonic
        )
        if elapsed_seconds < 0:
            raise RuntimeError(
                "session watchdog monotonic clock moved backward after start"
            )
        return elapsed_seconds / 60.0

    def check_timeout(self) -> bool:
        """Check if the session has exceeded its timeout.

        If the session has timed out and is in a running or slow state,
        automatically triggers the timeout transition.

        Returns:
            True if session has timed out, False otherwise
        """
        if self.timeout_minutes is None:
            return False

        runtime = self.get_runtime_minutes()
        if runtime is None:
            return False

        if runtime > self.timeout_minutes:
            current_state = self.get_state()
            if current_state in [SessionState.RUNNING, SessionState.SLOW]:
                logger.warning(
                    f"Session {self.session_id} exceeded timeout "
                    f"({runtime:.1f} > {self.timeout_minutes} minutes)"
                )
                self.timeout(data={'runtime_minutes': runtime, 'timeout_minutes': self.timeout_minutes})
                return True

        return False

    def get_state(self) -> SessionState:
        """Get the current state as an enum.

        Returns:
            Current SessionState enum value
        """
        return SessionState(self.state)

    def can_transition(self, trigger: str) -> bool:
        """Check if a transition is valid from the current state.

        Args:
            trigger: Name of the transition to check

        Returns:
            True if the transition is valid, False otherwise
        """
        trigger_func = getattr(self._model, f'may_{trigger}', None)
        if trigger_func and callable(trigger_func):
            return bool(trigger_func())
        return False

    def get_runtime_info(self) -> dict[str, Any]:
        """Get runtime information for this session.

        Returns:
            Dictionary with runtime information including:
            - started_at: ISO timestamp when session started (or None)
            - runtime_minutes: Current runtime in minutes (or None)
            - timeout_minutes: Configured timeout (or None)
            - is_timed_out: Whether session has exceeded timeout
        """
        runtime = self.get_runtime_minutes()
        is_timed_out = False
        if self.timeout_minutes is not None and runtime is not None:
            is_timed_out = runtime > self.timeout_minutes

        return {
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'runtime_minutes': runtime,
            'timeout_minutes': self.timeout_minutes,
            'is_timed_out': is_timed_out
        }

    # -------------------------------------------------------------------------
    # Typed transition methods (quarantine transitions' dynamic surface)
    # -------------------------------------------------------------------------

    def _invoke(self, name: str, **kwargs: Any) -> None:
        """Invoke a transitions-injected trigger on the internal model.

        This quarantines transitions' dynamic surface area inside the wrapper.
        """
        try:
            fn = getattr(self._model, name)
            fn(**kwargs)
        except MachineError as e:
            raise InvalidStateTransition(str(e)) from e
        finally:
            # Keep backward-compatible `state` attribute in sync
            self.state = self._model.state

    def launch(self, **kwargs: Any) -> None:
        """Launch the session."""
        self._invoke('launch', **kwargs)

    def started(self, **kwargs: Any) -> None:
        """Mark session as started."""
        self._invoke('started', **kwargs)

    def mark_slow(self, **kwargs: Any) -> None:
        """Mark session as running slow."""
        self._invoke('mark_slow', **kwargs)

    def complete(self, **kwargs: Any) -> None:
        """Mark session as completed."""
        self._invoke('complete', **kwargs)

    def fail(self, **kwargs: Any) -> None:
        """Mark session as failed."""
        self._invoke('fail', **kwargs)

    def timeout(self, **kwargs: Any) -> None:
        """Mark session as timed out."""
        self._invoke('timeout', **kwargs)

    def block(self, **kwargs: Any) -> None:
        """Mark session as blocked."""
        self._invoke('block', **kwargs)

    def needs_human(self, **kwargs: Any) -> None:
        """Mark session as needing human."""
        self._invoke('needs_human', **kwargs)

    def resume(self, **kwargs: Any) -> None:
        """Resume a blocked session."""
        self._invoke('resume', **kwargs)
