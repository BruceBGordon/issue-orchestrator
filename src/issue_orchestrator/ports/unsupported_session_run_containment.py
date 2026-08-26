"""Port for containing a live terminal whose run contract is unsupported."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.session_restoration import UnsupportedSessionRun


@runtime_checkable
class UnsupportedSessionRunContainment(Protocol):
    """Stop unsupported live work before allowing startup to continue."""

    def contain(self, run: UnsupportedSessionRun) -> None:
        """Contain the terminal or fail startup loudly."""
        ...
