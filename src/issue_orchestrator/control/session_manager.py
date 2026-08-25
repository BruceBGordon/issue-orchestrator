"""SessionManager - terminal session lifecycle management.

This module owns:
1. Session naming conventions (issue-N, review-N, rework-N)
2. Session lifecycle (start, stop, exists, status)
3. Worktree path conventions
4. Session reference types

It delegates actual terminal operations to the SessionRunner port.

Usage:
    manager = SessionManager(runner=session_runner, events=event_sink, config=config)
    ref = manager.start_issue_session(issue_number=123, command="claude", worktree_path=path)
    if manager.exists(ref):
        manager.stop(ref)
"""

import logging
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from ..infra.config import Config
from ..events import EventName
from ..ports import EventSink, SessionRunner, make_trace_event
from ..domain.terminal_launch import TerminalLaunch, TerminalShell

logger = logging.getLogger(__name__)


class SessionType(Enum):
    """Types of agent sessions."""

    ISSUE = "issue"
    REVIEW = "review"
    RETROSPECTIVE_REVIEW = "retrospective-review"
    REWORK = "rework"
    TECH_LEAD = "tech-lead"


@dataclass(frozen=True)
class SessionRef:
    """Reference to a terminal session.

    This is the canonical way to refer to a session throughout the codebase.
    It contains all the information needed to interact with the session.
    """

    session_type: SessionType
    number: int  # Issue number for issue/rework, PR number for review

    @property
    def name(self) -> str:
        """Get the terminal session name (e.g., 'issue-123')."""
        return f"{self.session_type.value}-{self.number}"

    @classmethod
    def from_name(cls, session_name: str) -> "SessionRef":
        """Parse a session name into a SessionRef.

        Args:
            session_name: Name like "issue-123", "review-456", "rework-123"

        Returns:
            SessionRef with parsed type and number

        Raises:
            ValueError: If session name format is invalid
        """
        match = re.match(r"^(issue|review|retrospective-review|rework|tech-lead)-(\d+)$", session_name)
        if not match:
            raise ValueError(f"Invalid session name format: {session_name}")
        session_type = SessionType(match.group(1))
        number = int(match.group(2))
        return cls(session_type=session_type, number=number)

    @classmethod
    def for_issue(cls, issue_number: int) -> "SessionRef":
        """Create a session reference for an issue."""
        return cls(session_type=SessionType.ISSUE, number=issue_number)

    @classmethod
    def for_review(cls, pr_number: int) -> "SessionRef":
        """Create a session reference for a review."""
        return cls(session_type=SessionType.REVIEW, number=pr_number)

    @classmethod
    def for_retrospective_review(cls, issue_number: int) -> "SessionRef":
        """Create a session reference for a retrospective review."""
        return cls(session_type=SessionType.RETROSPECTIVE_REVIEW, number=issue_number)

    @classmethod
    def for_rework(cls, issue_number: int) -> "SessionRef":
        """Create a session reference for a rework."""
        return cls(session_type=SessionType.REWORK, number=issue_number)

    @classmethod
    def for_tech_lead(cls, issue_number: int) -> "SessionRef":
        """Create a session reference for a tech_lead review."""
        return cls(session_type=SessionType.TECH_LEAD, number=issue_number)


@dataclass
class SessionContext:
    """Context for starting a session.

    Contains all the information needed to launch an agent session.
    """

    ref: SessionRef
    launch: TerminalLaunch
    working_dir: Path
    title: Optional[str] = None


