"""Terminal plugin that runs agent sessions as subprocesses.

This provides a tmux-free execution option while still emitting a session log
per worktree for debugging and session health checks.

Delegates all process spawning to ``AgentRunner.start()`` which handles PTY
creation, ``CleaningLogWriter`` setup, and process group isolation. This plugin
only manages session lifecycle (registry, existence checks, cleanup).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NoReturn

from ..control.isolation import build_agent_tool_path, build_isolation_prefix
from ..domain.process_group import (
    ProcessIdentityAbsent,
    ProcessIdentityPermissionDenied,
    ProcessIdentityPresent,
)
from ..domain.retained_thread import (
    RetainedThreadActivated,
    RetainedThreadActivationIndeterminate,
    RetainedThreadActivationInterrupted,
    RetainedThreadActivationRejected,
)
from ..domain.terminal_session_registry import (
    PendingTerminalSessionRecord,
    TerminalSessionRecord,
)
from ..domain.terminal_launch import (
    TerminalInteractionIntent,
    TerminalLaunch,
)
from ..domain.executor import ExecutorInteractiveSessionCancellation
from ..domain.terminal_session_termination import (
    TerminalSessionOwnerCancellation,
    TerminalSessionProcess,
    UnregisteredTerminalSessionOwnership,
)
from ..domain.terminal_session_termination import TerminalSessionStatus
from ..domain.terminal_session_lifecycle import (
    TerminalSessionWatcherCompleted,
    TerminalSessionWatcherFailed,
    TerminalSessionWatcherPolicy,
    TerminalSessionWatcherTimedOut,
)
from ..ports.process_group_supervisor import ProcessGroupSupervisor
from ..ports.terminal_session_terminator import TerminalSessionTerminator
from ..ports.terminal_session_owner import (
    TerminalSessionLaunchLease,
    TerminalSessionOwner,
)
from ..ports.terminal_session_registry import TerminalSessionRegistry
from .agent_runner import AgentRunner, AgentSession, AgentSpec
from .session_interactions import (
    SessionInteractionHandler,
    builtin_session_interaction_rules,
)
from ..infra.env import get_env
from ..infra.hooks.hookspec import hookimpl
from .terminal_session_lifecycle import (
    PendingTerminalSession,
    TerminalSessionWatcher,
    TerminalSessionWatcherFactory,
    TerminalSessionWatcherShutdownError,
)
from .terminal_session_registry import TerminalSessionRegistryError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _StartedTerminalSession:
    session: AgentSession
    launch_lease: TerminalSessionLaunchLease

    def __post_init__(self) -> None:
        if type(self.session) is not AgentSession:
            raise ValueError("started terminal session must contain AgentSession")
        if not isinstance(self.launch_lease, TerminalSessionLaunchLease):
            raise ValueError(
                "started terminal session must contain a typed launch lease"
            )


@dataclass(frozen=True, slots=True)
class _CommittedTerminalSession:
    """Durably identified session whose watcher has not activated yet."""

    record: TerminalSessionRecord
    pending_session: PendingTerminalSession

    def __post_init__(self) -> None:
        if type(self.record) is not TerminalSessionRecord:
            raise ValueError(
                "committed terminal session must contain TerminalSessionRecord"
            )
        if type(self.pending_session) is not PendingTerminalSession:
            raise ValueError(
                "committed terminal session must contain PendingTerminalSession"
            )


class SubprocessPlugin:
    """Terminal plugin that uses subprocesses instead of tmux.

    Delegates process spawning to :class:`AgentRunner` which handles PTY
    creation, ``CleaningLogWriter`` setup, and process group isolation.
    This plugin manages session lifecycle: registry, existence checks, cleanup.
    """

    def __init__(
        self,
        session_terminator: TerminalSessionTerminator,
        terminal_session_owner: TerminalSessionOwner,
        registry: TerminalSessionRegistry,
        process_group_supervisor: ProcessGroupSupervisor,
        watcher_policy: TerminalSessionWatcherPolicy,
        watcher_factory: TerminalSessionWatcherFactory,
        *,
        session_interactions_enabled: bool = False,
        worktree_base: Path | None = None,
    ) -> None:
        if not isinstance(session_terminator, TerminalSessionTerminator):
            raise ValueError(
                "SubprocessPlugin.session_terminator must be a "
                "TerminalSessionTerminator"
            )
        if not isinstance(process_group_supervisor, ProcessGroupSupervisor):
            raise ValueError(
                "SubprocessPlugin.process_group_supervisor must implement "
                "ProcessGroupSupervisor"
            )
        if not isinstance(terminal_session_owner, TerminalSessionOwner):
            raise ValueError(
                "SubprocessPlugin.terminal_session_owner must implement "
                "TerminalSessionOwner"
            )
        if not isinstance(registry, TerminalSessionRegistry):
            raise ValueError(
                "SubprocessPlugin.registry must implement TerminalSessionRegistry"
            )
        if type(watcher_policy) is not TerminalSessionWatcherPolicy:
            raise ValueError(
                "SubprocessPlugin.watcher_policy must be TerminalSessionWatcherPolicy"
            )
        if not isinstance(watcher_factory, TerminalSessionWatcherFactory):
            raise ValueError(
                "SubprocessPlugin.watcher_factory must implement "
                "TerminalSessionWatcherFactory"
            )
        self._session_terminator = session_terminator
        self._terminal_session_owner = terminal_session_owner
        self._registry = registry
        self._process_group_supervisor = process_group_supervisor
        self._watcher_policy = watcher_policy
        self._watcher_factory = watcher_factory
        self._recover_pending_launches()
        self._sessions: dict[str, AgentSession] = {}
        self._session_watchers: dict[str, TerminalSessionWatcher] = {}
        deny_stdin_val = get_env("SUBPROCESS_DENY_STDIN") or ""
        self._allow_stdin = deny_stdin_val.lower() not in {"1", "true", "yes"}
        self._session_interactions_enabled = session_interactions_enabled
        self._worktree_base = (
            worktree_base.resolve() if worktree_base is not None else None
        )
        self._warned_missing_worktree_base = False

    def _recover_pending_launches(self) -> None:
        committed_names = frozenset(self._registry.load())
        for pending in self._registry.load_pending():
            if pending.session_name in committed_names:
                raise TerminalSessionRegistryError(
                    "session cannot be both pending and committed: "
                    f"{pending.session_name!r}"
                )
            report = self._session_terminator.recover_unregistered(
                UnregisteredTerminalSessionOwnership.for_run_dir(pending.run_dir)
            )
            self._registry.remove_pending(pending.session_name)
            logger.warning(
                "[subprocess] recovered pending launch: session_name=%s "
                "terminal_owner=%s guardian_owner=%s",
                pending.session_name,
                report.terminal_owner.value,
                report.guardian_owner.value,
            )

    def _build_process_command(self, command: str, working_dir: Path) -> str:
        """Build the full command with path and isolation prefix."""
        path_prefix = build_agent_tool_path(working_dir, os.environ.get("PATH", ""))
        isolation_prefix = build_isolation_prefix(
            working_dir, scrub_env=True, isolate_home=False
        )
        return f'cd "{working_dir}" && export PATH="{path_prefix}" && {isolation_prefix}{command}'

    def _interaction_handler(
        self,
        intent: TerminalInteractionIntent,
        session_name: str,
        working_dir: Path,
    ) -> SessionInteractionHandler | None:
        if not self._session_interactions_enabled or not self._allow_stdin:
            return None
        if self._worktree_base is None:
            if not self._warned_missing_worktree_base:
                logger.warning(
                    "[session-interactions] disabled because worktree_base is not configured"
                )
                self._warned_missing_worktree_base = True
            return None
        if not working_dir.resolve().is_relative_to(self._worktree_base):
            return None
        rules = builtin_session_interaction_rules(intent)
        if not rules:
            return None
        return SessionInteractionHandler(session_name=session_name, rules=rules)

    def _start_process(
        self,
        launch: TerminalLaunch,
        working_dir: Path,
        session_name: str,
    ) -> _StartedTerminalSession:
        """Start an agent session via :class:`AgentRunner`.

        Builds the full command with isolation prefix, constructs an
        :class:`AgentSpec`, and delegates to ``AgentRunner.start()``.
        """
        full_cmd = self._build_process_command(launch.shell_command, working_dir)
        run_dir = launch.destination.run_dir
        log_path = launch.destination.recording_path
        if not run_dir.is_relative_to(working_dir.resolve()):
            raise ValueError(
                "terminal run destination must belong to the session worktree"
            )
        if not run_dir.is_dir():
            raise FileNotFoundError(
                f"terminal run destination does not exist: {run_dir}"
            )
        interaction_handler = self._interaction_handler(
            launch.interaction_intent,
            session_name,
            working_dir,
        )

        owner_cancellation = TerminalSessionOwnerCancellation.for_run_dir(run_dir)
        launch_lease = self._terminal_session_owner.prepare(
            (launch.shell.value, "-lc", full_cmd),
            owner_cancellation,
        )
        spec = AgentSpec(
            command=list(launch_lease.command),
            working_dir=working_dir,
            timeout_seconds=7200,  # Sessions manage their own timeout via provider_runner
            log_path=log_path,
            output_dir=log_path.parent,
        )
        runner = AgentRunner()
        try:
            session = runner.start_direct_with_file_descriptors(
                spec,
                launch_lease.inherited_file_descriptors,
                interaction_handler=interaction_handler,
            )
        except BaseException as primary_error:
            try:
                launch_lease.abandon_after_spawn_uncertainty()
            except BaseException as cleanup_error:
                raise BaseExceptionGroup(
                    "terminal spawn and uncertain-owner release failed",
                    (primary_error, cleanup_error),
                )
            raise
        logger.info(
            "[subprocess] session started: session_name=%s pid=%s log_path=%s run_dir=%s",
            session_name,
            session.pid,
            log_path,
            log_path.parent,
        )

        return _StartedTerminalSession(session, launch_lease)

    def _identify_started_process(
        self,
        process_id: int,
        run_dir: Path,
    ) -> TerminalSessionProcess:
        identity = self._session_terminator.identify(process_id)
        if type(identity) is ProcessIdentityAbsent:
            raise TerminalSessionRegistryError(
                "new terminal process disappeared before its birth identity "
                f"could be persisted: pid={process_id}"
            )
        if type(identity) is ProcessIdentityPermissionDenied:
            raise TerminalSessionRegistryError(
                "permission denied while identifying a new terminal process: "
                f"pid={process_id} detail={identity.detail}"
            )
        if type(identity) is not ProcessIdentityPresent:
            raise AssertionError("process identity observation is a closed union")
        if identity.process_group_id != process_id:
            raise TerminalSessionRegistryError(
                "new terminal process is not its process-group leader: "
                f"pid={process_id} pgid={identity.process_group_id}"
            )
        return TerminalSessionProcess(
            process_id=process_id,
            birth_identity=identity.birth_identity,
            terminal_cancellation=(
                TerminalSessionOwnerCancellation.for_run_dir(run_dir)
            ),
            executor_cancellation=(
                ExecutorInteractiveSessionCancellation.for_run_dir(run_dir)
            ),
        )

    def _record_is_active(self, record: TerminalSessionRecord) -> bool:
        """Resolve liveness without ever treating a recycled PID as the session."""
        session = self._sessions.get(record.session_name)
        if session is not None and session.is_alive():
            return True
        status = self._session_terminator.status(record.process)
        if status is TerminalSessionStatus.ACTIVE:
            return True
        self._session_terminator.terminate(record.process)
        return False

    def _kill_process(self, record: TerminalSessionRecord) -> None:
        """Contain a live or recovered session before reporting it stopped."""
        self._session_terminator.terminate(record.process)

    @hookimpl
    def create_session(
        self,
        session_id: int,
        launch: TerminalLaunch,
        working_dir: str,
        title: str | None,
        session_name: str,  # Required - caller must provide explicit name
    ) -> bool | None:
        logger.info(
            "[subprocess] create_session called: session_id=%s session_name=%r",
            session_id,
            session_name,
        )
        if type(launch) is not TerminalLaunch:
            raise ValueError("SubprocessPlugin.create_session requires TerminalLaunch")
        worktree = Path(working_dir)
        if self.session_exists(session_id, session_name):
            return False
        pending_record = self._build_pending_record(
            session_id,
            launch,
            worktree,
            title,
            session_name,
        )
        pending_session = self._start_pending_session(
            pending_record,
            launch,
            worktree,
        )
        committed = self._commit_pending_session(pending_record, pending_session)
        self._activate_committed_watcher(committed)
        return True

    @staticmethod
    def _build_pending_record(
        session_id: int,
        launch: TerminalLaunch,
        worktree: Path,
        title: str | None,
        session_name: str,
    ) -> PendingTerminalSessionRecord:
        """Construct exact durable launch intent before any process can start."""
        is_review = session_name.startswith("review-")
        tab_name = title or session_name
        if is_review:
            try:
                pr_num = int(session_name.replace("review-", ""))
                tab_name = f"Review PR #{pr_num}"
            except ValueError:
                tab_name = session_name
        return PendingTerminalSessionRecord(
            session_name=session_name,
            issue_number=session_id,
            worktree_path=worktree.resolve(),
            run_dir=launch.destination.run_dir,
            registered_at=datetime.now().astimezone(),
            recording_path=launch.destination.recording_path,
            tab_name=tab_name,
            is_review=is_review,
        )

    def _start_pending_session(
        self,
        pending_record: PendingTerminalSessionRecord,
        launch: TerminalLaunch,
        worktree: Path,
    ) -> PendingTerminalSession:
        """Own launch intent through spawn and child-side owner readiness."""
        self._registry.begin_launch(pending_record)
        try:
            started_session = self._start_process(
                launch,
                worktree,
                pending_record.session_name,
            )
        except BaseException as error:
            self._fail_pending_launch(pending_record, error)
            raise AssertionError("unreachable after pending-launch recovery")
        pending_session = PendingTerminalSession(
            started_session.session,
            self._process_group_supervisor,
            started_session.launch_lease,
        )
        try:
            pending_session.require_owner_ready()
        except BaseException as error:
            self._abort_identified_pending_launch(
                pending_record,
                pending_session,
                error,
            )
            raise AssertionError("unreachable after owner-readiness cleanup")
        return pending_session

    def _commit_pending_session(
        self,
        pending_record: PendingTerminalSessionRecord,
        pending_session: PendingTerminalSession,
    ) -> _CommittedTerminalSession:
        """Identify and atomically promote a launch to durable session state."""
        session = pending_session.session
        try:
            process = self._identify_started_process(
                session.pid,
                pending_record.run_dir,
            )
        except BaseException as error:
            self._abort_identified_pending_launch(
                pending_record,
                pending_session,
                error,
            )
            raise AssertionError("unreachable after unregistered-session cleanup")
        record = TerminalSessionRecord(
            session_name=pending_record.session_name,
            issue_number=pending_record.issue_number,
            worktree_path=pending_record.worktree_path,
            process=process,
            registered_at=pending_record.registered_at,
            recording_path=pending_record.recording_path,
            tab_name=pending_record.tab_name,
            is_review=pending_record.is_review,
        )
        try:
            self._registry.commit_launch(pending_record, record)
        except BaseException as error:
            self._abort_identified_pending_launch(
                pending_record,
                pending_session,
                error,
            )
            raise AssertionError("unreachable after identified-session cleanup")
        return _CommittedTerminalSession(record, pending_session)

    def _activate_committed_watcher(
        self,
        committed: _CommittedTerminalSession,
    ) -> None:
        """Retain the watcher before crossing its ambiguous activation boundary."""
        record = committed.record
        pending_session = committed.pending_session
        session = pending_session.session
        try:
            watcher = self._watcher_factory.create(record.session_name, session)
        except BaseException as watcher_error:
            try:
                pending_session.abort(watcher_error)
            except BaseException as abort_outcome:
                if abort_outcome is not watcher_error:
                    # The committed row remains authoritative whenever exact
                    # group containment or PTY finalization is unproven.
                    raise
            try:
                self._registry.remove(record.session_name)
            except BaseException as rollback_error:
                raise BaseExceptionGroup(
                    "contained terminal watcher startup and registry rollback failed",
                    (watcher_error, rollback_error),
                )
            raise watcher_error
        self._sessions[record.session_name] = session
        self._session_watchers[record.session_name] = watcher
        activation = watcher.activate()
        if type(activation) is RetainedThreadActivationRejected:
            self._rollback_rejected_watcher(
                record,
                pending_session,
                activation.error,
            )
            raise AssertionError("unreachable after rejected watcher rollback")
        if type(activation) is RetainedThreadActivationInterrupted:
            self._recover_interrupted_watcher(record, watcher, activation.error)
            raise AssertionError("unreachable after interrupted watcher recovery")
        if type(activation) is RetainedThreadActivationIndeterminate:
            self._recover_interrupted_watcher(record, watcher, activation.error)
            raise AssertionError("unreachable after indeterminate watcher recovery")
        if type(activation) is not RetainedThreadActivated:
            raise AssertionError("watcher activation is a closed union")

    def _rollback_rejected_watcher(
        self,
        record: TerminalSessionRecord,
        pending_session: PendingTerminalSession,
        activation_error: BaseException,
    ) -> NoReturn:
        """Rollback a committed row only when the watcher never executed."""
        failures: list[BaseException] = [activation_error]
        try:
            pending_session.abort(activation_error)
        except BaseException as abort_outcome:
            if abort_outcome is not activation_error:
                failures.append(abort_outcome)
        if len(failures) == 1:
            self._sessions.pop(record.session_name)
            self._session_watchers.pop(record.session_name)
            try:
                self._registry.remove(record.session_name)
            except BaseException as rollback_error:
                failures.append(rollback_error)
        if len(failures) == 1:
            raise activation_error
        raise BaseExceptionGroup(
            "terminal watcher activation was rejected and rollback failed",
            failures,
        )

    def _recover_interrupted_watcher(
        self,
        record: TerminalSessionRecord,
        watcher: TerminalSessionWatcher,
        activation_error: BaseException,
    ) -> NoReturn:
        """Contain an ambiguously activated watcher through its retained owner."""
        failures: list[BaseException] = [activation_error]
        try:
            self._kill_process(record)
        except BaseException as containment_error:
            containment_error.add_note(
                "terminal watcher activation recovery could not contain session"
            )
            failures.append(containment_error)
        outcome = watcher.await_completion(self._watcher_policy)
        if type(outcome) is TerminalSessionWatcherTimedOut:
            failures.append(
                TerminalSessionWatcherShutdownError(
                    "interrupted terminal watcher remained live after containment: "
                    f"session_name={outcome.session_name!r} "
                    f"pid={outcome.process_id} timeout={outcome.timeout_seconds:.3f}s"
                )
            )
            raise BaseExceptionGroup(
                "terminal watcher activation recovery was incomplete",
                failures,
            )
        if type(outcome) is TerminalSessionWatcherFailed:
            failures.append(outcome.error)
        elif type(outcome) is not TerminalSessionWatcherCompleted:
            raise AssertionError("terminal watcher outcome is a closed union")
        self._sessions.pop(record.session_name)
        self._session_watchers.pop(record.session_name)
        try:
            self._registry.remove(record.session_name)
        except BaseException as registry_error:
            failures.append(registry_error)
        if len(failures) == 1:
            raise activation_error
        raise BaseExceptionGroup(
            "terminal watcher activation was interrupted",
            failures,
        )

    def _fail_pending_launch(
        self,
        pending: PendingTerminalSessionRecord,
        primary_error: BaseException,
    ) -> NoReturn:
        try:
            self._session_terminator.recover_unregistered(
                UnregisteredTerminalSessionOwnership.for_run_dir(pending.run_dir)
            )
        except BaseException as recovery_error:
            raise BaseExceptionGroup(
                "terminal launch failed and durable owner recovery failed",
                (primary_error, recovery_error),
            )
        try:
            self._registry.remove_pending(pending.session_name)
        except BaseException as cleanup_error:
            raise BaseExceptionGroup(
                "terminal launch failed after recovery but intent cleanup failed",
                (primary_error, cleanup_error),
            )
        raise primary_error

    def _abort_identified_pending_launch(
        self,
        pending: PendingTerminalSessionRecord,
        pending_session: PendingTerminalSession,
        primary_error: BaseException,
    ) -> NoReturn:
        try:
            pending_session.abort(primary_error)
        except BaseException as abort_outcome:
            if abort_outcome is not primary_error:
                raise
        try:
            self._registry.remove_pending(pending.session_name)
        except BaseException as cleanup_error:
            raise BaseExceptionGroup(
                "contained terminal launch retained a pending registry row",
                (primary_error, cleanup_error),
            )
        raise primary_error

    def _cleanup_session(self, session_name: str) -> None:
        """Retire local resources only after the watcher proves completion."""
        owns_session = session_name in self._sessions
        owns_watcher = session_name in self._session_watchers
        if owns_session != owns_watcher:
            raise RuntimeError(
                "terminal session and watcher ownership diverged: "
                f"session_name={session_name!r} owns_session={owns_session} "
                f"owns_watcher={owns_watcher}"
            )
        if not owns_session:
            # Recovered sessions have only durable process ownership; their
            # prior process's in-memory PTY watcher cannot survive a restart.
            return
        watcher = self._session_watchers[session_name]
        outcome = watcher.await_completion(self._watcher_policy)
        if type(outcome) is TerminalSessionWatcherTimedOut:
            raise TerminalSessionWatcherShutdownError(
                "terminal session watcher remained live after containment: "
                f"session_name={outcome.session_name!r} pid={outcome.process_id} "
                f"timeout={outcome.timeout_seconds:.3f}s"
            )
        if type(outcome) is TerminalSessionWatcherFailed:
            raise TerminalSessionWatcherShutdownError(
                "terminal session watcher failed during PTY finalization: "
                f"session_name={outcome.session_name!r} pid={outcome.process_id}"
            ) from outcome.error
        if type(outcome) is not TerminalSessionWatcherCompleted:
            raise AssertionError("terminal watcher outcome is a closed union")
        self._session_watchers.pop(session_name)
        self._sessions.pop(session_name)

    @hookimpl
    def session_exists(self, session_id: int, session_name: str) -> bool | None:
        records = self._registry.load()
        record = records.get(session_name)
        if not record:
            return False
        if self._record_is_active(record):
            return True
        # Process is dead - wait for watcher thread to finish flushing output
        logger.info(
            "[subprocess] session no longer alive: session_name=%s pid=%s log_path=%s",
            session_name,
            record.pid,
            record.recording_path,
        )
        self._cleanup_session(session_name)
        self._registry.remove(session_name)
        return False

    @hookimpl
    def session_exists_by_name(self, session_name: str) -> bool | None:
        return self.session_exists(0, session_name)

    @hookimpl
    def kill_session(self, session_id: int, session_name: str) -> bool | None:
        records = self._registry.load()
        record = records.get(session_name)
        if not record:
            return False
        self._kill_process(record)
        self._cleanup_session(session_name)
        self._registry.remove(session_name)
        return True

    @hookimpl
    def discover_running_sessions(self) -> list[dict] | None:
        records = self._registry.load()
        running: list[dict] = []
        for record in records.values():
            if self._record_is_active(record):
                running.append(
                    {
                        "issue_number": record.issue_number,
                        "tab_name": record.tab_name,
                        "is_review": record.is_review,
                        "session_name": record.session_name,
                        "run_dir": str(record.run_dir),
                    }
                )
            else:
                self._cleanup_session(record.session_name)
                self._registry.remove(record.session_name)
        return running

    @hookimpl
    def cleanup_idle_sessions(self) -> int | None:
        records = self._registry.load()
        cleaned = 0
        for record in list(records.values()):
            if not self._record_is_active(record):
                self._cleanup_session(record.session_name)
                self._registry.remove(record.session_name)
                cleaned += 1
        return cleaned

    @hookimpl
    def get_session_output(
        self, session_id: int, lines: int, session_name: str
    ) -> str | None:
        record = self._registry.load().get(session_name)
        if not record:
            return None
        log_path = record.recording_path
        if not log_path.exists():
            return ""
        try:
            content = log_path.read_text()
        except Exception:
            return ""
        output_lines = content.splitlines()
        return "\n".join(output_lines[-lines:]) if output_lines else ""

    @hookimpl
    def send_to_session(
        self, session_id: int, text: str, session_name: str
    ) -> bool | None:
        if not self._allow_stdin:
            return False
        session = self._sessions.get(session_name)
        if not session:
            return False
        return session.send(text)

    @hookimpl
    def send_to_session_by_name(self, session_name: str, text: str) -> bool | None:
        return self.send_to_session(0, text, session_name)

    @hookimpl
    def focus_session(self, session_id: int, session_name: str) -> bool | None:
        return False

    @hookimpl
    def on_orchestrator_startup(self) -> None:
        logger.info("[subprocess] Terminal backend ready (AgentRunner).")

    @hookimpl
    def on_orchestrator_shutdown(self) -> None:
        records = self._registry.load()
        failures: list[BaseException] = []
        for record in records.values():
            try:
                self._kill_process(record)
                self._cleanup_session(record.session_name)
                self._registry.remove(record.session_name)
            except BaseException as error:
                error.add_note(f"terminal shutdown session: {record.session_name}")
                failures.append(error)
        if failures:
            raise BaseExceptionGroup(
                "one or more terminal sessions could not be contained",
                failures,
            )

    @hookimpl
    def terminal_health_check(self) -> dict | None:
        return {
            "healthy": True,
            "server_running": True,
            "session_exists": bool(self._registry.load()),
            "error": None,
        }
