"""Strong records for durable terminal-session launch ownership."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .terminal_session_termination import TerminalSessionProcess


@dataclass(frozen=True, slots=True)
class PendingTerminalSessionRecord:
    """Durable launch intent committed before any terminal process is spawned."""

    session_name: str
    issue_number: int
    worktree_path: Path
    run_dir: Path
    registered_at: datetime
    recording_path: Path
    tab_name: str
    is_review: bool

    def __post_init__(self) -> None:
        if type(self.session_name) is not str or not self.session_name:
            raise ValueError(
                "PendingTerminalSessionRecord.session_name must not be empty"
            )
        if type(self.issue_number) is not int or self.issue_number < 0:
            raise ValueError(
                "PendingTerminalSessionRecord.issue_number must be non-negative"
            )
        if not self.worktree_path.is_absolute():
            raise ValueError(
                "PendingTerminalSessionRecord.worktree_path must be absolute"
            )
        if not self.run_dir.is_relative_to(self.worktree_path):
            raise ValueError(
                "PendingTerminalSessionRecord.run_dir must belong to worktree_path"
            )
        if not self.recording_path.is_relative_to(self.run_dir):
            raise ValueError(
                "PendingTerminalSessionRecord.recording_path must belong to run_dir"
            )
        if self.registered_at.tzinfo is None:
            raise ValueError(
                "PendingTerminalSessionRecord.registered_at must be timezone-aware"
            )
        if type(self.tab_name) is not str or not self.tab_name:
            raise ValueError(
                "PendingTerminalSessionRecord.tab_name must not be empty"
            )
        if type(self.is_review) is not bool:
            raise ValueError("PendingTerminalSessionRecord.is_review must be bool")


@dataclass(frozen=True, slots=True)
class TerminalSessionRecord:
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
            raise ValueError("TerminalSessionRecord.session_name must not be empty")
        if type(self.issue_number) is not int or self.issue_number < 0:
            raise ValueError(
                "TerminalSessionRecord.issue_number must be non-negative"
            )
        if not self.worktree_path.is_absolute():
            raise ValueError("TerminalSessionRecord.worktree_path must be absolute")
        if type(self.process) is not TerminalSessionProcess:
            raise ValueError(
                "TerminalSessionRecord.process must be TerminalSessionProcess"
            )
        if self.registered_at.tzinfo is None:
            raise ValueError(
                "TerminalSessionRecord.registered_at must be timezone-aware"
            )
        if not self.recording_path.is_absolute():
            raise ValueError("TerminalSessionRecord.recording_path must be absolute")
        if not self.run_dir.is_relative_to(self.worktree_path):
            raise ValueError(
                "TerminalSessionRecord.run_dir must belong to worktree_path"
            )
        if not self.recording_path.is_relative_to(self.run_dir):
            raise ValueError(
                "TerminalSessionRecord.recording_path must belong to run_dir"
            )
        if type(self.tab_name) is not str or not self.tab_name:
            raise ValueError("TerminalSessionRecord.tab_name must not be empty")
        if type(self.is_review) is not bool:
            raise ValueError("TerminalSessionRecord.is_review must be bool")

    @property
    def pid(self) -> int:
        return self.process.process_id

    @property
    def run_dir(self) -> Path:
        return self.process.executor_cancellation.record_path.parent
