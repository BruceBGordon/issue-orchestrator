"""Behaviour-complete termination of a tech-lead session (#6824 R7).

Extracted from the orchestrator facade, which coordinates rather than executes:
the effects below and — more importantly — the rule that each is attempted
INDEPENDENTLY are policy, and policy belongs beside the typed outcome it
produces rather than inside a facade method.

``kill_session`` only stops the terminal, and the one-shot driver that calls
this runs NO further tick afterwards — so a recorded cleanup fact would never be
applied. The termination is therefore self-contained, mirroring the outcomes
normal completion produces: remove the session state machine, stop the terminal,
reconcile the session out of ``active_sessions``, release its claim, and
FORCE-remove the disposable scratch worktree.

A failure of one effect never aborts the others, and the result is a typed
:class:`~.tech_lead_trigger.TechLeadTerminationOutcome` — the SOLE owner of a
failed one-shot cleanup. On a scratch-worktree removal failure the outcome
carries the exact ``leaked_worktree`` path so the caller can require explicit
operator removal; there is no second, tick-based retry mechanism to defer to.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Callable, Protocol

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState, Session
    from .tech_lead_trigger import TechLeadTerminationOutcome

logger = logging.getLogger(__name__)


class TechLeadTerminationHost(Protocol):
    """The facade surface a termination drives.

    Structural, so this control owner never imports the infra facade — and so a
    test can supply exactly the collaborators the effects touch.
    """

    @property
    def state(self) -> "OrchestratorState": ...

    @property
    def deps(self) -> object: ...

    def kill_session(self, name: str) -> None: ...


def terminate_tech_lead_session(
    host: TechLeadTerminationHost, session: "Session"
) -> "TechLeadTerminationOutcome":
    """Stop the session and clean up after it, reporting what actually worked."""
    from .tech_lead_trigger import TechLeadTerminationOutcome

    number = session.issue.number
    attempt = _effect_runner(number)
    deps = host.deps

    smm = getattr(deps, "state_machine_manager", None)
    machine_removed = attempt(
        lambda: smm.remove_session_machine(session.terminal_id) if smm else None,
        "remove state machine",
    )
    terminal_stopped = attempt(
        lambda: host.kill_session(session.terminal_id), "stop terminal"
    )
    host.state.drop_active_session(session.terminal_id)  # pure in-memory owner op

    claims = getattr(deps, "claim_manager", None)
    lease_id = getattr(session, "lease_id", None)
    claim_released = attempt(
        lambda: claims.release_claim(number, lease_id)
        if (claims and lease_id)
        else None,
        "release claim",
    )

    worktrees = getattr(deps, "worktree_manager", None)
    disposable = bool(
        getattr(session, "scratch_worktree", False) and session.worktree_path
    )
    worktree_removed = attempt(
        lambda: worktrees.remove(session.worktree_path, force=True)
        if (disposable and worktrees)
        else None,
        "remove scratch worktree",
    )
    return TechLeadTerminationOutcome(
        terminal_stopped=terminal_stopped,
        machine_removed=machine_removed,
        claim_released=claim_released,
        worktree_removed=worktree_removed,
        # A failed removal surfaces the EXACT leaked path for explicit operator
        # action before exit — this is the single cleanup-failure owner.
        leaked_worktree=(
            str(session.worktree_path)
            if (disposable and not worktree_removed)
            else None
        ),
    )


def _effect_runner(issue_number: int) -> Callable[[Callable[[], object], str], bool]:
    """Attempt one effect, reporting success without letting it stop the rest."""

    def attempt(effect: Callable[[], object], what: str) -> bool:
        try:
            effect()
            return True
        except Exception:
            logger.warning(
                "[TECH_LEAD] Failed to %s for issue #%d on timeout terminate",
                what,
                issue_number,
                exc_info=True,
            )
            return False

    return attempt


__all__ = ["TechLeadTerminationHost", "terminate_tech_lead_session"]
