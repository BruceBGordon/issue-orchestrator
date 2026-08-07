"""Session observation and completion handling.

Naming convention (from architecture review):
- "Observer" implies non-authoritative fact-gathering
- Observers observe, they don't decide
- Decisions belong in Controllers (LifecycleController)

Components that observe are named Observers;
Components that decide are named Controllers;
Components that act are named Adapters.
"""

import logging
import time
from datetime import datetime
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..control.label_manager import LabelManager
    from ..domain.state_machines.session_machine import SessionStateMachine
    from ..ports import RepositoryHost, SessionRunner, TerminalObserver
    from ..ports.fresh_issue_reader import FreshIssueReader
    from ..ports.issue import Issue

from ..domain import ProcessState
from ..domain.process_state import ProcessExitInfo
from ..infra.config import Config
from ..infra.logging_config import issue_log
from ..events import EventName
from ..domain.models import Session, SessionStatus
from ..ports import EventSink, TraceEvent, NullEventSink
from ..ports.provider_readiness import (
    NO_PROVIDER_READINESS_PROBE,
    ProviderReadinessProbe,
)
from ..ports.session_output import SessionOutput
from .observation import SessionObservation, SessionObservationResult

logger = logging.getLogger(__name__)

# A provider login banner renders within seconds of launch (2740 ms and 522 ms
# in the two recordings that motivated #6999), so an auth check outside the
# first few minutes cannot be about *this* launch's credentials. The window is
# a cost guard rather than a correctness guard — the diagnosis is confirmed by
# the provider's own probe either way — so it lives here as a constant rather
# than becoming another knob nobody tunes.
PROVIDER_AUTH_CHECK_WINDOW_SECONDS = 300

# Only the head of the log can hold a launch-time banner; reading more would
# just feed the agent's own working output into the signature match.
PROVIDER_AUTH_CHECK_MAX_BYTES = 8192


