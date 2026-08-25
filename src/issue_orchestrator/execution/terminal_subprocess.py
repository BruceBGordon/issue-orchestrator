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
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..control.isolation import build_agent_tool_path, build_isolation_prefix
from ..domain.process_group import (
    ProcessBirthIdentity,
    ProcessIdentityAbsent,
    ProcessIdentityPermissionDenied,
    ProcessIdentityPresent,
)
from ..domain.terminal_launch import (
    TerminalInteractionIntent,
    TerminalLaunch,
)
from ..domain.executor import ExecutorInteractiveSessionCancellation
from ..domain.terminal_session_termination import TerminalSessionProcess
from ..domain.terminal_session_termination import TerminalSessionStatus
from ..domain.terminal_session_lifecycle import (
    TerminalSessionWatcherCompleted,
    TerminalSessionWatcherFailed,
    TerminalSessionWatcherPolicy,
    TerminalSessionWatcherTimedOut,
)
from ..ports.process_group_supervisor import ProcessGroupSupervisor
from ..ports.terminal_session_terminator import TerminalSessionTerminator
from .agent_runner import AgentRunner, AgentSession, AgentSpec
from .session_interactions import (
    SessionInteractionHandler,
    builtin_session_interaction_rules,
)
from ..infra.env import get_env
from ..infra.hooks.hookspec import hookimpl
from ..infra.repo_identity import state_dir
from ..infra.sqlite_connection import open_sqlite
from .terminal_session_lifecycle import (
    PendingTerminalSession,
    TerminalSessionWatcher,
    TerminalSessionWatcherShutdownError,
)

logger = logging.getLogger(__name__)


class SubprocessRegistryError(RuntimeError):
    """Persisted terminal ownership cannot be interpreted safely."""


@dataclass(frozen=True, slots=True)
class _SessionRecord:
    """Complete durable identity for one subprocess-backed terminal session."""

    session_name: str
    issue_number: int
    worktree_path: Path
    process: TerminalSessionProcess
    registered_at: datetime
    recording_path: Path
    tab_name: str
    is_review: bool

    def __post_init__(self) -> None:
        if type(self.session_name) is not str or not self.session_name:
            raise ValueError("_SessionRecord.session_name must not be empty")
        if type(self.issue_number) is not int or self.issue_number < 0:
            raise ValueError("_SessionRecord.issue_number must be non-negative")
        self._require_storage_paths()
        if type(self.process) is not TerminalSessionProcess:
            raise ValueError("_SessionRecord.process must be TerminalSessionProcess")
        if self.registered_at.tzinfo is None:
            raise ValueError("_SessionRecord.registered_at must be timezone-aware")
        if type(self.tab_name) is not str or not self.tab_name:
            raise ValueError("_SessionRecord.tab_name must not be empty")
        if type(self.is_review) is not bool:
            raise ValueError("_SessionRecord.is_review must be bool")

    def _require_storage_paths(self) -> None:
        if not self.worktree_path.is_absolute():
            raise ValueError("_SessionRecord.worktree_path must be absolute")
        if not self.recording_path.is_absolute():
            raise ValueError("_SessionRecord.recording_path must be absolute")
        if not self.run_dir.is_relative_to(self.worktree_path):
            raise ValueError("_SessionRecord.run_dir must belong to worktree_path")
        if not self.recording_path.is_relative_to(self.run_dir):
            raise ValueError("_SessionRecord.recording_path must belong to run_dir")

    @property
    def pid(self) -> int:
        return self.process.process_id

    @property
    def run_dir(self) -> Path:
        return self.process.executor_cancellation.record_path.parent