class SessionManager:
    """Manages terminal session lifecycle.

    This class owns:
    - Session naming conventions
    - Session lifecycle (start, stop, exists)
    - Worktree path conventions

    It delegates to SessionRunner for actual terminal operations
    and emits events via EventSink.
    """

    def __init__(
        self,
        runner: SessionRunner,
        events: EventSink,
        config: Config,
    ):
        """Initialize the SessionManager.

        Args:
            runner: SessionRunner port for terminal operations
            events: EventSink for trace events
            config: Configuration with repo settings
        """
        self.runner = runner
        self.events = events
        self.config = config

    def get_worktree_path(
        self,
        issue_number: int,
        worktree_base: Optional[Path] = None,
        repo_root: Optional[Path] = None,
    ) -> Path:
        """Get the worktree path for a given issue number.

        Args:
            issue_number: The GitHub issue number
            worktree_base: Override base directory for worktrees
            repo_root: Override repository root

        Returns:
            Path to the worktree directory
        """
        actual_repo_root = repo_root or self.config.repo_root
        actual_worktree_base = worktree_base
        if actual_worktree_base is None:
            actual_worktree_base = actual_repo_root.parent
        else:
            actual_worktree_base = Path(actual_worktree_base).resolve()

        repo_name = actual_repo_root.name
        return actual_worktree_base / f"{repo_name}-{issue_number}"

    def start(self, ctx: SessionContext) -> bool:
        """Start a terminal session.

        Args:
            ctx: Session context with all launch parameters

        Returns:
            True if session started successfully, False otherwise
        """
        success = self.runner.create_session(
            session_id=ctx.ref.number,
            launch=ctx.launch,
            working_dir=str(ctx.working_dir),
            title=ctx.title,
            session_name=ctx.ref.name,
        )

        if success:
            self.events.publish(
                make_trace_event(
                    EventName.SESSION_LAUNCHED,
                    {
                        "session_type": ctx.ref.session_type.value,
                        "number": ctx.ref.number,
                        "session_name": ctx.ref.name,
                        "working_dir": str(ctx.working_dir),
                    },
                )
            )
            logger.info(f"Started session: {ctx.ref.name}")
        else:
            self.events.publish(
                make_trace_event(
                    EventName.SESSION_START_FAILED,
                    {
                        "session_type": ctx.ref.session_type.value,
                        "number": ctx.ref.number,
                        "session_name": ctx.ref.name,
                    },
                )
            )
            logger.error(f"Failed to start session: {ctx.ref.name}")

        return success

    def stop(self, ref: SessionRef) -> None:
        """Stop a terminal session.

        Args:
            ref: Reference to the session to stop
        """
        self.runner.kill_session(ref.number, session_name=ref.name)
        self.events.publish(
            make_trace_event(
                EventName.SESSION_STOPPED,
                {
                    "session_type": ref.session_type.value,
                    "number": ref.number,
                    "session_name": ref.name,
                },
            )
        )
        logger.info(f"Stopped session: {ref.name}")

    def exists(self, ref: SessionRef) -> bool:
        """Check if a session exists and is running.

        Args:
            ref: Reference to the session to check

        Returns:
            True if session exists and is running
        """
        return self.runner.session_exists(ref.number, session_name=ref.name)

    def get_output(self, ref: SessionRef, lines: int = 50) -> Optional[str]:
        """Get recent output from a session.

        Args:
            ref: Reference to the session
            lines: Number of lines to retrieve

        Returns:
            Terminal output string, or None if not available
        """
        return self.runner.get_session_output(ref.number, lines, session_name=ref.name)

    def discover_running(self) -> list[SessionRef]:
        """Discover sessions that survived an orchestrator restart.

        Returns:
            List of SessionRefs for running sessions
        """
        running = self.runner.discover_running_sessions()
        refs = []
        for session_info in running:
            try:
                # Session info has 'tab_name' with format like "issue-123"
                tab_name = session_info.get("tab_name", "")
                ref = SessionRef.from_name(tab_name)
                refs.append(ref)
            except ValueError:
                logger.warning(f"Could not parse session info: {session_info}")
        return refs

    def cleanup_idle(self) -> int:
        """Clean up sessions where the agent has exited.

        Returns:
            Number of sessions cleaned up
        """
        count = self.runner.cleanup_idle_sessions()
        if count > 0:
            self.events.publish(
                make_trace_event(
                    EventName.SESSION_CLEANUP,
                    {"cleaned_count": count},
                )
            )
            logger.info(f"Cleaned up {count} idle sessions")
        return count


# Convenience functions for creating session contexts


def issue_session_context(
    issue_number: int,
    command: str,
    working_dir: Path,
    title: Optional[str] = None,
) -> SessionContext:
    """Create a context for launching an issue session."""
    return SessionContext(
        ref=SessionRef.for_issue(issue_number),
        launch=TerminalLaunch.classified(command, TerminalShell.BASH),
        working_dir=working_dir,
        title=title or f"Issue #{issue_number}",
    )


def review_session_context(
    pr_number: int,
    command: str,
    working_dir: Path,
    title: Optional[str] = None,
) -> SessionContext:
    """Create a context for launching a review session."""
    return SessionContext(
        ref=SessionRef.for_review(pr_number),
        launch=TerminalLaunch.classified(command, TerminalShell.BASH),
        working_dir=working_dir,
        title=title or f"Review PR #{pr_number}",
    )


def rework_session_context(
    issue_number: int,
    command: str,
    working_dir: Path,
    title: Optional[str] = None,
) -> SessionContext:
    """Create a context for launching a rework session."""
    return SessionContext(
        ref=SessionRef.for_rework(issue_number),
        launch=TerminalLaunch.classified(command, TerminalShell.BASH),
        working_dir=working_dir,
        title=title or f"Rework Issue #{issue_number}",
    )
