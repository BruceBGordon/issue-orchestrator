"""Typed owner for creating a terminal session from its canonical name."""

from __future__ import annotations

from pathlib import Path

from ..domain.terminal_launch import TerminalLaunch
from ..ports.event_sink import EventSink
from .session_manager import SessionManager
from .session_routing import create_session


class NamedSessionCreator:
    """Bind session-name validation and terminal creation behind one callable."""

    def __init__(self, session_manager: SessionManager, events: EventSink) -> None:
        self._session_manager = session_manager
        self._events = events

    def __call__(
        self,
        name: str,
        launch: TerminalLaunch,
        working_dir: Path,
        title: str | None,
    ) -> bool:
        return create_session(
            name,
            launch,
            working_dir,
            title,
            self._session_manager,
            self._events,
        )
