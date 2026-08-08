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

The outcome lives HERE rather than in the control implementation (#6999 F6): a
port whose return type is owned by one concrete implementation is not a contract
anybody can depend on, it is that implementation wearing a Protocol. And the
outcome carries FACTS - what happened, to which labels, held by whom - never a
response body. How those facts are phrased and shaped is transport policy, and
lives at the transport edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Callable, Protocol, TypeVar

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState

_T = TypeVar("_T")


class OperatorCommandIntent(Enum):
    """Which button the operator pressed.

    Carried as an enum rather than a rendered verb so the command never has to
    choose the operator's words: "queued for retry" and "dismissed" are things
    to SAY about a transition, and saying them is the transport's job.
    """

    RETRY = "retry"
    DISMISS = "dismiss"


class OperatorCommandStatus(Enum):
    """How far an operator command got. The transport maps only this."""

    #: Labels cleared AND local state settled. The operator's request happened.
    COMMITTED = "committed"
    #: The shared needs-human block is still on the issue - its owner refused,
    #: or the write did not commit. Nothing after it was touched.
    STILL_BLOCKED = "still_blocked"
    #: Ordinary gating labels would not come off GitHub, so local state was
    #: deliberately left in place rather than letting the planner relaunch.
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class OperatorCommandOutcome:
    """One settled operator transition, as facts rather than as a response."""

    intent: OperatorCommandIntent
    status: OperatorCommandStatus
    issue_number: int
    removed: tuple[str, ...] = ()
    #: Ordinary labels GitHub would not remove. A genuine failed write: the
    #: repository adapter already treats an absent label as idempotent success
    #: and already retries transport faults, so anything surfacing here means
    #: the label is still on the issue (#6999 F5 round 7).
    failed: tuple[str, ...] = ()
    #: The shared block, when it is what stopped the command.
    blocked: str | None = None
    #: Which lifecycles are holding that block, for the operator to act on.
    held_by: tuple[str, ...] = field(default_factory=tuple)

    @property
    def committed(self) -> bool:
        return self.status is OperatorCommandStatus.COMMITTED


class LockedRunner(Protocol):
    """Runs a callable under the facade's state lock, returning its result."""

    def __call__(self, fn: Callable[[], _T]) -> _T: ...


class OperatorIssueCommands(Protocol):
    """The two operator commands, each a single settled transition."""

    def retry(self, issue_number: int) -> OperatorCommandOutcome:
        """Unblock the issue and make the planner eligible to pick it up."""
        ...

    def dismiss(self, issue_number: int) -> OperatorCommandOutcome:
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
    "OperatorCommandIntent",
    "OperatorCommandOutcome",
    "OperatorCommandStatus",
    "OperatorIssueCommandFactory",
    "OperatorIssueCommands",
]
