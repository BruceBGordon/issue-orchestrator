"""Port: build a configured :class:`CompletionHandler`.

Same split as :mod:`.session_launcher_factory`, for the same reason. Most of
the handler's collaborators are application dependencies the composition root
already owns; its state-machine registry is facade-owned runtime state.

Keeping the assembly at the composition boundary means control code never has
to know the facade's dependency-container layout, and the facade never has to
rummage through it (#6999 A4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ..control.completion_handler import CompletionHandler
    from ..control.state_machine_manager import StateMachineManager


class CompletionHandlerFactory(Protocol):
    """Builds a completion handler from facade-owned runtime state."""

    def __call__(
        self,
        *,
        state_machines: "StateMachineManager",
    ) -> "CompletionHandler": ...