class _SubprocessRegistry:
    """Persist subprocess sessions for restart discovery."""

    def __init__(self, repo_root: Path) -> None:
        self._state_dir = state_dir(repo_root)
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._state_dir / "session_registry.sqlite"
        self._legacy_db_path = self._state_dir / "subprocess_sessions.sqlite"
        self._legacy_dir = self._state_dir / "subprocess_sessions"
        self._legacy_index = self._state_dir / "subprocess_sessions.json"
        self._legacy_backup = self._legacy_index.with_suffix(".json.bak")
        self._migrate_legacy_db_if_needed()
        self._ensure_db()
        self._migrate_legacy_if_needed()
        self.load()

    def _connect(self) -> sqlite3.Connection:
        return open_sqlite(self._db_path)

    def _ensure_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_name TEXT PRIMARY KEY,
                    issue_number INTEGER NOT NULL,
                    worktree_path TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    log_path TEXT NOT NULL,
                    tab_name TEXT NOT NULL,
                    is_review INTEGER NOT NULL,
                    run_dir TEXT NOT NULL,
                    process_birth_identity TEXT NOT NULL
                )
                """
            )
            existing_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if "run_dir" not in existing_columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN run_dir TEXT")
            if "process_birth_identity" not in existing_columns:
                conn.execute(
                    "ALTER TABLE sessions ADD COLUMN "
                    "process_birth_identity TEXT"
                )
            conn.commit()
        try:
            inode = os.stat(self._db_path).st_ino
        except FileNotFoundError:
            inode = None
        logger.info(
            "Session registry initialized: db=%s inode=%s pid=%d",
            self._db_path,
            inode,
            os.getpid(),
        )

    def _migrate_legacy_db_if_needed(self) -> None:
        if not self._legacy_db_path.exists() or self._db_path.exists():
            return
        try:
            self._legacy_db_path.replace(self._db_path)
        except OSError as exc:
            raise SubprocessRegistryError(
                "could not move the legacy subprocess registry into place"
            ) from exc

    def _migrate_legacy_if_needed(self) -> None:
        if self._db_path.exists() and self._has_rows():
            return
        legacy_sources = tuple(
            path
            for path in (
                self._legacy_index,
                self._legacy_backup,
                self._legacy_dir,
            )
            if path.exists()
        )
        if not legacy_sources:
            return
        raise SubprocessRegistryError(
            "legacy subprocess registry cannot be migrated safely because it "
            "does not contain process birth identities: "
            + ", ".join(str(path) for path in legacy_sources)
        )

    def _has_rows(self) -> bool:
        try:
            with self._connect() as conn:
                cur = conn.execute("SELECT 1 FROM sessions LIMIT 1")
                return cur.fetchone() is not None
        except sqlite3.DatabaseError as exc:
            raise SubprocessRegistryError(
                f"could not inspect subprocess registry {self._db_path}"
            ) from exc

    def load(self) -> dict[str, _SessionRecord]:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT session_name, issue_number, worktree_path, pid, "
                    "started_at, log_path, tab_name, is_review, run_dir, "
                    "process_birth_identity FROM sessions"
                )
                records = {}
                for row in cur.fetchall():
                    if row[8] is None or type(row[9]) is not str:
                        raise SubprocessRegistryError(
                            "session registry row lacks a process birth identity; "
                            f"refusing unsafe PID recovery: session={row[0]!r}"
                        )
                    run_dir = Path(row[8])
                    records[row[0]] = _SessionRecord(
                        session_name=row[0],
                        issue_number=row[1],
                        worktree_path=Path(row[2]),
                        process=TerminalSessionProcess(
                            process_id=row[3],
                            birth_identity=ProcessBirthIdentity(row[9]),
                            executor_cancellation=(
                                ExecutorInteractiveSessionCancellation.for_run_dir(
                                    run_dir
                                )
                            ),
                        ),
                        registered_at=datetime.fromisoformat(row[4]),
                        recording_path=Path(row[5]),
                        tab_name=row[6],
                        is_review=bool(row[7]),
                    )
                return records
        except sqlite3.DatabaseError as exc:
            raise SubprocessRegistryError(
                f"could not read subprocess registry {self._db_path}"
            ) from exc

    def upsert(self, record: _SessionRecord) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO sessions (
                        session_name, issue_number, worktree_path, pid, started_at,
                        log_path, tab_name, is_review, run_dir,
                        process_birth_identity
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_name) DO UPDATE SET
                        issue_number=excluded.issue_number,
                        worktree_path=excluded.worktree_path,
                        pid=excluded.pid,
                        started_at=excluded.started_at,
                        log_path=excluded.log_path,
                        tab_name=excluded.tab_name,
                        is_review=excluded.is_review,
                        run_dir=excluded.run_dir,
                        process_birth_identity=(
                            excluded.process_birth_identity
                        )
                    """,
                    (
                        record.session_name,
                        record.issue_number,
                        str(record.worktree_path),
                        record.pid,
                        record.registered_at.isoformat(),
                        str(record.recording_path),
                        record.tab_name,
                        1 if record.is_review else 0,
                        str(record.run_dir),
                        record.process.birth_identity.kernel_token,
                    ),
                )
                conn.commit()
        except sqlite3.DatabaseError as exc:
            raise SubprocessRegistryError(
                f"could not persist subprocess session {record.session_name!r}"
            ) from exc

    def remove(self, session_name: str) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM sessions WHERE session_name = ?", (session_name,)
                )
                conn.commit()
        except sqlite3.DatabaseError as exc:
            raise SubprocessRegistryError(
                f"could not remove subprocess session {session_name!r}"
            ) from exc

