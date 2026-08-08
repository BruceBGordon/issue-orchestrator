"""Port: what an operator's "retry" and "dismiss" buttons actually do (#6999 F5).

These two commands are not label edits with some bookkeeping after them. Each is
ONE transition across two stores that must agree: the labels GitHub carries, and
the in-memory retry/queue/history state the planner reads. The invariant joining
them has a direction - *local state may only be pruned once the GitHub side of
the transition committed* - because pruning first hides an issue GitHub still
blocks and lets the planner relaunch straight into it.

That invariant needs one implementation. It previously had none: the HTTP layer
constructed the label owner, branched on its fields, then reset retry history,
replaced session history, rewrote cached issue state and rebuilt a queue cache
itself - so the ordering rule lived in the transport, once per endpoint, and
drifted between them.

So the transport asks for a COMMAND and maps ONE typed outcome. It does not know
the label owner, the retry-history representation, the queue cache, or the order
they must be touched in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Protocol, TypeVar

if TYPE_CHECKING:
    from ..control.operator_issue_command_runner import OperatorCommandOutcome
    from ..domain.models import OrchestratorState

_T = TypeVar("_T")


class LockedRunner(Protocol):
    """Runs a callable under the facade's state lock, returning its result."""

    def __call__(self, fn: Callable[[], _T]) -> _T: ...


class OperatorIssueCommands(Protocol):
    """The two operator commands, each a single settled transition."""

    def retry(self, issue_number: int) -> "OperatorCommandOutcome":
        """Unblock the issue and make the planner eligible to pick it up."""
        ...

    def dismiss(self, issue_number: int) -> "OperatorCommandOutcome":
        """Unblock the issue and take it off the board without retrying."""
        ...


class OperatorIssueCommandFactory(Protocol):
    """Builds the commands from facade-owned runtime state.

    Same split as :mod:`.completion_handler_factory`, for the same reason: every
    collaborator but two belongs to the composition root, and those two - the
    live state and the lock guarding it - belong to the orchestrator facade.
    Assembling at the boundary keeps control code out of the dependency
    container and the facade out of the wiring.
    """

    def __call__(
        self,
        *,
        state: Callable[[], "OrchestratorState"],
        run_locked: LockedRunner,
    ) -> OperatorIssueCommands:
        ...


__all__ = [
    "LockedRunner",
    "OperatorIssueCommandFactory",
    "OperatorIssueCommands",
]
