"""Port for collision-resistant kernel process identity observation."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.process_group import ProcessIdentityObservation


@runtime_checkable
class ProcessIdentityObserver(Protocol):
    """Observe a PID's exact kernel birth token and process group."""

    def observe_process(self, process_id: int) -> ProcessIdentityObservation:
        """Return a closed exact identity observation for one PID."""
        ...


@runtime_checkable
class ProcessSessionResolver(Protocol):
    """Ask the kernel which session one PID belongs to.

    macOS ``ps`` prints 0 in the sess column for every process, so
    session membership cannot be read from the process table there;
    ``getsid(2)`` is the kernel's own answer. ``None`` means the kernel
    would not answer for that PID (gone, zombie, or denied), so the PID
    cannot be proven a member.
    """

    def resolve_session(self, process_id: int) -> int | None:
        ...
