"""Terminal plugin that runs agent sessions as subprocesses.

This provides a tmux-free execution option while still emitting a session log
per worktree for debugging and session health checks.

Delegates all process spawning to ``AgentRunner.start()`` which handles PTY
creation, ``CleaningLogWriter`` setup, and process group isolation. This plugin
only manages session lifecycle (registry, existence checks, cleanup).
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from ..control.isolation import build_agent_tool_path, build_isolation_prefix
from ..domain.terminal_launch import (
    TerminalInteractionIntent,
    TerminalLaunch,
    TerminalShell,
)
from .agent_runner import AgentRunner, AgentSession, AgentSpec
from .session_interactions import (
    SessionInteractionHandler,
    builtin_session_interaction_rules,
)
from ..infra.env import get_env
from ..infra.hooks.hookspec import hookimpl
from ..infra.repo_identity import state_dir
from ..infra.sqlite_connection import open_sqlite
from ..infra.terminal_recording import TERMINAL_RECORDING_FILENAME

logger = logging.getLogger(__name__)
_RUN_DIR_ENV_RE = re.compile(r"ISSUE_ORCHESTRATOR_RUN_DIR=(['\"]?)([^'\"\s]+)\1")


@dataclass
class _SessionRecord:
    session_name: str
    issue_number: int
    worktree_path: str
    pid: int
    started_at: str
    log_path: str
    tab_name: str
    is_review: bool


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

    def _connect(self) -> sqlite3.Connection:
        return open_sqlite(self._db_path)
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
                    is_review INTEGER NOT NULL
                )
                """
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
        except Exception as exc:
            logger.warning("Failed to migrate legacy session DB: %s", exc)

    def _migrate_legacy_if_needed(self) -> None:
        if self._db_path.exists() and self._has_rows():
            return
        legacy_records = self._load_legacy_records()
        if not legacy_records:
            return
        for record in legacy_records.values():
            self.upsert(record)

    def _has_rows(self) -> bool:
        try:
            with self._connect() as conn:
                cur = conn.execute("SELECT 1 FROM sessions LIMIT 1")
                return cur.fetchone() is not None
        except sqlite3.DatabaseError:
            self._handle_corrupt_db()
            return False

    def _handle_corrupt_db(self) -> None:
        corrupt_path = self._db_path.with_suffix(".sqlite.corrupt")
        try:
            if self._db_path.exists():
                self._db_path.replace(corrupt_path)
        except Exception:
            pass
        self._ensure_db()

    def _load_legacy_records(self) -> dict[str, _SessionRecord]:
        if self._legacy_index.exists() or self._legacy_backup.exists():
            for path in (self._legacy_index, self._legacy_backup):
                if not path.exists():
                    continue
                try:
                    raw = json.loads(path.read_text())
                except Exception:
                    continue
                records = self._records_from_payload(raw)
                if records:
                    return records
        if self._legacy_dir.exists():
            records: dict[str, _SessionRecord] = {}
            for path in sorted(self._legacy_dir.glob("*.json")):
                try:
                    raw = json.loads(path.read_text())
                    record = _SessionRecord(**raw)
                    records[record.session_name] = record
                except Exception:
                    continue
            return records
        return {}

    def _records_from_payload(self, raw: dict) -> dict[str, _SessionRecord]:
        records: dict[str, _SessionRecord] = {}
        for name, data in raw.items():
            try:
                records[name] = _SessionRecord(**data)
            except TypeError:
                continue
        return records

    def load(self) -> dict[str, _SessionRecord]:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "SELECT session_name, issue_number, worktree_path, pid, started_at, log_path, tab_name, is_review "
                    "FROM sessions"
                )
                records = {}
                for row in cur.fetchall():
                    records[row[0]] = _SessionRecord(
                        session_name=row[0],
                        issue_number=row[1],
                        worktree_path=row[2],
                        pid=row[3],
                        started_at=row[4],
                        log_path=row[5],
                        tab_name=row[6],
                        is_review=bool(row[7]),
                    )
                return records
        except sqlite3.DatabaseError:
            logger.warning("Failed to read subprocess registry db: %s", self._db_path)
            self._handle_corrupt_db()
            return {}

    def upsert(self, record: _SessionRecord) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO sessions (session_name, issue_number, worktree_path, pid, started_at, log_path, tab_name, is_review)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_name) DO UPDATE SET
                        issue_number=excluded.issue_number,
                        worktree_path=excluded.worktree_path,
                        pid=excluded.pid,
                        started_at=excluded.started_at,
                        log_path=excluded.log_path,
                        tab_name=excluded.tab_name,
                        is_review=excluded.is_review
                    """,
                    (
                        record.session_name,
                        record.issue_number,
                        record.worktree_path,
                        record.pid,
                        record.started_at,
                        record.log_path,
                        record.tab_name,
                        1 if record.is_review else 0,
                    ),
                )
                conn.commit()
        except sqlite3.DatabaseError:
            self._handle_corrupt_db()

    def remove(self, session_name: str) -> None:
        try:
            with self._connect() as conn:
                conn.execute("DELETE FROM sessions WHERE session_name = ?", (session_name,))
                conn.commit()
        except sqlite3.DatabaseError:
            self._handle_corrupt_db()

    def clear(self) -> None:
        try:
            with self._connect() as conn:
                conn.execute("DELETE FROM sessions")
                conn.commit()
        except sqlite3.DatabaseError:
            self._handle_corrupt_db()


class SubprocessPlugin:
    """Terminal plugin that uses subprocesses instead of tmux.

    Delegates process spawning to :class:`AgentRunner` which handles PTY
    creation, ``CleaningLogWriter`` setup, and process group isolation.
    This plugin manages session lifecycle: registry, existence checks, cleanup.
    """

    def __init__(
        self,
        *,
        session_interactions_enabled: bool = False,
        worktree_base: Path | None = None,
    ) -> None:
        repo_root = Path(get_env("REPO_ROOT") or Path.cwd()).resolve()
        self._registry = _SubprocessRegistry(repo_root)
        self._sessions: dict[str, AgentSession] = {}
        self._watcher_threads: dict[str, threading.Thread] = {}
        deny_stdin_val = get_env("SUBPROCESS_DENY_STDIN") or ""
        self._allow_stdin = deny_stdin_val.lower() not in {"1", "true", "yes"}
        self._session_interactions_enabled = session_interactions_enabled
        self._worktree_base = worktree_base.resolve() if worktree_base is not None else None
        self._warned_missing_worktree_base = False

    def _session_log_path(self, working_dir: Path, session_name: str, command: str | None = None) -> Path:
        if command:
            match = _RUN_DIR_ENV_RE.search(command)
            if match:
                run_dir = Path(match.group(2))
                if not run_dir.is_absolute():
                    run_dir = (working_dir / run_dir).resolve()
                run_dir.mkdir(parents=True, exist_ok=True)
                return run_dir / TERMINAL_RECORDING_FILENAME
        raise ValueError(
            "terminal session creation requires ISSUE_ORCHESTRATOR_RUN_DIR in command"
        )

    def _build_process_command(self, command: str, working_dir: Path) -> str:
        """Build the full command with path and isolation prefix."""
        path_prefix = build_agent_tool_path(working_dir, os.environ.get("PATH", ""))
        isolation_prefix = build_isolation_prefix(working_dir, scrub_env=True, isolate_home=False)
        return f'cd "{working_dir}" && export PATH="{path_prefix}" && {isolation_prefix}{command}'

    def _start_session_watcher(self, session: AgentSession, session_name: str) -> None:
        """Start a thread that waits for the session to complete."""

        def _watch() -> None:
            logger.info(
                "[subprocess] watcher started: session_name=%s pid=%s",
                session_name,
                session.pid,
            )
            result = session.wait()  # Blocks until exit; auto-flushes log
            logger.info(
                "[subprocess] watcher completed: session_name=%s pid=%s exit_code=%s timed_out=%s duration=%.1fs",
                session_name,
                session.pid,
                result.exit_code,
                result.timed_out,
                result.duration_seconds,
            )

        thread = threading.Thread(target=_watch, daemon=True)
        self._watcher_threads[session_name] = thread
        thread.start()

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
        log_path = self._session_log_path(
            working_dir,
            session_name,
            launch.shell_command,
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

        self._sessions[session_name] = session
        self._start_session_watcher(session, session_name)
        return session

    def _process_alive(self, pid: int, session_name: str | None = None) -> bool:
        """Check if a process is still running."""
        session = self._sessions.get(session_name) if session_name else None
        if session is not None:
            return session.is_alive()
        # Fall back to kill(0) check for recovered sessions without a handle
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _kill_process(self, pid: int, session_name: str | None = None) -> None:
        """Kill a process, trying AgentSession first."""
        session = self._sessions.get(session_name) if session_name else None
        if session is not None:
            session.kill()
            return
        # Fall back to manual kill for recovered sessions without a handle
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                return

    @hookimpl
    def create_session(
        self,
        session_id: int,
        command: str,
        interaction_intent: TerminalInteractionIntent,
        shell: TerminalShell,
        working_dir: str,
        title: str | None,
        session_name: str,  # Required - caller must provide explicit name
    ) -> bool | None:
        logger.info(
            "[subprocess] create_session called: session_id=%s session_name=%r",
            session_id,
            session_name,
        )
        worktree = Path(working_dir)
        if self.session_exists(session_id, session_name):
            return False

        launch = TerminalLaunch(command, shell, interaction_intent)
        session = self._start_process(launch, worktree, session_name)
        is_review = session_name.startswith("review-")
        tab_name = title or session_name
        if is_review:
            try:
                pr_num = int(session_name.replace("review-", ""))
                tab_name = f"Review PR #{pr_num}"
            except ValueError:
                tab_name = session_name

        assert session.pid is not None, "AgentSession has no pid"
        record = _SessionRecord(
            session_name=session_name,
            issue_number=session_id,
            worktree_path=str(worktree.resolve()),
            pid=session.pid,
            started_at=datetime.now().isoformat(),
            log_path=str(self._session_log_path(worktree, session_name, command)),
            tab_name=tab_name,
            is_review=is_review,
        )
        self._registry.upsert(record)
        return True

    def _wait_for_watcher_thread(self, session_name: str, timeout: float = 2.0) -> None:
        """Wait for the watcher thread to finish and clean up resources."""
        thread = self._watcher_threads.get(session_name)
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._watcher_threads.pop(session_name, None)

    def _cleanup_session(self, session_name: str) -> None:
        """Clean up all resources for a session."""
        self._wait_for_watcher_thread(session_name)
        self._sessions.pop(session_name, None)

    @hookimpl
    def session_exists(self, session_id: int, session_name: str) -> bool | None:
        records = self._registry.load()
        record = records.get(session_name)
        if not record:
            return False
        if self._process_alive(record.pid, session_name):
            return True
        # Process is dead - wait for watcher thread to finish flushing output
        logger.info(
            "[subprocess] session no longer alive: session_name=%s pid=%s log_path=%s",
            session_name,
            record.pid,
            record.log_path,
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
        self._kill_process(record.pid, session_name)
        self._cleanup_session(session_name)
        self._registry.remove(session_name)
        return True

    @hookimpl
    def discover_running_sessions(self) -> list[dict] | None:
        records = self._registry.load()
        running: list[dict] = []
        for record in records.values():
            if self._process_alive(record.pid, record.session_name):
                running.append({
                    "issue_number": record.issue_number,
                    "tab_name": record.tab_name,
                    "is_review": record.is_review,
                    "session_name": record.session_name,
                    "run_dir": str(Path(record.log_path).parent),
                })
            else:
                self._cleanup_session(record.session_name)
                self._registry.remove(record.session_name)
        return running

    @hookimpl
    def cleanup_idle_sessions(self) -> int | None:
        records = self._registry.load()
        cleaned = 0
        for record in list(records.values()):
            if not self._process_alive(record.pid, record.session_name):
                self._cleanup_session(record.session_name)
                self._registry.remove(record.session_name)
                cleaned += 1
        return cleaned

    @hookimpl
    def get_session_output(self, session_id: int, lines: int, session_name: str) -> str | None:
        record = self._registry.load().get(session_name)
        if not record:
            return None
        log_path = Path(record.log_path)
        if not log_path.exists():
            return ""
        try:
            content = log_path.read_text()
        except Exception:
            return ""
        output_lines = content.splitlines()
        return "\n".join(output_lines[-lines:]) if output_lines else ""

    @hookimpl
    def send_to_session(self, session_id: int, text: str, session_name: str) -> bool | None:
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
        for record in records.values():
            if self._process_alive(record.pid, record.session_name):
                self._kill_process(record.pid, record.session_name)
        # Wait for all watcher threads to finish
        for session_name in list(self._watcher_threads.keys()):
            self._wait_for_watcher_thread(session_name, timeout=1.0)
        self._sessions.clear()
        self._registry.clear()

    @hookimpl
    def terminal_health_check(self) -> dict | None:
        return {
            "healthy": True,
            "server_running": True,
            "session_exists": bool(self._registry.load()),
            "error": None,
        }