class SessionObserver:
    """Observe running sessions and gather facts about their state.

    This class observes sessions and returns facts (SessionStatus).
    It does NOT make policy decisions - that's the controller's job.

    Note: handle_completion() currently contains some policy (which labels
    to add/remove). This should eventually move to LifecycleController,
    with this class only doing observation.
    """

    def __init__(
        self,
        config: Config,
        session_output: SessionOutput,
        session_machines: dict[str, "SessionStateMachine"] | None = None,
        events: EventSink | None = None,
        session_runner: Optional["SessionRunner"] = None,
        repository_host: Optional["RepositoryHost"] = None,
        fresh_issue_reader: Optional["FreshIssueReader"] = None,
        terminal_observer: Optional["TerminalObserver"] = None,
        label_manager: "LabelManager | None" = None,
        provider_readiness_probe: ProviderReadinessProbe = NO_PROVIDER_READINESS_PROBE,
    ) -> None:
        """Initialize the observer with configuration.

        Args:
            config: Orchestrator configuration
            session_output: SessionOutput port for session artifacts
            session_machines: Optional dict mapping session names to state machines
            events: Optional EventSink for emitting trace events
            session_runner: SessionRunner port for terminal operations
            repository_host: RepositoryHost port for GitHub operations
            fresh_issue_reader: FreshIssueReader port for correctness-critical reads
            terminal_observer: Optional TerminalObserver for process state detection
            provider_readiness_probe: Typed provider-readiness boundary (#6999).
                Defaults to the explicit "nothing to probe" reader, which never
                reports an auth failure.
        """
        self.config = config
        self._provider_readiness_probe = provider_readiness_probe
        self.session_machines = session_machines or {}
        self.events = events or NullEventSink()
        self._session_runner = session_runner
        self._repository_host = repository_host
        self._fresh_issue_reader = fresh_issue_reader
        self._terminal_observer = terminal_observer
        self._session_output = session_output
        if label_manager is None:
            from ..control.label_manager import LabelManager
            label_manager = LabelManager(config)
        self._lm = label_manager

    def _extract_session_number(self, session_name: str) -> int:
        """Extract the numeric ID from a session name (handles both issue- and review- prefixes)."""
        if session_name.startswith("issue-"):
            return int(session_name.replace("issue-", ""))
        elif session_name.startswith("review-"):
            return int(session_name.replace("review-", ""))
        elif session_name.startswith("rework-"):
            return int(session_name.replace("rework-", ""))
        elif session_name.startswith("tech-lead-"):
            return int(session_name.replace("tech-lead-", ""))
        else:
            raise ValueError(f"Unknown session name format: {session_name}")

    def _session_exists_by_name(self, session_name: str) -> bool:
        """Check if a session exists by its full name (e.g., 'review-456')."""
        if self._session_runner is None:
            return False
        return self._session_runner.session_exists_by_name(session_name)

    def _send_exit_to_session_by_name(self, session_name: str) -> bool:
        """Send /exit command to a session by name."""
        if self._session_runner is None:
            return False
        return self._session_runner.send_to_session_by_name(session_name, "/exit")

    def _get_open_prs_for_branch(self, branch: str) -> list:
        """Get open PRs for a branch using the repository host."""
        if self._repository_host is None:
            return []
        return self._repository_host.get_prs_for_branch(branch, state="open")

    def _get_issue_labels(self, issue_number: int) -> list[str]:
        """Get labels for an issue using the repository host."""
        if self._fresh_issue_reader is None:
            return []
        return self._fresh_issue_reader.read_issue_labels(issue_number)

    def _get_runtime_and_timeout(
        self, session: Session
    ) -> tuple[Optional[float], Optional[int]]:
        """Get runtime and timeout values from machine or session."""
        machine = self.session_machines.get(session.terminal_id)
        if machine:
            return machine.get_runtime_minutes(), machine.timeout_minutes
        return session.runtime_minutes, session.agent_config.timeout_minutes

    def _is_timeout_exceeded(
        self, session: Session, runtime: Optional[float]
    ) -> bool:
        """Check if the session has exceeded its timeout."""
        machine = self.session_machines.get(session.terminal_id)
        if machine and machine.timeout_minutes:
            if runtime and runtime > machine.timeout_minutes:
                return True
        elif session.is_timed_out:
            return True
        return False

    def _check_process_state(
        self, session: Session
    ) -> tuple[bool | None, bool, ProcessExitInfo | None]:
        """Check process state via terminal observer.

        Returns:
            Tuple of (process_alive, detection_authoritative, exit_info)
            - process_alive: True/False/None if can't determine
            - detection_authoritative: True if pane_dead gave definitive answer
            - exit_info: Exit info if process exited
        """
        process_alive: bool | None = None
        detection_authoritative = False
        exit_info = None

        if self._terminal_observer:
            process_state = self._terminal_observer.get_process_state(
                session.terminal_id
            )
            if process_state == ProcessState.RUNNING:
                process_alive = True
                detection_authoritative = True
            elif process_state in (ProcessState.EXITED, ProcessState.SIGNALED):
                process_alive = False
                detection_authoritative = True
                exit_info = self._terminal_observer.get_exit_info(session.terminal_id)
                if exit_info:
                    logger.info(
                        issue_log(session.issue.number, "Process exited (pane_dead): %s"),
                        exit_info,
                    )

        return process_alive, detection_authoritative, exit_info

    def _check_completion_json(
        self,
        session: Session,
        exists: bool,
        runtime: Optional[float],
        timeout: Optional[int],
        timeout_exceeded: bool,
    ) -> SessionObservationResult | None:
        """Check for valid completion.json and return result if found.

        Returns:
            SessionObservationResult for a valid completion, None otherwise.
        """
        import json

        completion_path = session.worktree_path / session.completion_path
        if not completion_path.exists():
            return None

        try:
            with open(completion_path) as f:
                data = json.load(f)
            required_fields = ["session_id", "timestamp", "outcome", "summary"]
            if all(k in data for k in required_fields):
                # Detection is observed every tick while the session waits in
                # deferred states (e.g. background review exchange). Emit the
                # event and info log only once per session — the controller
                # still re-evaluates on every terminated() return.
                if session.completion_detected_at is None:
                    logger.info(
                        issue_log(
                            session.issue.number,
                            "Valid completion.json detected: outcome=%s",
                        ),
                        data.get("outcome"),
                    )
                    self.events.publish(
                        TraceEvent(
                            EventName.OBSERVATION_COMPLETION_DETECTED,
                            {
                                "issue_number": session.issue.number,
                                "session_name": session.terminal_id,
                                "outcome": data.get("outcome"),
                                "session_exists": exists,
                            },
                        )
                    )
                    session.completion_detected_at = datetime.now()
                if timeout_exceeded:
                    return SessionObservationResult.timed_out(
                        runtime_minutes=runtime,
                        timeout_minutes=timeout,
                        session_exists=exists,
                    )
                return SessionObservationResult.terminated(runtime_minutes=runtime)
            else:
                logger.debug(
                    "completion.json missing required fields, treating as incomplete"
                )
        except (json.JSONDecodeError, OSError) as e:
            logger.debug(f"completion.json not yet valid (partial write?): {e}")

        return None

    def _try_send_exit_if_has_pr(self, session: Session) -> None:
        """Send /exit to session if it has an open PR but is still running."""
        if session.exit_sent:
            return
        try:
            prs = self._get_open_prs_for_branch(session.branch_name)
            if prs:
                logger.info(
                    issue_log(
                        session.issue.number, "Has PR but still running - sending /exit"
                    ),
                )
                if self._send_exit_to_session_by_name(session.terminal_id):
                    session.exit_sent = True
        except Exception as e:
            logger.warning(
                issue_log(session.issue.number, "Could not check for PRs: %s"), e
            )
            self.events.publish(
                TraceEvent(
                    EventName.APPLY_FAILED,
                    {
                        "step_type": "observer_pr_check",
                        "issue_number": session.issue.number,
                        "branch": session.branch_name,
                        "error": str(e),
                    },
                )
            )

    def _check_grace_period(
        self, session: Session, runtime: Optional[float]
    ) -> SessionObservationResult | None:
        """Check if session should be kept alive via grace period.

        Returns:
            SessionObservationResult.running() if grace period applies, None otherwise
        """
        grace_period = self.config.session_grace_period_seconds
        log_activity_threshold = self.config.session_log_activity_seconds
        session_age = (datetime.now() - session.started_at).total_seconds()

        log_path = (
            self._session_output.get_log_path(
                session.worktree_path, session.terminal_id
            )
            if self._session_output
            else None
        )
        log_is_progressing = False
        log_age = float("inf")
        if log_path and log_path.exists():
            try:
                log_mtime = log_path.stat().st_mtime
                log_age = time.time() - log_mtime
                log_is_progressing = log_age < log_activity_threshold
            except OSError:
                pass

        if session_age < grace_period:
            logger.info(
                issue_log(
                    session.issue.number,
                    "GRACE_PERIOD: session only %.0fs old (< %ds grace) - treating as running",
                ),
                session_age,
                grace_period,
            )
            return SessionObservationResult.running(runtime_minutes=runtime)

        if log_is_progressing:
            logger.info(
                issue_log(
                    session.issue.number,
                    "LOG_ACTIVE: log modified %.0fs ago (< %ds threshold) - treating as running",
                ),
                log_age,
                log_activity_threshold,
            )
            return SessionObservationResult.running(runtime_minutes=runtime)

        return None

    def _capture_terminal_output_on_termination(self, session: Session) -> None:
        """Capture terminal output when session terminates without completion."""
        completion_path = session.worktree_path / session.completion_path
        if completion_path.exists() or not self._session_runner:
            return

        try:
            terminal_output = self._session_runner.get_session_output(
                session.issue.number,
                lines=100,
                session_name=session.terminal_id,
            )
            if terminal_output:
                truncated = (
                    terminal_output[-2000:]
                    if len(terminal_output) > 2000
                    else terminal_output
                )
                logger.warning(
                    issue_log(
                        session.issue.number,
                        "Terminated without completion. Terminal output:\n%s",
                    ),
                    truncated,
                )
        except Exception as e:
            logger.debug(
                issue_log(session.issue.number, "Could not capture terminal output: %s"),
                e,
            )

    def _emit_observation_event(
        self,
        session: Session,
        result: SessionObservationResult,
        exit_info: ProcessExitInfo | None,
    ) -> None:
        """Emit observation result event for debugging."""
        if result.observation == SessionObservation.RUNNING:
            return

        completion_path = session.worktree_path / session.completion_path
        event_data = {
            "issue_number": session.issue.number,
            "session_name": session.terminal_id,
            "observation": result.observation.value,
            "session_exists": result.session_exists,
            "runtime_minutes": result.runtime_minutes,
            "timeout_minutes": result.timeout_minutes,
            "worktree_path": str(session.worktree_path),
            "completion_json_exists": completion_path.exists(),
        }
        if exit_info:
            event_data["exit_code"] = exit_info.exit_code
            event_data["exit_signal"] = exit_info.signal
        self.events.publish(TraceEvent(EventName.OBSERVATION_RESULT, event_data))

    def observe_session(self, session: Session) -> SessionObservationResult:
        """Observe a session and return facts about its state.

        This method only gathers facts. It does NOT decide outcomes.
        The controller uses these observations + completion.json to decide.

        Detection hierarchy:
        1. PRIMARY: Process state via terminal observer (pane_dead attribute)
           - RUNNING: process is alive
           - EXITED/SIGNALED: process terminated, capture exit info
        2. SECONDARY: completion.json for agent outcome
        3. TERTIARY: Window existence (fallback for terminals without pane_dead)

        Returns:
            SessionObservationResult with observed facts:
            - RUNNING: Session exists and not timed out
            - TERMINATED: Session no longer exists
            - TIMED_OUT: Session exceeded timeout (may or may not exist)
        """
        runtime, timeout = self._get_runtime_and_timeout(session)
        timeout_exceeded = self._is_timeout_exceeded(session, runtime)
        process_alive, detection_authoritative, exit_info = self._check_process_state(
            session
        )

        # FALLBACK: Check if window exists (for terminals without pane_dead support)
        exists = self._session_exists_by_name(session.terminal_id)
        if process_alive is None:
            process_alive = exists

        # Check for completion.json
        completion_result = self._check_completion_json(
            session, exists, runtime, timeout, timeout_exceeded
        )
        if completion_result:
            return completion_result

        # If session is running and has PR, try to send /exit
        if process_alive:
            self._try_send_exit_if_has_pr(session)

        # Build observation result
        result = self._build_observation_result(
            session, runtime, timeout, timeout_exceeded, process_alive,
            detection_authoritative, exists
        )

        self._emit_observation_event(session, result, exit_info)
        return result

    def _build_observation_result(
        self,
        session: Session,
        runtime: Optional[float],
        timeout: Optional[int],
        timeout_exceeded: bool,
        process_alive: bool,
        detection_authoritative: bool,
        exists: bool,
    ) -> SessionObservationResult:
        """Build the observation result based on gathered facts."""
        if timeout_exceeded:
            return SessionObservationResult.timed_out(
                runtime_minutes=runtime,
                timeout_minutes=timeout,
                session_exists=exists,
            )

        if process_alive:
            auth_result = self._check_provider_auth(session, runtime)
            if auth_result is not None:
                return auth_result
            self._emit_no_output_if_stale(session)
            return SessionObservationResult.running(runtime_minutes=runtime)

        # Process appears dead - check grace period if detection is uncertain
        if not detection_authoritative:
            grace_result = self._check_grace_period(session, runtime)
            if grace_result:
                return grace_result

        # Authoritative detection says dead, or grace period didn't apply
        if detection_authoritative:
            self._capture_terminal_output_on_termination(session)

        return SessionObservationResult.terminated(runtime_minutes=runtime)

    def _check_provider_auth(
        self, session: Session, runtime: Optional[float]
    ) -> SessionObservationResult | None:
        """Observe whether this live session's provider is authenticated.

        Fact-gathering only: the observer hands the session's early output to
        the typed provider-readiness boundary and reports back whatever verdict
        the *provider adapter* reached. It keeps no banner list of its own, and
        the adapter confirms any signature against the provider's credential
        probe — so an agent that merely echoes a provider's auth banner (this
        orchestrator working on its own auth tooling does exactly that) cannot
        kill its own session (#6999).
        """
        provider = session.agent_config.provider
        if not provider:
            return None
        session_age = (datetime.now() - session.started_at).total_seconds()
        if session_age > PROVIDER_AUTH_CHECK_WINDOW_SECONDS:
            return None
        log_path = (
            self._session_output.get_log_path(session.worktree_path, session.terminal_id)
            if self._session_output
            else None
        )
        if not log_path or not log_path.exists():
            return None
        head = self._read_log_head(log_path, PROVIDER_AUTH_CHECK_MAX_BYTES)
        if not head:
            return None

        readiness = self._provider_readiness_probe.diagnose_session_output(
            provider, head
        )
        if not readiness.human_fixable:
            return None

        logger.error(
            issue_log(
                session.issue.number,
                "PROVIDER_AUTH_FAILED: session=%s provider=%s detail=%s "
                "age=%.0fs - failing instead of burning the timeout",
            ),
            session.terminal_id,
            provider,
            readiness.detail,
            session_age,
        )
        self.events.publish(TraceEvent(
            EventName.SESSION_LAUNCH_FAILED_AUTH,
            {
                "issue_number": session.issue.number,
                "session_name": session.terminal_id,
                "provider": provider,
                "readiness": readiness.state.value,
                "detail": readiness.detail,
                "human_fixable": readiness.human_fixable,
                "session_age_seconds": int(session_age),
            },
        ))
        return SessionObservationResult.provider_auth_failed(
            readiness, runtime_minutes=runtime, session_exists=True
        )

    @staticmethod
    def _read_log_head(log_path, max_bytes: int) -> str:
        """Read the first ``max_bytes`` of a session log, tolerating decode noise."""
        try:
            with open(log_path, "rb") as handle:
                raw = handle.read(max_bytes)
        except OSError:
            return ""
        return raw.decode("utf-8", errors="replace")

    def _emit_no_output_if_stale(self, session: Session) -> None:
        """Emit a session_no_output event if the session log is idle too long."""
        log_path = (
            self._session_output.get_log_path(session.worktree_path, session.terminal_id)
            if self._session_output else None
        )
        if not log_path or not log_path.exists():
            return

        try:
            stat = log_path.stat()
        except OSError:
            return

        changed = (
            session.last_log_mtime is None
            or session.last_log_size is None
            or stat.st_mtime != session.last_log_mtime
            or stat.st_size != session.last_log_size
        )
        if changed:
            session.last_log_mtime = stat.st_mtime
            session.last_log_size = stat.st_size
            session.last_output_monotonic = time.monotonic()
            session.last_output_at = time.time()
            session.last_output_tail = self._read_log_tail(
                log_path,
                self.config.session_no_output_tail_lines,
                self.config.session_no_output_max_bytes,
            )
            session.last_no_output_monotonic = None
            return

        if session.last_output_monotonic is None:
            return

        now = time.monotonic()
        idle_seconds = now - session.last_output_monotonic
        if idle_seconds < self.config.session_no_output_seconds:
            return

        if session.last_no_output_monotonic is not None:
            if now - session.last_no_output_monotonic < self.config.session_no_output_repeat_seconds:
                return

        session.last_no_output_monotonic = now
        payload = {
            "issue_number": session.issue.number,
            "session_name": session.terminal_id,
            "idle_seconds": int(idle_seconds),
            "last_output_at": session.last_output_at,
            "worktree_path": str(session.worktree_path),
            "log_path": str(log_path),
            "tail": session.last_output_tail or "",
        }
        logger.warning(
            issue_log(
                session.issue.number,
                "SESSION_NO_OUTPUT: session=%s idle=%ss log=%s size=%s last_output_at=%s tail=%r",
            ),
            session.terminal_id,
            int(idle_seconds),
            log_path,
            stat.st_size,
            session.last_output_at,
            (session.last_output_tail or "")[-200:],
        )
        self.events.publish(TraceEvent(EventName.SESSION_NO_OUTPUT, payload))

    def _read_log_tail(self, log_path, tail_lines: int, max_bytes: int) -> str:
        try:
            content = log_path.read_text()
        except Exception:
            return ""
        lines = content.splitlines()
        tail = "\n".join(lines[-tail_lines:])
        if len(tail.encode("utf-8")) > max_bytes:
            tail = tail.encode("utf-8")[-max_bytes:].decode("utf-8", errors="replace")
        return tail

    def _check_timeout_status(self, session: Session) -> SessionStatus | None:
        """Check if session has timed out.

        Returns:
            SessionStatus.TIMED_OUT if timed out, None otherwise
        """
        machine = self.session_machines.get(session.terminal_id)
        if machine and machine.check_timeout():
            logger.info(
                issue_log(
                    session.issue.number,
                    "Session timed out (state_machine): runtime=%.1fm timeout=%dm",
                ),
                machine.get_runtime_minutes(),
                machine.timeout_minutes,
            )
            return SessionStatus.TIMED_OUT

        if session.is_timed_out:
            logger.info(
                issue_log(
                    session.issue.number,
                    "Session timed out: runtime=%sm timeout=%sm",
                ),
                session.runtime_minutes,
                session.agent_config.timeout_minutes,
            )
            return SessionStatus.TIMED_OUT

        return None

    def _handle_running_session(self, session: Session) -> SessionStatus:
        """Handle a running session, possibly sending /exit if PR exists.

        Returns:
            SessionStatus.RUNNING
        """
        self._try_send_exit_if_has_pr(session)
        logger.debug(
            issue_log(session.issue.number, "Still running: session=%s"),
            session.terminal_id,
        )
        return SessionStatus.RUNNING

    def _determine_exited_session_outcome(self, session: Session) -> SessionStatus:
        """Determine the outcome for a session that has exited.

        Returns:
            SessionStatus (COMPLETED, BLOCKED, NEEDS_HUMAN, or FAILED)
        """
        logger.debug(
            issue_log(session.issue.number, "Session exited, checking completion status"),
        )

        # Check if PR exists for the branch
        try:
            prs = self._get_open_prs_for_branch(session.branch_name)
            if prs:
                logger.info(
                    issue_log(
                        session.issue.number,
                        "Found %d open PR(s) for branch %s - COMPLETED",
                    ),
                    len(prs),
                    session.branch_name,
                )
                return SessionStatus.COMPLETED
        except Exception as e:
            logger.warning(
                issue_log(
                    session.issue.number,
                    "Failed to check for open PRs on branch %s: %s",
                ),
                session.branch_name,
                e,
            )

        return self._determine_outcome_from_labels(session)

    def _determine_outcome_from_labels(self, session: Session) -> SessionStatus:
        """Determine outcome by checking issue labels.

        Returns:
            SessionStatus (BLOCKED, NEEDS_HUMAN, or FAILED)
        """
        try:
            current_labels = self._get_issue_labels(session.issue.number)
            logger.debug(
                issue_log(session.issue.number, "Fresh labels: %s"), current_labels
            )
        except Exception as e:
            # The reader RAISES on an incomplete read rather than returning an
            # empty list (#6957 round-2 review F4/A4), so this fallback is what
            # keeps a timeout from classifying a blocked/needs-human session as
            # plain FAILED: last-known labels beat a fabricated empty set.
            logger.warning(
                issue_log(session.issue.number, "Failed to fetch labels: %s"), e
            )
            current_labels = session.issue.labels

        if self._lm.blocked in current_labels:
            logger.info(
                issue_log(session.issue.number, "Has '%s' label - BLOCKED"),
                self._lm.blocked,
            )
            return SessionStatus.BLOCKED

        if self._lm.needs_human in current_labels:
            logger.info(
                issue_log(session.issue.number, "Has '%s' label - NEEDS_HUMAN"),
                self._lm.needs_human,
            )
            return SessionStatus.NEEDS_HUMAN

        logger.info(
            issue_log(session.issue.number, "Ended without completion markers - FAILED"),
        )
        return SessionStatus.FAILED

    def check_session(self, session: Session) -> SessionStatus:
        """Check the status of a session.

        Logic:
        1. If runtime > timeout -> TIMED_OUT (uses state machine if available)
        2. If session still running:
           a. Check if PR exists -> send /exit, return RUNNING (will complete next check)
           b. Otherwise -> RUNNING
        3. If session exited:
           a. Check if PR exists for branch -> COMPLETED
           b. Check if issue has 'blocked' label -> BLOCKED
           c. Check if issue has 'needs-human' label -> NEEDS_HUMAN
           d. Otherwise -> FAILED

        Args:
            session: The session to check

        Returns:
            SessionStatus indicating the current state of the session
        """
        timeout_status = self._check_timeout_status(session)
        if timeout_status:
            return timeout_status

        if self._session_exists_by_name(session.terminal_id):
            return self._handle_running_session(session)

        return self._determine_exited_session_outcome(session)

    def check_all_sessions(self, sessions: list[Session]) -> dict[int, SessionStatus]:
        """Check all sessions and return their statuses.

        Args:
            sessions: List of sessions to check

        Returns:
            Dictionary mapping issue_number to SessionStatus
        """
        statuses: dict[int, SessionStatus] = {}

        for session in sessions:
            try:
                status = self.check_session(session)
                statuses[session.issue.number] = status
            except Exception as e:
                logger.error(
                    issue_log(session.issue.number, "Error checking session: %s"),
                    e,
                )
                statuses[session.issue.number] = SessionStatus.FAILED

        return statuses

    def handle_completion(self, session: Session, status: SessionStatus) -> None:
        """Handle session completion observation.

        Note: Label operations (add blocked-failed, remove in-progress) and
        session cleanup (killing terminals, removing worktrees) are now handled
        via the action system through CleanupSessionAction.

        The observer's role is to OBSERVE, not to take actions. All side effects
        are handled via the Planner → ActionApplier flow.

        Args:
            session: The completed session
            status: The final status of the session
        """
        issue_number = session.issue.number

        # Observer only logs - cleanup is handled via CleanupSessionAction
        # generated by the Planner from immediate_cleanups facts
        logger.info(
            issue_log(issue_number, "Observer noted completion: status=%s terminal=%s"),
            status.value,
            session.terminal_id,
        )

    def detect_stale_in_progress(
        self,
        issues: list["Issue"],
        active_sessions: list[Session],
    ) -> list["Issue"]:
        """Find issues with in-progress label but no running session.

        This is a fact-gathering operation - it detects stale state where
        an issue has the in-progress label but there's no active session
        working on it.

        Args:
            issues: All issues to check
            active_sessions: Currently active sessions

        Returns:
            List of issues that have stale in-progress labels
        """
        active_issue_numbers = {s.issue.number for s in active_sessions}
        stale_issues = []

        for issue in issues:
            if self._lm.is_in_progress(issue.labels):
                if issue.number not in active_issue_numbers:
                    logger.debug(
                        issue_log(issue.number, "Stale in-progress: label present but no active session"),
                    )
                    stale_issues.append(issue)

        return stale_issues


# Backwards compatibility alias (deprecated)
# TODO: Remove after all imports are updated
SessionMonitor = SessionObserver
