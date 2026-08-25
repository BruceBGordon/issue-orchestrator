"""Port: build a configured :class:`SessionLauncher`.

The launcher needs twenty collaborators. Most are application
dependencies the composition root already owns; a handful are the
orchestrator facade's own state-machine accessors and callbacks, known
only at call time.

Splitting it this way keeps assembly at the composition boundary while
letting the facade supply what is genuinely its own. Previously the
facade rummaged through the whole dependency bundle inline, and an
earlier attempt to extract that moved the rummaging into the control
layer instead — worse, not better (#6924 A3/A3-R2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional, Protocol

if TYPE_CHECKING:
    from ..control.dependency_evaluator import DependencyEvaluator
    from ..control.session_launcher import SessionLauncher
    from ..domain.state_machines.issue_machine import IssueStateMachine
    from ..domain.state_machines.review_machine import ReviewStateMachine
    from ..domain.state_machines.session_machine import SessionStateMachine
    from .board_snapshot_provider import BoardSnapshotProvider
    from .issue import Issue as IssueProtocol


class SessionLauncherFactory(Protocol):
    """Builds a launcher from facade-owned callbacks.

    The implementation closes over the application dependencies; callers
    pass only what the facade owns.
    """

    def __call__(
        self,
        *,
        board_snapshot_provider: "BoardSnapshotProvider",
        session_exists_fn: Callable[[str], bool],
        get_issue_machine: Callable[["IssueProtocol"], Optional["IssueStateMachine"]],
        get_session_machine: Callable[[str, int, int], Optional["SessionStateMachine"]],
        get_review_machine: Callable[[int, int], Optional["ReviewStateMachine"]],
        refresh_issue_fn: Optional[
            Callable[[int], Optional["IssueProtocol"]]
        ],
        dependency_evaluator: Optional["DependencyEvaluator"],
    ) -> "SessionLauncher":
        ...