class SubprocessPlugin:
    """Terminal plugin that uses subprocesses instead of tmux.

    Delegates process spawning to :class:`AgentRunner` which handles PTY
    creation, ``CleaningLogWriter`` setup, and process group isolation.
    This plugin manages session lifecycle: registry, existence checks, cleanup.
    """

    def __init__(
        self,
        session_terminator: TerminalSessionTerminator,
        process_group_supervisor: ProcessGroupSupervisor,
        watcher_policy: TerminalSessionWatcherPolicy,
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
        if type(watcher_policy) is not TerminalSessionWatcherPolicy:
            raise ValueError(
                "SubprocessPlugin.watcher_policy must be "
                "TerminalSessionWatcherPolicy"
            )
        repo_root = Path(get_env("REPO_ROOT") or Path.cwd()).resolve()
        self._registry = _SubprocessRegistry(repo_root)
        self._sessions: dict[str, AgentSession] = {}
        self._session_watchers: dict[str, TerminalSessionWatcher] = {}
        self._session_terminator = session_terminator
        self._process_group_supervisor = process_group_supervisor
        self._watcher_policy = watcher_policy
        deny_stdin_val = get_env("SUBPROCESS_DENY_STDIN") or ""
        self._allow_stdin = deny_stdin_val.lower() not in {"1", "true", "yes"}
        self._session_interactions_enabled = session_interactions_enabled
        self._worktree_base = (
            worktree_base.resolve() if worktree_base is not None else None
        )
        self._warned_missing_worktree_base = False

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
    ) -> AgentSession:
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

        spec = AgentSpec(
            command=[launch.shell.value, "-lc", full_cmd],
            working_dir=working_dir,
            timeout_seconds=7200,  # Sessions manage their own timeout via provider_runner
            log_path=log_path,
            output_dir=log_path.parent,
        )
        runner = AgentRunner()
        session = runner.start(spec, interaction_handler=interaction_handler)
        logger.info(
            "[subprocess] session started: session_name=%s pid=%s log_path=%s run_dir=%s",
            session_name,
            session.pid,
            log_path,
            log_path.parent,
        )

        return session

    def _identify_started_process(
        self,
        process_id: int,
        run_dir: Path,
    ) -> TerminalSessionProcess:
        identity = self._session_terminator.identify(process_id)
        if type(identity) is ProcessIdentityAbsent:
            raise SubprocessRegistryError(
                "new terminal process disappeared before its birth identity "
                f"could be persisted: pid={process_id}"
            )
        if type(identity) is ProcessIdentityPermissionDenied:
            raise SubprocessRegistryError(
                "permission denied while identifying a new terminal process: "
                f"pid={process_id} detail={identity.detail}"
            )
        if type(identity) is not ProcessIdentityPresent:
            raise AssertionError("process identity observation is a closed union")
        if identity.process_group_id != process_id:
            raise SubprocessRegistryError(
                "new terminal process is not its process-group leader: "
                f"pid={process_id} pgid={identity.process_group_id}"
            )
        return TerminalSessionProcess(
            process_id=process_id,
            birth_identity=identity.birth_identity,
            executor_cancellation=(
                ExecutorInteractiveSessionCancellation.for_run_dir(run_dir)
            ),
        )

    def _record_is_active(self, record: _SessionRecord) -> bool:
        """Resolve liveness without ever treating a recycled PID as the session."""
        session = self._sessions.get(record.session_name)
        if session is not None and session.is_alive():
            return True
        status = self._session_terminator.status(record.process)
        if status is TerminalSessionStatus.ACTIVE:
            return True
        self._session_terminator.terminate(record.process)
        return False

    def _kill_process(self, record: _SessionRecord) -> None:
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
            raise ValueError(
                "SubprocessPlugin.create_session requires TerminalLaunch"
            )
        worktree = Path(working_dir)
        if self.session_exists(session_id, session_name):
            return False

        pending_session = PendingTerminalSession(
            self._start_process(launch, worktree, session_name),
            self._process_group_supervisor,
        )
        session = pending_session.session
        is_review = session_name.startswith("review-")
        tab_name = title or session_name
        if is_review:
            try:
                pr_num = int(session_name.replace("review-", ""))
                tab_name = f"Review PR #{pr_num}"
            except ValueError:
                tab_name = session_name

        try:
            process = self._identify_started_process(
                session.pid,
                launch.destination.run_dir,
            )
        except BaseException as error:
            pending_session.abort(error)
            raise AssertionError("unreachable after unregistered-session cleanup")
        record = _SessionRecord(
            session_name=session_name,
            issue_number=session_id,
            worktree_path=worktree.resolve(),
            process=process,
            registered_at=datetime.now().astimezone(),
            recording_path=launch.destination.recording_path,
            tab_name=tab_name,
            is_review=is_review,
        )
        try:
            self._registry.upsert(record)
        except BaseException as error:
            pending_session.abort(error)
            raise AssertionError("unreachable after identified-session cleanup")
        try:
            watcher = TerminalSessionWatcher.start(session_name, session)
        except BaseException as watcher_error:
            try:
                self._registry.remove(session_name)
            except BaseException as rollback_error:
                pending_session.abort(
                    BaseExceptionGroup(
                        "terminal watcher startup and registry rollback failed",
                        [watcher_error, rollback_error],
                    )
                )
            pending_session.abort(watcher_error)
            raise AssertionError("unreachable after watcher-start cleanup")
        self._sessions[session_name] = session
        self._session_watchers[session_name] = watcher
        return True

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
                error.add_note(
                    "terminal shutdown session: " f"{record.session_name}"
                )
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
