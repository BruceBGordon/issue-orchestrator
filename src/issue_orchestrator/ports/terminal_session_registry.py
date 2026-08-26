"""Port for durable terminal-session launch ownership."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.terminal_session_registry import (
    PendingTerminalSessionRecord,
    TerminalSessionRecord,
)


@runtime_checkable
class TerminalSessionRegistry(Protocol):
    """Atomically persist pending and identified terminal ownership."""

    def load(self) -> dict[str, TerminalSessionRecord]: ...

    def load_pending(self) -> tuple[PendingTerminalSessionRecord, ...]: ...

    def begin_launch(self, record: PendingTerminalSessionRecord) -> None: ...

    def upsert(self, record: TerminalSessionRecord) -> None: ...

    def commit_launch(
        self,
        pending: PendingTerminalSessionRecord,
        record: TerminalSessionRecord,
    ) -> None: ...

    def remove_pending(self, session_name: str) -> None: ...

    def remove(self, session_name: str) -> None: ...
